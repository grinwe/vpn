from datetime import timedelta
import hmac
import logging

from .time_utils import utcnow
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import (
    SCOPE_PROBE_READ,
    SCOPE_PROBE_WRITE,
    SCOPE_TRAFFIC_WRITE,
    ALL_SCOPES,
    AuthPrincipal,
    generate_token,
    require_scope,
)
from .config import get_settings
from .db import SessionLocal
from .security import decrypt as _decrypt
from .security import encrypt as _encrypt
from .services.autoscale import evaluate_all_pools
from .services.payments import ProviderError, get_provider
from .services.health import record_probe, recompute_node_health
from .services.node_spawner import NodeSpawnError, destroy_node, spawn_node
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
    if not x_admin_token or not hmac.compare_digest(
        x_admin_token.encode("utf-8"),
        settings.admin_api_token.encode("utf-8"),
    ):
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
        extra=metadata,
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
            connection_uri=_decrypt(device.connection_uri),
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
        # The API image intentionally ships without ansible (the provisioning
        # worker image carries it). Deep health-check therefore probes the
        # task queue instead: if we cannot enqueue, new subscriptions will
        # silently fall back to the legacy in-process thread runner.
        from .queue import get_queue

        queue = get_queue()
        if queue is None:
            raise HTTPException(status_code=500, detail="queue backend unreachable")
    return {"status": "ok"}


@router.get("/stats", response_model=schemas.StatsOut)
def get_stats(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """High-level counters for the admin dashboard.

    Everything is a cheap ``COUNT(*)`` on indexed columns — safe to hit
    on every dashboard render. For anything heavier (time series, per-
    node traffic, probe histograms) point people at Grafana instead of
    growing this endpoint.
    """
    from sqlalchemy import func as sa_func

    users_total = db.query(sa_func.count(models.User.id)).scalar() or 0
    subs_active = (
        db.query(sa_func.count(models.Subscription.id))
        .filter(models.Subscription.status == models.SubscriptionStatus.active)
        .scalar()
        or 0
    )
    subs_total = db.query(sa_func.count(models.Subscription.id)).scalar() or 0
    invoices_pending = (
        db.query(sa_func.count(models.Invoice.id))
        .filter(models.Invoice.status == models.InvoiceStatus.pending)
        .scalar()
        or 0
    )
    nodes_total = db.query(sa_func.count(models.VPNNode.id)).scalar() or 0
    nodes_active = (
        db.query(sa_func.count(models.VPNNode.id))
        .filter(models.VPNNode.status == models.VPNNodeStatus.active)
        .scalar()
        or 0
    )
    devices_active = (
        db.query(sa_func.count(models.Device.id))
        .filter(models.Device.status == models.DeviceStatus.active)
        .scalar()
        or 0
    )
    tasks_pending = (
        db.query(sa_func.count(models.ProvisioningTask.id))
        .filter(
            models.ProvisioningTask.status.in_(
                [
                    models.ProvisioningTaskStatus.pending,
                    models.ProvisioningTaskStatus.running,
                ]
            )
        )
        .scalar()
        or 0
    )
    tasks_failed = (
        db.query(sa_func.count(models.ProvisioningTask.id))
        .filter(models.ProvisioningTask.status == models.ProvisioningTaskStatus.failed)
        .scalar()
        or 0
    )
    return schemas.StatsOut(
        users_total=users_total,
        subscriptions_active=subs_active,
        subscriptions_total=subs_total,
        invoices_pending=invoices_pending,
        nodes_total=nodes_total,
        nodes_active=nodes_active,
        devices_active=devices_active,
        provisioning_tasks_pending=tasks_pending,
        provisioning_tasks_failed=tasks_failed,
    )


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
def list_nodes(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    region: str | None = None,
    pool_id: int | None = None,
    is_active: bool | None = None,
):
    query = db.query(models.VPNNode)
    if status_filter:
        try:
            query = query.filter(models.VPNNode.status == models.VPNNodeStatus(status_filter))
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid status") from exc
    if region:
        query = query.filter(models.VPNNode.region == region)
    if pool_id is not None:
        query = query.filter(models.VPNNode.pool_id == pool_id)
    if is_active is not None:
        query = query.filter(models.VPNNode.is_active.is_(is_active))
    nodes = (
        query.order_by(models.VPNNode.created_at)
        .offset(offset)
        .limit(limit)
        .all()
    )
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
def list_tasks(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    target_type: str | None = None,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    query = db.query(models.ProvisioningTask)
    if status_filter:
        try:
            query = query.filter(
                models.ProvisioningTask.status == models.ProvisioningTaskStatus(status_filter)
            )
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid status") from exc
    if target_type:
        query = query.filter(models.ProvisioningTask.target_type == target_type)
    tasks = (
        query.order_by(models.ProvisioningTask.created_at.desc())
        .offset(offset)
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
        connection_uri=_decrypt(device.connection_uri),
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


@router.get("/users", response_model=list[schemas.UserOut])
def list_users(
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    q = db.query(models.User)
    if search:
        like = f"%{search}%"
        q = q.filter(
            (models.User.telegram_id.ilike(like)) | (models.User.email.ilike(like))
        )
    users = q.order_by(models.User.id.desc()).offset(offset).limit(limit).all()
    if not users:
        return []
    user_ids = [u.id for u in users]
    counts_rows = (
        db.query(models.Subscription.user_id, models.Subscription.id)
        .filter(models.Subscription.user_id.in_(user_ids))
        .all()
    )
    counts: dict[int, int] = {}
    for uid, _sid in counts_rows:
        counts[uid] = counts.get(uid, 0) + 1
    return [
        schemas.UserOut(
            id=u.id,
            telegram_id=u.telegram_id,
            email=u.email,
            created_at=u.created_at,
            subscription_count=counts.get(u.id, 0),
        )
        for u in users
    ]


@router.get("/users/by_telegram/{telegram_id}", response_model=list[schemas.SubscriptionOut])
def get_user_by_telegram(
    telegram_id: str,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _subscriptions_for_user(user.id, db)


@router.get("/users/{user_id}", response_model=list[schemas.SubscriptionOut])
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
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


def _apply_traffic_delta(
    db: Session,
    sub: models.Subscription,
    *,
    delta_mb: int,
    actor: str,
    actor_type: models.AuditActor,
) -> tuple[bool, list[int]]:
    """Add ``delta_mb`` to a subscription's counter, revoke if over limit.

    Shared by the admin-facing single-subscription endpoint and the
    node-side batch traffic ingestion endpoint — both need identical
    over-limit semantics, so the logic lives here rather than being
    duplicated. Commits on its own; caller should not have open changes
    on the same subscription row.
    """
    sub.traffic_used_mb = (sub.traffic_used_mb or 0) + max(0, int(delta_mb))
    over_limit = bool(
        sub.traffic_limit_mb is not None and sub.traffic_used_mb > sub.traffic_limit_mb
    )

    revocation_task_ids: list[int] = []
    if over_limit and sub.status != models.SubscriptionStatus.blocked:
        sub.status = models.SubscriptionStatus.blocked
        sub.notes = "traffic limit exceeded"
        orchestrator = ProvisioningOrchestrator(db)
        tasks = orchestrator.revoke_subscription_devices(sub, reason="traffic limit exceeded")
        revocation_task_ids = [task.id for task in tasks]
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

    return over_limit, revocation_task_ids


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

    actor, actor_type = _resolve_admin_actor(admin_actor)
    over_limit, revocation_task_ids = _apply_traffic_delta(
        db,
        sub,
        delta_mb=payload.used_mb,
        actor=actor,
        actor_type=actor_type,
    )
    db.refresh(sub)
    return schemas.SubscriptionTrafficOut(
        subscription_id=sub.id,
        status=sub.status.value,
        traffic_used_mb=sub.traffic_used_mb,
        traffic_limit_mb=sub.traffic_limit_mb,
        over_limit=over_limit,
        revocation_task_ids=revocation_task_ids,
    )


@router.post(
    "/nodes/{node_id}/traffic",
    response_model=schemas.NodeTrafficIngestOut,
)
def ingest_node_traffic(
    node_id: int,
    payload: schemas.NodeTrafficReport,
    db: Session = Depends(get_db),
    principal: AuthPrincipal = Depends(require_scope(SCOPE_TRAFFIC_WRITE)),
):
    """Accept a batch of per-user traffic counters from a node collector.

    Node-side collector (see ``infra/ansible/roles/install_traffic_collector``)
    reads and zeros iptables byte counters on a short cadence and POSTs the
    deltas here. Each sample carries the device's ``access_username`` — we
    look it up on this specific node, attribute the bytes to the owning
    subscription, and reuse :func:`_apply_traffic_delta` so the over-limit
    path is identical to the admin endpoint.

    Unknown usernames are *not* an error: they usually mean the device was
    revoked between the collector's last refresh of ``users.d`` and this
    report. We just skip them and return the list so the collector can log
    and move on.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    # Per-node lookup: Device.access_username is unique only within a node,
    # so we must filter by the node via the credential->config chain.
    devices_by_username: dict[str, models.Device] = {}
    for device in (
        db.query(models.Device)
        .join(models.VPNConfig, models.Device.config_id == models.VPNConfig.id)
        .filter(models.VPNConfig.node_id == node_id)
        .all()
    ):
        if device.access_username:
            devices_by_username[device.access_username] = device

    per_sub_delta_mb: dict[int, int] = {}
    unknown: list[str] = []
    accepted = 0
    for sample in payload.samples:
        device = devices_by_username.get(sample.access_username)
        if not device:
            unknown.append(sample.access_username)
            continue
        total_bytes = int(sample.uplink_bytes) + int(sample.downlink_bytes)
        # Round up on conversion so that tiny but non-zero bursts are
        # accounted — otherwise a user exhausting their plan in <1MB chunks
        # would never trip the limit.
        delta_mb = (total_bytes + (1024 * 1024 - 1)) // (1024 * 1024) if total_bytes > 0 else 0
        if delta_mb <= 0:
            accepted += 1
            continue
        per_sub_delta_mb[device.subscription_id] = (
            per_sub_delta_mb.get(device.subscription_id, 0) + delta_mb
        )
        accepted += 1

    results: list[schemas.NodeTrafficSubscriptionResult] = []
    for subscription_id, delta_mb in per_sub_delta_mb.items():
        sub = db.get(models.Subscription, subscription_id)
        if not sub:
            continue
        over_limit, task_ids = _apply_traffic_delta(
            db,
            sub,
            delta_mb=delta_mb,
            actor=f"traffic-collector:node-{node_id}",
            actor_type=models.AuditActor.system,
        )
        db.refresh(sub)
        results.append(
            schemas.NodeTrafficSubscriptionResult(
                subscription_id=sub.id,
                used_mb_delta=delta_mb,
                used_mb_total=sub.traffic_used_mb or 0,
                over_limit=over_limit,
                revocation_task_ids=task_ids,
            )
        )

    # Bump last-seen on the node itself — a successful traffic report is
    # also a strong liveness signal.
    node.last_health_check_at = utcnow()
    db.commit()

    return schemas.NodeTrafficIngestOut(
        node_id=node_id,
        accepted_samples=accepted,
        unknown_usernames=unknown,
        subscriptions=results,
    )


@router.get("/subscriptions/{subscription_id}/status", response_model=schemas.SubscriptionStatusOut)
def get_subscription_status(subscription_id: int, db: Session = Depends(get_db)):
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    is_active = sub.status == models.SubscriptionStatus.active and sub.expires_at > utcnow()
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


def _mark_invoice_paid_core(
    db: Session,
    invoice_id: int,
    *,
    actor: str,
    actor_type: models.AuditActor,
    payment_id: int | None = None,
) -> schemas.InvoicePaidOut:
    """Internal helper shared by the admin endpoint and payment webhooks.

    Kept outside of the route function so that the payment webhook code path
    (which does its own auth via HMAC signature, not admin token) can reuse
    the exact same "flip invoice to paid → provision subscription" logic.
    """
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

    if payment_id:
        payment = db.get(models.Payment, payment_id)
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
            now = utcnow()
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


@router.post("/invoices/{invoice_id}/mark_paid", response_model=schemas.InvoicePaidOut)
def mark_invoice_paid(
    invoice_id: int,
    body: schemas.InvoiceMarkPaidRequest | None = None,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    actor, actor_type = _resolve_admin_actor(admin_actor)
    return _mark_invoice_paid_core(
        db,
        invoice_id,
        actor=actor,
        actor_type=actor_type,
        payment_id=body.payment_id if body else None,
    )


# ---------------------------------------------------------------------------
# Cloud providers and automated node spawning
# ---------------------------------------------------------------------------


@router.post("/cloud/providers", response_model=schemas.CloudProviderOut)
def create_cloud_provider(
    payload: schemas.CloudProviderCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    try:
        kind = models.CloudProviderKind(payload.kind)
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Unknown provider kind") from exc

    provider = models.CloudProvider(
        name=payload.name,
        kind=kind,
        api_token_enc=_encrypt(payload.api_token),
        default_image=payload.default_image,
        default_region=payload.default_region,
        default_plan=payload.default_plan,
        ssh_key_ids=payload.ssh_key_ids,
        is_active=payload.is_active,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "cloud_provider_created", "cloud_provider", provider.id, actor_type=actor_type)
    return schemas.CloudProviderOut(
        id=provider.id,
        name=provider.name,
        kind=provider.kind.value,
        default_image=provider.default_image,
        default_region=provider.default_region,
        default_plan=provider.default_plan,
        ssh_key_ids=provider.ssh_key_ids,
        is_active=provider.is_active,
        created_at=provider.created_at,
    )


@router.get("/cloud/providers", response_model=list[schemas.CloudProviderOut])
def list_cloud_providers(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    rows = db.query(models.CloudProvider).order_by(models.CloudProvider.id).all()
    return [
        schemas.CloudProviderOut(
            id=p.id,
            name=p.name,
            kind=p.kind.value,
            default_image=p.default_image,
            default_region=p.default_region,
            default_plan=p.default_plan,
            ssh_key_ids=p.ssh_key_ids,
            is_active=p.is_active,
            created_at=p.created_at,
        )
        for p in rows
    ]


@router.post("/nodes/spawn", response_model=schemas.VPNNodeOut)
def spawn_node_route(
    payload: schemas.NodeSpawnRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    try:
        node, _task = spawn_node(
            db,
            provider_id=payload.provider_id,
            name=payload.name,
            region=payload.region,
            plan=payload.plan,
            image=payload.image,
            ssh_key_ids=payload.ssh_key_ids,
            pool_id=payload.pool_id,
            user_data=payload.user_data,
            notes=payload.notes,
        )
    except NodeSpawnError as exc:
        raise HTTPException(status_code=502, detail=f"spawn failed: {exc}") from exc

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_spawned",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={
            "provider_id": payload.provider_id,
            "region": payload.region,
            "plan": payload.plan,
        },
    )
    return schemas.VPNNodeOut.from_orm(node)


@router.post("/nodes/{node_id}/destroy")
def destroy_node_route(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        destroy_node(db, node)
    except NodeSpawnError as exc:
        raise HTTPException(status_code=502, detail=f"destroy failed: {exc}") from exc
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "node_destroyed", "vpn_node", node.id, actor_type=actor_type)
    return {"node_id": node.id, "status": node.status.value}


# ---------------------------------------------------------------------------
# Health probes / auto-migration
# ---------------------------------------------------------------------------


@router.post("/nodes/{node_id}/probes", response_model=schemas.NodeHealthOut)
def submit_probe(
    node_id: int,
    payload: schemas.HealthProbeIn,
    db: Session = Depends(get_db),
    principal: AuthPrincipal = Depends(require_scope(SCOPE_PROBE_WRITE)),
):
    """Accept a probe observation and recompute node health.

    Intended to be called by external probers (active check rigs in different
    regions) or by a passive ingestor that aggregates client-side telemetry.
    Requires the admin token so that random actors cannot poison health data.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        result = models.ProbeResult(payload.result)
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid probe result") from exc

    record_probe(
        db,
        node=node,
        source_region=payload.source_region,
        result=result,
        latency_ms=payload.latency_ms,
        source_kind=payload.source_kind,
        details=payload.details,
    )
    summary = recompute_node_health(db, node)
    return schemas.NodeHealthOut(**summary)


@router.get("/probes/targets", response_model=schemas.ProbeTargetList)
def list_probe_targets(
    db: Session = Depends(get_db),
    principal: AuthPrincipal = Depends(require_scope(SCOPE_PROBE_READ)),
):
    """Return the list of node endpoints that probe agents should check.

    Called by out-of-band probe rigs in each source region (ru-mts, kz, eu…)
    on a schedule. The agent pulls this list, runs its checks, and POSTs
    the outcome of each endpoint back to ``/nodes/{id}/probes``.

    Only nodes that are active and past the ``registering`` stage are
    included — a half-provisioned node returning ``refused`` from a probe
    rig would otherwise cook its health score before real traffic ever
    reached it.
    """
    nodes = (
        db.query(models.VPNNode)
        .filter(
            models.VPNNode.is_active.is_(True),
            models.VPNNode.status != models.VPNNodeStatus.registering,
        )
        .all()
    )

    targets: list[schemas.ProbeTarget] = []
    for node in nodes:
        endpoints: list[schemas.ProbeTargetEndpoint] = []
        for cfg in node.configs:
            if not cfg.is_enabled:
                continue
            kind = "tls" if cfg.sni else "tcp"
            endpoints.append(
                schemas.ProbeTargetEndpoint(
                    protocol=cfg.protocol.value if hasattr(cfg.protocol, "value") else str(cfg.protocol),
                    port=cfg.port,
                    kind=kind,
                    sni=cfg.sni,
                )
            )
        if not endpoints:
            continue
        targets.append(
            schemas.ProbeTarget(
                node_id=node.id,
                name=node.name,
                region=node.region,
                host=node.host,
                endpoints=endpoints,
            )
        )

    return schemas.ProbeTargetList(generated_at=utcnow(), targets=targets)


@router.get("/nodes/{node_id}/health", response_model=schemas.NodeHealthOut)
def get_node_health(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    summary = recompute_node_health(db, node, auto_migrate=False)
    return schemas.NodeHealthOut(**summary)


@router.post("/nodes/{node_id}/migrate")
def migrate_node_route(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Force migration of all active subscriptions off this node."""
    from .services.health import migrate_subscriptions_off

    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    migrated = migrate_subscriptions_off(db, node, reason="manual migration")
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_migrated",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={"migrated_count": len(migrated)},
    )
    return {"node_id": node.id, "migrated_subscriptions": migrated}


# ---------------------------------------------------------------------------
# Autoscale
# ---------------------------------------------------------------------------


def _pool_to_autoscale_out(pool: models.ServerPool) -> schemas.PoolAutoscaleOut:
    return schemas.PoolAutoscaleOut(
        pool_id=pool.id,
        pool_name=pool.name,
        autoscale_enabled=bool(pool.autoscale_enabled),
        autoscale_provider_id=pool.autoscale_provider_id,
        autoscale_region=pool.autoscale_region,
        autoscale_plan=pool.autoscale_plan,
        autoscale_image=pool.autoscale_image,
        autoscale_high_watermark=(
            float(pool.autoscale_high_watermark)
            if pool.autoscale_high_watermark is not None
            else None
        ),
        autoscale_max_nodes=pool.autoscale_max_nodes,
    )


@router.put("/pools/{pool_id}/autoscale", response_model=schemas.PoolAutoscaleOut)
def update_pool_autoscale(
    pool_id: int,
    body: schemas.PoolAutoscaleConfig,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    pool = db.get(models.ServerPool, pool_id)
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")

    for field in (
        "autoscale_enabled",
        "autoscale_provider_id",
        "autoscale_region",
        "autoscale_plan",
        "autoscale_image",
        "autoscale_high_watermark",
        "autoscale_max_nodes",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(pool, field, value)

    db.add(pool)
    db.commit()
    db.refresh(pool)

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "pool_autoscale_updated", "server_pool", pool.id, actor_type=actor_type)
    return _pool_to_autoscale_out(pool)


@router.post("/autoscale/tick", response_model=list[schemas.PoolDecisionOut])
def autoscale_tick(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Run one autoscale evaluation synchronously.

    Intended for smoke tests and manual operator runs. The periodic loop
    runs inside the worker process (``AUTOSCALE_INTERVAL`` env var) — this
    endpoint bypasses the queue and reports decisions right away.
    """
    decisions = evaluate_all_pools(db)
    return [
        schemas.PoolDecisionOut(
            pool_id=d.pool_id,
            pool_name=d.pool_name,
            utilization=d.utilization,
            total_capacity=d.total_capacity,
            active_subs=d.active_subs,
            node_count=d.node_count,
            scaled_up=d.scaled_up,
            new_node_id=d.new_node_id,
            reason=d.reason,
        )
        for d in decisions
    ]


# ---------------------------------------------------------------------------
# Payment gateway: checkout + webhooks
# ---------------------------------------------------------------------------


@router.post("/invoices/{invoice_id}/checkout", response_model=schemas.InvoiceCheckoutOut)
def checkout_invoice(
    invoice_id: int,
    body: schemas.InvoiceCheckoutRequest | None = None,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin_token),
):
    """Create a payment-provider invoice and return the pay URL.

    The bot calls this after the user picks a plan; we persist a
    ``Payment(status=pending, provider=..., external_id=...)`` so that the
    webhook handler can correlate the callback back to our invoice.
    """
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status != models.InvoiceStatus.pending:
        raise HTTPException(status_code=400, detail="Invoice is not in pending state")

    provider_name = (body.provider if body else None) or None
    try:
        provider = get_provider(provider_name)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        provider_invoice = provider.create_invoice(
            invoice_id=invoice.id,
            amount=float(invoice.amount),
            currency=invoice.currency,
            description=f"VPN subscription #{invoice.id}",
            return_url=(body.return_url if body else None),
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"payment provider error: {exc}") from exc

    # Persist a Payment row — this is what the webhook will look up.
    # subscription_id may be NULL for new_subscription invoices; the link
    # back to the checkout is via invoice_id.
    payment = models.Payment(
        subscription_id=invoice.subscription_id,
        invoice_id=invoice.id,
        amount=invoice.amount,
        currency=invoice.currency,
        status=models.PaymentStatus.pending,
        provider=provider.name,
        external_id=provider_invoice.external_id,
    )
    db.add(payment)
    db.commit()

    return schemas.InvoiceCheckoutOut(
        invoice_id=invoice.id,
        provider=provider.name,
        external_id=provider_invoice.external_id,
        pay_url=provider_invoice.pay_url,
        amount=provider_invoice.amount,
        currency=provider_invoice.currency,
    )


@router.post("/payments/webhook/{provider_name}")
async def payment_webhook(
    provider_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Handle asynchronous callbacks from payment providers.

    Auth is HMAC-signature based (verified inside the provider driver), not
    via the admin token — so this endpoint is intentionally unauthenticated
    from FastAPI's point of view. A missing or invalid signature returns
    401. A successful ``paid`` event runs the same invoice→subscription
    pipeline as the admin ``mark_paid`` route.
    """
    try:
        provider = get_provider(provider_name)
    except ProviderError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        event = provider.verify_webhook(body, headers)
    except ProviderError as exc:
        logger.warning("webhook verification failed for %s: %s", provider_name, exc)
        raise HTTPException(status_code=401, detail="invalid webhook") from exc

    if event.status != "paid":
        # Record the event but don't flip the invoice — expired / other
        # statuses are not actionable for us beyond logging.
        logger.info("webhook event %s for invoice %s — no action", event.status, event.external_id)
        return {"ok": True, "status": event.status}

    try:
        invoice_id = int(event.external_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="webhook payload is not an invoice id") from exc

    # Lock the invoice row and mark a Payment as paid if we have one.
    pending_payment = (
        db.query(models.Payment)
        .filter(
            models.Payment.invoice_id == invoice_id,
            models.Payment.provider == provider.name,
        )
        .order_by(models.Payment.id.desc())
        .first()
    )
    payment_id = pending_payment.id if pending_payment else None

    result = _mark_invoice_paid_core(
        db,
        invoice_id,
        actor=f"{provider.name}:webhook",
        actor_type=models.AuditActor.system,
        payment_id=payment_id,
    )
    return {"ok": True, "invoice_id": result.id, "status": result.status}


# ---------------------------------------------------------------------------
# Scoped API tokens — admin CRUD
# ---------------------------------------------------------------------------


@router.get("/api-tokens", response_model=list[schemas.ApiTokenOut])
def list_api_tokens(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """List all scoped API tokens (hashes never leave the DB)."""
    rows = db.query(models.ApiToken).order_by(models.ApiToken.id).all()
    return [schemas.ApiTokenOut.from_orm(r) for r in rows]


@router.post(
    "/api-tokens",
    response_model=schemas.ApiTokenCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
def create_api_token(
    payload: schemas.ApiTokenCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Mint a new scoped token.

    The plaintext is returned exactly once in the response body — it is
    never stored and cannot be recovered later. Validate scopes against
    the canonical list so a typo in a curl call doesn't quietly create a
    token with zero capabilities.
    """
    bad = [s for s in payload.scopes if s not in ALL_SCOPES]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unknown scopes: {bad}. allowed: {list(ALL_SCOPES)}",
        )
    if db.query(models.ApiToken).filter(models.ApiToken.name == payload.name).first():
        raise HTTPException(status_code=409, detail="token with that name already exists")

    plaintext, token_hash = generate_token()
    row = models.ApiToken(
        name=payload.name,
        token_hash=token_hash,
        scopes=payload.scopes,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _audit(
        db,
        actor="admin",
        action="api_token.create",
        target_type="api_token",
        target_id=row.id,
        metadata={"name": row.name, "scopes": row.scopes},
        actor_type=models.AuditActor.admin,
    )
    db.commit()

    out = schemas.ApiTokenCreatedOut(
        id=row.id,
        name=row.name,
        scopes=list(row.scopes or []),
        is_active=row.is_active,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        token=plaintext,
    )
    return out


@router.delete("/api-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_token(
    token_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Disable a token. Row is kept for audit continuity."""
    row = db.get(models.ApiToken, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="token not found")
    row.is_active = False
    db.commit()
    _audit(
        db,
        actor="admin",
        action="api_token.revoke",
        target_type="api_token",
        target_id=row.id,
        metadata={"name": row.name},
        actor_type=models.AuditActor.admin,
    )
    db.commit()
