from datetime import datetime, timedelta
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
) -> tuple[models.Subscription, models.ProvisioningTask]:
    orchestrator = ProvisioningOrchestrator(db)
    try:
        sub, task = orchestrator.provision_subscription(
            user, plan, node_id=node_id, device_name=device_name
        )
        db.refresh(sub)
        _audit(db, user.telegram_id or "unknown", "subscription_created", "subscription", sub.id)
        return sub, task
    except Exception as exc:  # noqa: BLE001
        logger.exception("Provisioning failed for user %s", user.telegram_id)
        db.rollback()
        raise HTTPException(status_code=500, detail="Provisioning failed") from exc


def _invoice_with_credentials(
    invoice: models.Invoice,
    credentials: list[models.Credential] | None = None,
    subscription: models.Subscription | None = None,
    task: models.ProvisioningTask | None = None,
) -> schemas.InvoicePaidOut:
    creds = credentials or []
    device_id = None
    if subscription and subscription.devices:
        device_id = subscription.devices[0].id
    return schemas.InvoicePaidOut(
        id=invoice.id,
        user_id=invoice.user_id,
        user_telegram_id=invoice.user.telegram_id if invoice.user else None,
        plan_id=invoice.plan_id,
        plan_name=invoice.plan.name if invoice.plan else "",
        subscription_id=invoice.subscription_id,
        amount=float(invoice.amount),
        currency=invoice.currency,
        status=invoice.status.value,
        action=invoice.action.value,
        created_at=invoice.created_at,
        credentials=[schemas.CredentialOut.from_orm(c) for c in creds],
        provisioning_task_id=task.id if task else None,
        device_id=device_id,
    )


def _subscription_provision_response(
    subscription: models.Subscription, task: models.ProvisioningTask
) -> schemas.SubscriptionProvisionResponse:
    device = subscription.devices[0] if subscription.devices else None
    if not device:
        raise HTTPException(status_code=500, detail="Subscription has no device")

    return schemas.SubscriptionProvisionResponse(
        subscription_id=subscription.id,
        status=subscription.status.value,
        expires_at=subscription.expires_at,
        node_id=subscription.node_id,
        plan_id=subscription.plan_id,
        provisioning_task_id=task.id,
        device=schemas.DeviceStatusOut(
            id=device.id,
            name=device.name,
            status=device.status.value,
            config_id=device.config_id,
            access_username=device.access_username,
            connection_uri=device.connection_uri,
            credentials=[schemas.CredentialOut.from_orm(c) for c in device.credentials],
            provisioning_task_id=task.id,
        ),
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


@router.get("/provisioning/tasks/{task_id}", response_model=schemas.ProvisioningTaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(models.ProvisioningTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return schemas.ProvisioningTaskOut.from_orm(task)


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


@router.post("/subscriptions", response_model=schemas.SubscriptionProvisionResponse)
def create_subscription(
    payload: schemas.SubscriptionCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    plan = db.get(models.Plan, payload.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    user = _get_or_create_user(db, payload.telegram_id, payload.email)
    sub, task = _create_subscription_for_user(
        db, user, plan, node_id=payload.node_id, device_name=payload.device_name
    )
    return _subscription_provision_response(sub, task)


@router.get("/devices/{device_id}", response_model=schemas.DeviceStatusOut)
def get_device(
    device_id: int,
    telegram_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin_token),
):
    device = db.get(models.Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Ownership check: must be admin or provide matching telegram_id
    if not admin_token:
        if not telegram_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        owner = device.subscription.user if device.subscription else None
        if not owner or owner.telegram_id != telegram_id:
            raise HTTPException(status_code=403, detail="Forbidden")

    task = (
        db.query(models.ProvisioningTask)
        .filter(
            models.ProvisioningTask.target_type == "device",
            models.ProvisioningTask.target_id == device_id,
        )
        .order_by(models.ProvisioningTask.created_at.desc())
        .first()
    )
    return schemas.DeviceStatusOut(
        id=device.id,
        name=device.name,
        status=device.status.value,
        config_id=device.config_id,
        access_username=device.access_username,
        connection_uri=device.connection_uri,
        credentials=[schemas.CredentialOut.from_orm(c) for c in device.credentials],
        provisioning_task_id=task.id if task else None,
    )


@router.post("/devices/{device_id}/revoke", response_model=schemas.ProvisioningTaskOut)
def revoke_device(
    device_id: int,
    body: schemas.DisableRequest | None = None,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    device = db.get(models.Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.revoke_device(device, reason=body.reason if body else None)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "device_revoked",
        "device",
        device_id,
        metadata={"reason": body.reason if body else None},
        actor_type=actor_type,
    )
    db.refresh(task)
    return schemas.ProvisioningTaskOut.from_orm(task)


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
    orchestrator = ProvisioningOrchestrator(db)
    total_tasks = 0
    for sub in subs:
        sub.status = models.SubscriptionStatus.blocked
        sub.notes = body.reason
        tasks = orchestrator.revoke_subscription_devices(sub, reason=body.reason)
        total_tasks += len(tasks)
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
    return {"disabled": len(subs), "revocation_tasks": total_tasks}


def _subscriptions_for_user(user_id: int, db: Session) -> list[schemas.SubscriptionOut]:
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


@router.get("/users/by_telegram/{telegram_id}", response_model=list[schemas.SubscriptionOut])
def get_user_by_telegram(telegram_id: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _subscriptions_for_user(user.id, db)


@router.get("/users/{user_id}", response_model=list[schemas.SubscriptionOut])
def get_user(user_id: int, db: Session = Depends(get_db)):
    return _subscriptions_for_user(user_id, db)


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
    tasks = orchestrator.revoke_subscription_devices(sub, reason="disabled by admin")
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "subscription_disabled", "subscription", subscription_id, actor_type=actor_type)
    return {
        "subscription_id": sub.id,
        "status": sub.status.value,
        "revocation_tasks": [task.id for task in tasks],
    }


@router.post(
    "/subscriptions/{subscription_id}/traffic",
    response_model=schemas.SubscriptionTrafficOut,
)
def update_subscription_traffic(
    subscription_id: int,
    payload: schemas.SubscriptionTrafficUpdate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Increment traffic usage for a subscription and optionally block it when over limit."""

    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sub.traffic_used_mb = (sub.traffic_used_mb or 0) + payload.used_mb
    over_limit = bool(sub.traffic_limit_mb is not None and sub.traffic_used_mb > sub.traffic_limit_mb)

    revocation_task_ids: list[int] = []
    if over_limit and sub.status != models.SubscriptionStatus.blocked:
        sub.status = models.SubscriptionStatus.blocked
        sub.notes = "traffic limit exceeded"
        orchestrator = ProvisioningOrchestrator(db)
        tasks = orchestrator.revoke_subscription_devices(sub, reason="traffic limit exceeded")
        revocation_task_ids = [task.id for task in tasks]
        actor, actor_type = _resolve_admin_actor(admin_actor)
        _audit(
            db,
            actor,
            "subscription_over_limit",
            "subscription",
            sub.id,
            actor_type=actor_type,
            metadata={"used_mb": sub.traffic_used_mb, "limit_mb": sub.traffic_limit_mb},
        )
    else:
        db.commit()

    db.refresh(sub)
    return schemas.SubscriptionTrafficOut(
        subscription_id=sub.id,
        status=sub.status.value,
        traffic_used_mb=sub.traffic_used_mb,
        traffic_limit_mb=sub.traffic_limit_mb,
        over_limit=over_limit,
        revocation_task_ids=revocation_task_ids,
    )


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

    try:
        action = models.InvoiceAction(body.action)
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid invoice action") from exc

    user = _get_user_from_payload(db, body.user_id, body.telegram_id)
    if body.subscription_id:
        subscription = db.get(models.Subscription, body.subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if subscription.user_id != user.id or subscription.plan_id != plan.id:
            raise HTTPException(status_code=400, detail="Subscription does not match invoice data")
    amount = body.amount if body.amount is not None else float(plan.price)
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        subscription_id=body.subscription_id,
        amount=amount,
        currency=body.currency,
        action=action,
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
                subscription_id=inv.subscription_id,
                amount=float(inv.amount),
                currency=inv.currency,
                status=inv.status.value,
                action=inv.action.value,
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
    invoice = (
        db.query(models.Invoice)
        .filter(models.Invoice.id == invoice_id)
        .with_for_update()
        .first()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status == models.InvoiceStatus.failed:
        raise HTTPException(status_code=400, detail="Invoice is marked as failed")

    if body and body.payment_id:
        payment = db.get(models.Payment, body.payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.subscription and payment.subscription.user_id != invoice.user_id:
            raise HTTPException(status_code=400, detail="Payment does not belong to invoice user")
        payment.status = models.PaymentStatus.paid
        db.add(payment)

    latest_subscription = invoice.subscription
    if invoice.status == models.InvoiceStatus.paid:
        if not latest_subscription:
            latest_subscription = (
                db.query(models.Subscription)
                .filter(
                    models.Subscription.user_id == invoice.user_id,
                    models.Subscription.plan_id == invoice.plan_id,
                )
                .order_by(models.Subscription.created_at.desc())
                .first()
            )
        device = latest_subscription.devices[0] if latest_subscription and latest_subscription.devices else None
        task = None
        if device:
            task = (
                db.query(models.ProvisioningTask)
                .filter(
                    models.ProvisioningTask.target_type == "device",
                    models.ProvisioningTask.target_id == device.id,
                )
                .order_by(models.ProvisioningTask.created_at.desc())
                .first()
            )
        credentials = latest_subscription.credentials if latest_subscription else []
        return _invoice_with_credentials(invoice, credentials, subscription=latest_subscription, task=task)

    if invoice.status != models.InvoiceStatus.pending:
        raise HTTPException(status_code=400, detail="Invoice cannot be paid in current status")

    plan = db.get(models.Plan, invoice.plan_id)
    user = db.get(models.User, invoice.user_id)
    if not plan or not user:
        raise HTTPException(status_code=400, detail="Invoice is inconsistent: missing user or plan")

    credentials: list[models.Credential] = []
    subscription: models.Subscription | None = None
    task: models.ProvisioningTask | None = None
    try:
        if invoice.action == models.InvoiceAction.renewal:
            if not invoice.subscription_id:
                raise HTTPException(status_code=400, detail="Invoice missing subscription for renewal")
            subscription = db.get(models.Subscription, invoice.subscription_id)
            if not subscription:
                raise HTTPException(status_code=404, detail="Subscription not found for renewal")
            if subscription.user_id != invoice.user_id or subscription.plan_id != invoice.plan_id:
                raise HTTPException(status_code=400, detail="Invoice does not match subscription")
            now = datetime.utcnow()
            base_time = subscription.expires_at if subscription.expires_at > now else now
            subscription.expires_at = base_time + timedelta(days=plan.duration_days)
            subscription.status = models.SubscriptionStatus.active
            db.add(subscription)
            invoice.subscription_id = subscription.id
            credentials = subscription.credentials
        else:
            if invoice.subscription_id:
                raise HTTPException(status_code=400, detail="Invoice already bound to subscription")
            subscription, task = _create_subscription_for_user(db, user, plan)
            credentials = subscription.credentials
            invoice.subscription_id = subscription.id
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process invoice %s", invoice_id)
        raise HTTPException(status_code=500, detail="Failed to create or update subscription") from exc

    invoice.status = models.InvoiceStatus.paid
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "invoice_paid",
        "invoice",
        invoice.id,
        actor_type=actor_type,
        metadata={"subscription_id": invoice.subscription_id},
    )
    return _invoice_with_credentials(invoice, credentials, subscription=subscription, task=task)
