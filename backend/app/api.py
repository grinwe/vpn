from datetime import datetime
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import models, schemas
from .config import get_settings
from .db import SessionLocal
from .services.ansible_runner import _ensure_ansible_root
from .services.provisioning import ProvisioningOrchestrator

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)
settings = get_settings()
ADMIN_ACTOR_HEADER = settings.admin_actor_header


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_admin(x_admin_token: str | None = Header(default=None)) -> str:
    """Simple admin authentication based on shared token header."""
    if not settings.admin_api_token:
        logger.error("ADMIN_API_TOKEN is not configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="admin token missing")
    if not x_admin_token or x_admin_token != settings.admin_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin token")
    return x_admin_token


def optional_admin_token(x_admin_token: str | None = Header(default=None)) -> str | None:
    if x_admin_token:
        return require_admin(x_admin_token)
    return None


def _audit(
    db: Session,
    actor: str,
    action: str,
    target_type: str,
    target_id: int | None,
    *,
    metadata: dict[str, Any] | None = None,
    actor_type: models.AuditActor = models.AuditActor.system,
) -> models.AuditLog:
    log = models.AuditLog(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata,
        actor_type=actor_type,
    )
    db.add(log)
    db.commit()
    return log


def _resolve_admin_actor(actor_header: str | None) -> tuple[str, models.AuditActor]:
    if actor_header:
        return actor_header, models.AuditActor.admin
    return "admin", models.AuditActor.admin


def _get_or_create_user(db: Session, telegram_id: str, email: str | None = None) -> models.User:
    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        user = models.User(telegram_id=telegram_id, email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
        _audit(db, telegram_id, "user_created", "user", user.id)
    return user


def _get_user_from_payload(
    db: Session, user_id: int | None, telegram_id: str | None, email: str | None = None
) -> models.User:
    if user_id is not None:
        user = db.get(models.User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    if telegram_id:
        return _get_or_create_user(db, telegram_id, email)

    raise HTTPException(status_code=400, detail="user_id or telegram_id is required")


def _create_subscription_for_user(
    db: Session,
    user: models.User,
    plan: models.Plan,
    node_id: int | None = None,
    device_name: str | None = None,
) -> models.Subscription:
    orchestrator = ProvisioningOrchestrator(db)
    try:
        sub = orchestrator.provision_subscription(user, plan, node_id=node_id, device_name=device_name)
        db.refresh(sub)
        _audit(db, user.telegram_id or "unknown", "subscription_created", "subscription", sub.id)
        return sub
    except Exception as exc:  # noqa: BLE001
        logger.exception("Provisioning failed for user %s", user.telegram_id)
        db.rollback()
        raise HTTPException(status_code=500, detail="Provisioning failed") from exc


def _invoice_with_credentials(
    invoice: models.Invoice,
    credentials: list[models.Credential] | None = None,
) -> schemas.InvoicePaidOut:
    creds = credentials or []
    return schemas.InvoicePaidOut(
        id=invoice.id,
        user_id=invoice.user_id,
        user_telegram_id=invoice.user.telegram_id if invoice.user else None,
        plan_id=invoice.plan_id,
        plan_name=invoice.plan.name if invoice.plan else "",
        amount=float(invoice.amount),
        currency=invoice.currency,
        status=invoice.status.value,
        created_at=invoice.created_at,
        credentials=[schemas.CredentialOut.from_orm(c) for c in creds],
    )


@router.get("/healthz")
def healthcheck(db: Session = Depends(get_db), deep: bool = Query(default=False)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="database not reachable")
    if deep:
        try:
            _ensure_ansible_root()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"ansible not available: {exc}") from exc
    return {"status": "ok"}


@router.post("/nodes", response_model=schemas.VPNNodeOut)
def create_node(
    payload: schemas.VPNNodeCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    node = models.VPNNode(
        name=payload.name,
        region=payload.region,
        host=payload.host,
        ssh_port=payload.ssh_port,
        pool_id=payload.pool_id,
        notes=payload.notes,
        status=models.VPNNodeStatus.registering,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "node_created", "vpn_node", node.id, actor_type=actor_type)
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task("node", node.id, "bootstrap", {"pool_id": payload.pool_id})
    db.commit()
    orchestrator.run_task_async(task, node=node)
    return node


@router.get("/nodes", response_model=list[schemas.VPNNodeOut])
def list_nodes(db: Session = Depends(get_db), admin_token: str = Depends(require_admin)):
    nodes = db.query(models.VPNNode).order_by(models.VPNNode.created_at).all()
    return [schemas.VPNNodeOut.from_orm(n) for n in nodes]


@router.post("/nodes/{node_id}/configs", response_model=schemas.VPNConfigOut)
def create_config(
    node_id: int,
    payload: schemas.VPNConfigCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    try:
        protocol = models.VPNConfigProtocol(payload.protocol)
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Unknown protocol") from exc

    config = models.VPNConfig(
        node_id=node.id,
        name=payload.name,
        protocol=protocol,
        port=payload.port,
        sni=payload.sni,
        public_key=payload.public_key,
        fallback=payload.fallback,
        settings=payload.settings,
        is_enabled=payload.is_enabled,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "config_created", "vpn_config", config.id, actor_type=actor_type)
    return config


@router.get("/nodes/{node_id}/configs", response_model=list[schemas.VPNConfigOut])
def list_configs(node_id: int, db: Session = Depends(get_db), admin_token: str = Depends(require_admin)):
    configs = db.query(models.VPNConfig).filter(models.VPNConfig.node_id == node_id).all()
    return [schemas.VPNConfigOut.from_orm(cfg) for cfg in configs]


@router.get("/provisioning/tasks", response_model=list[schemas.ProvisioningTaskOut])
def list_tasks(limit: int = 50, db: Session = Depends(get_db), admin_token: str = Depends(require_admin)):
    tasks = (
        db.query(models.ProvisioningTask)
        .order_by(models.ProvisioningTask.created_at.desc())
        .limit(limit)
        .all()
    )
    return [schemas.ProvisioningTaskOut.from_orm(t) for t in tasks]


@router.post("/provisioning/tasks/{task_id}/execute", response_model=schemas.ProvisioningTaskOut)
def execute_task(task_id: int, db: Session = Depends(get_db), admin_token: str = Depends(require_admin)):
    task = db.get(models.ProvisioningTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    orchestrator = ProvisioningOrchestrator(db)
    node = db.get(models.VPNNode, task.target_id) if task.target_type == "node" else None
    orchestrator.run_task_async(task, node=node)
    db.refresh(task)
    return schemas.ProvisioningTaskOut.from_orm(task)


@router.post("/provisioning/tasks/{task_id}/rerun", response_model=schemas.ProvisioningTaskOut)
def rerun_task(task_id: int, db: Session = Depends(get_db), admin_token: str = Depends(require_admin)):
    task = db.get(models.ProvisioningTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {models.ProvisioningTaskStatus.failed, models.ProvisioningTaskStatus.pending}:
        raise HTTPException(status_code=400, detail="Task is not failed or pending")
    orchestrator = ProvisioningOrchestrator(db)
    orchestrator.reset_failed_task(task)
    node = db.get(models.VPNNode, task.target_id) if task.target_type == "node" else None
    orchestrator.run_task_async(task, node=node)
    db.refresh(task)
    return schemas.ProvisioningTaskOut.from_orm(task)


@router.post("/subscriptions", response_model=list[schemas.CredentialOut])
def create_subscription(payload: schemas.SubscriptionCreate, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, payload.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    user = _get_or_create_user(db, payload.telegram_id, payload.email)
    sub = _create_subscription_for_user(db, user, plan, node_id=payload.node_id, device_name=payload.device_name)
    return [schemas.CredentialOut.from_orm(cred) for cred in sub.credentials]


@router.post("/users/{user_id}/disable")
def disable_user(
    user_id: int,
    body: schemas.DisableRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    subs = db.query(models.Subscription).filter(models.Subscription.user_id == user_id).all()
    if not subs:
        raise HTTPException(status_code=404, detail="User or subscriptions not found")
    for sub in subs:
        sub.status = models.SubscriptionStatus.blocked
        sub.notes = body.reason
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "user_disabled",
        "user",
        user_id,
        metadata={"reason": body.reason},
        actor_type=actor_type,
    )
    return {"disabled": len(subs)}


@router.get("/users/{user_id}", response_model=list[schemas.SubscriptionOut])
def get_user(user_id: int, db: Session = Depends(get_db)):
    subs = (
        db.query(models.Subscription)
        .filter(models.Subscription.user_id == user_id)
        .all()
    )
    if not subs:
        raise HTTPException(status_code=404, detail="Subscriptions not found")
    result = []
    for sub in subs:
        item = schemas.SubscriptionOut(
            id=sub.id,
            plan_name=sub.plan.name,
            node=sub.node.name,
            region=sub.node.region,
            expires_at=sub.expires_at,
            status=sub.status.value,
            credentials=[schemas.CredentialOut.from_orm(c) for c in sub.credentials],
            devices=[schemas.DeviceOut.from_orm(d) for d in sub.devices],
        )
        result.append(item)
    return result


@router.post("/subscriptions/{subscription_id}/disable")
def disable_subscription(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(status_code=400, detail="Subscription is not active")

    orchestrator = ProvisioningOrchestrator(db)
    device = sub.devices[0] if sub.devices else None
    payload = {"subscription_id": subscription_id, "state": "absent"}
    if device and device.access_username:
        payload.update({"username": device.access_username, "port": device.config.port})
    task = orchestrator.create_task("device", subscription_id, "revoke", payload)
    sub.status = models.SubscriptionStatus.blocked
    sub.expires_at = datetime.utcnow()
    db.commit()
    orchestrator.run_task_async(task, node=sub.node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "subscription_disabled", "subscription", subscription_id, actor_type=actor_type)
    return {"subscription_id": sub.id, "status": sub.status.value}


@router.get("/subscriptions/{subscription_id}/status", response_model=schemas.SubscriptionStatusOut)
def get_subscription_status(subscription_id: int, db: Session = Depends(get_db)):
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    is_active = sub.status == models.SubscriptionStatus.active and sub.expires_at > datetime.utcnow()
    return schemas.SubscriptionStatusOut(
        plan_name=sub.plan.name,
        server_name=sub.node.name,
        expires_at=sub.expires_at,
        is_active=is_active,
        proto_configs=[schemas.CredentialOut.from_orm(c) for c in sub.credentials],
    )


@router.post("/payments")
def create_payment(
    body: schemas.PaymentCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    sub = db.get(models.Subscription, body.subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    payment = models.Payment(
        subscription_id=sub.id,
        amount=body.amount,
        currency=body.currency,
        status=models.PaymentStatus(body.status),
        provider=body.provider,
        external_id=body.external_id,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "payment_created", "payment", payment.id, actor_type=actor_type)
    return {"id": payment.id, "status": payment.status.value}


@router.post("/invoices", response_model=schemas.InvoiceOut)
def create_invoice(
    body: schemas.InvoiceCreate,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin_token),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    plan = db.get(models.Plan, body.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    user = _get_user_from_payload(db, body.user_id, body.telegram_id)
    amount = body.amount if body.amount is not None else float(plan.price)
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        amount=amount,
        currency=body.currency,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    if admin_token:
        actor, actor_type = _resolve_admin_actor(admin_actor)
    else:
        actor, actor_type = (body.telegram_id or str(user.id), models.AuditActor.user)
    _audit(db, actor, "invoice_created", "invoice", invoice.id, actor_type=actor_type)
    return schemas.InvoiceOut.from_orm(invoice)


@router.get("/plans", response_model=list[schemas.PlanOut])
def list_plans(db: Session = Depends(get_db)):
    plans = db.query(models.Plan).order_by(models.Plan.id).all()
    return [schemas.PlanOut.from_orm(plan) for plan in plans]


@router.get("/invoices", response_model=list[schemas.InvoiceListItem])
def list_invoices(
    status: str | None = None,
    limit: int = 10,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    query = db.query(models.Invoice).order_by(models.Invoice.created_at.desc())
    if status:
        try:
            invoice_status = models.InvoiceStatus(status)
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid status") from exc
        query = query.filter(models.Invoice.status == invoice_status)

    invoices = query.limit(limit).all()
    result: list[schemas.InvoiceListItem] = []
    for inv in invoices:
        result.append(
            schemas.InvoiceListItem(
                id=inv.id,
                user_id=inv.user_id,
                user_telegram_id=inv.user.telegram_id if inv.user else None,
                plan_id=inv.plan_id,
                plan_name=inv.plan.name if inv.plan else "",
                amount=float(inv.amount),
                currency=inv.currency,
                status=inv.status.value,
                created_at=inv.created_at,
            )
        )
    return result


@router.post("/invoices/{invoice_id}/mark_paid", response_model=schemas.InvoicePaidOut)
def mark_invoice_paid(
    invoice_id: int,
    body: schemas.InvoiceMarkPaidRequest | None = None,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status == models.InvoiceStatus.paid:
        subscription = (
            db.query(models.Subscription)
            .filter(
                models.Subscription.user_id == invoice.user_id,
                models.Subscription.plan_id == invoice.plan_id,
            )
            .order_by(models.Subscription.created_at.desc())
            .first()
        )
        credentials = subscription.credentials if subscription else []
        return _invoice_with_credentials(invoice, credentials)

    plan = db.get(models.Plan, invoice.plan_id)
    user = db.get(models.User, invoice.user_id)
    if not plan or not user:
        raise HTTPException(status_code=400, detail="Invoice is inconsistent: missing user or plan")

    if body and body.payment_id:
        payment = db.get(models.Payment, body.payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.subscription and payment.subscription.user_id != invoice.user_id:
            raise HTTPException(status_code=400, detail="Payment does not belong to invoice user")
        payment.status = models.PaymentStatus.paid
        db.add(payment)

    invoice.status = models.InvoiceStatus.paid
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    credentials: list[models.Credential] = []
    try:
        sub = _create_subscription_for_user(db, user, plan)
        credentials = sub.credentials
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to create subscription for invoice %s", invoice_id)
        raise HTTPException(status_code=500, detail="Failed to create subscription") from exc

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "invoice_paid", "invoice", invoice.id, actor_type=actor_type)
    return _invoice_with_credentials(invoice, credentials)
