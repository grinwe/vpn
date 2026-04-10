from datetime import timedelta
import logging

from .time_utils import utcnow
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
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
    optional_admin as optional_admin_token,
    require_admin,
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


@router.post("/nodes/{node_id}/resync")
def resync_node_clients(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Force re-push of all active VLESS+Reality users onto a node.

    Safety net for the "empty clients after site.yml" class of bugs:
    if an operator suspects the node has drifted from the backend's
    view of who's provisioned (e.g. after a manual config edit, a
    restore-from-backup, or a half-broken bootstrap), this endpoint
    enqueues a resync task that re-adds every active credential via
    manage_vless_user.sh. The helper is idempotent so running this
    in any node state is safe.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.resync_node_vless_clients(node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_resync",
        "vpn_node",
        node.id,
        metadata={"task_id": task.id if task else None},
        actor_type=actor_type,
    )
    if not task:
        return {"node_id": node.id, "task_id": None, "clients": 0}
    return {
        "node_id": node.id,
        "task_id": task.id,
        "clients": len((task.payload or {}).get("clients", [])),
    }


@router.post("/nodes/{node_id}/bootstrap")
def rebootstrap_node(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Re-run site.yml against an existing node from scratch.

    Use when you want to reapply the full ansible role stack to a node
    — rolling out role changes, recovering from a half-broken manual
    edit, rotating keys, etc. Creates a fresh ``bootstrap`` task (same
    action the initial node creation uses) and hands it to the
    orchestrator. The install_vless_reality role preserves existing
    clients across re-renders, and the post-site.yml auto-resync
    covers the edge case where it can't (see NODES.md § VLESS client
    resync), so running this on a node with active users is safe.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task(
        "node", node.id, "bootstrap", {"pool_id": node.pool_id, "rerun": True}
    )
    db.commit()
    orchestrator.run_task_async(task, node=node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_bootstrap_rerun",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={"task_id": task.id},
    )
    return {"node_id": node.id, "task_id": task.id}


@router.post("/nodes/{node_id}/active", response_model=schemas.VPNNodeOut)
def set_node_active(
    node_id: int,
    body: dict,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Toggle ``is_active``. A node with ``is_active=False`` stays up but
    is excluded from the scheduler in :func:`services.provisioning._pick_node`,
    so new subscriptions won't land on it. Existing subs keep working.
    Useful for staging a freshly-added node for manual testing before it
    starts taking real traffic.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    if "is_active" not in body:
        raise HTTPException(400, "is_active required")
    node.is_active = bool(body["is_active"])
    db.add(node)
    db.commit()
    db.refresh(node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "node_set_active",
        "vpn_node", node.id,
        actor_type=actor_type,
        metadata={"is_active": node.is_active},
    )
    db.commit()
    return schemas.VPNNodeOut.from_orm(node)


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

    # Only one config per protocol per node — the extra_vars collector
    # and the ansible roles both assume this. Silently returning the
    # existing row (as the idempotent helpers used to do) made the
    # admin form look broken: operators hit "+ Добавить" and nothing
    # changed. Fail loud instead; to replace a config, delete first.
    existing_same_protocol = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == protocol,
        )
        .first()
    )
    if existing_same_protocol is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Config for protocol {protocol.value} already exists on this node "
                f"(id={existing_same_protocol.id}). Delete it first to replace."
            ),
        )

    # VLESS Reality: if the admin left public_key empty we generate the
    # whole keypair + short_id backend-side via the shared helper. The
    # add-config form in the admin UI relies on this behavior (see
    # admin/src/pages/Nodes.tsx comment on AddConfigForm).
    if protocol == models.VPNConfigProtocol.vless_reality and not payload.public_key:
        from .services.node_spawner import ensure_reality_config
        config = ensure_reality_config(
            db, node,
            port=payload.port or None,
            sni=payload.sni or None,
            dest=payload.fallback or None,
        )
    # ShadowTLS+SS: backend owns both secrets (outer shadow-tls password
    # and inner ss-rust PSK). The admin form only supplies port + SNI;
    # passwords are generated here and stored encrypted.
    elif protocol == models.VPNConfigProtocol.shadowtls_ss and not (
        payload.settings or {}
    ).get("ss_password_enc"):
        from .services.node_spawner import ensure_shadowtls_config
        config = ensure_shadowtls_config(
            db, node,
            port=payload.port or None,
            handshake_domain=payload.sni or None,
            name=payload.name or None,
        )
    else:
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
    # Existing warm bundles were built against the previous protocol set;
    # drop them so the warmer rebuilds with the new config included.
    from .services import warm_pool
    warm_pool.invalidate_node_warm_pool(db, node.id, reason="config added")
    return config


@router.delete("/nodes/{node_id}/configs/{config_id}", status_code=204)
def delete_config(
    node_id: int,
    config_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Delete a VPN protocol config from a node.

    Refuses if any Device still points at this config — those devices
    would instantly break, and the admin should rotate them to another
    node first. To force-delete, disable the config instead (clients
    stop getting fresh URIs but existing ones keep working until the
    node itself is reprovisioned).
    """
    config = db.get(models.VPNConfig, config_id)
    if not config or config.node_id != node_id:
        raise HTTPException(status_code=404, detail="Config not found")
    # Only *live* devices block the delete. A revoked/disabled device
    # still has ``config_id`` set (we never null it out on unbind) but
    # it's terminal — the user is already off the node, so keeping the
    # VPNConfig around for its sake is pointless.
    device_count = (
        db.query(models.Device)
        .filter(
            models.Device.config_id == config.id,
            models.Device.status.notin_(
                [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
            ),
        )
        .count()
    )
    if device_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{device_count} device(s) still bound to this config. "
                "Rotate them to another node first, or disable the config."
            ),
        )
    # Hard-delete terminal devices + their credentials that still point
    # at this config. devices.config_id is NOT NULL so we can't just
    # null it; and the 409 gate above already guarantees everything
    # left here is revoked/disabled, so losing the rows is safe.
    # Also null out any orphan Credential rows whose FK is nullable —
    # no need to delete history, just unlink.
    dead_devices = (
        db.query(models.Device)
        .filter(models.Device.config_id == config.id)
        .all()
    )
    for dev in dead_devices:
        db.query(models.Credential).filter(
            models.Credential.device_id == dev.id
        ).delete(synchronize_session=False)
        db.delete(dev)
    db.query(models.Credential).filter(
        models.Credential.config_id == config.id
    ).update({models.Credential.config_id: None}, synchronize_session=False)
    db.flush()

    db.delete(config)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "config_deleted", "vpn_config", config_id, actor_type=actor_type)
    from .services import warm_pool
    warm_pool.invalidate_node_warm_pool(db, node_id, reason="config removed")
    return None


@router.get("/nodes/{node_id}/configs", response_model=list[schemas.VPNConfigOut])
def list_configs(node_id: int, db: Session = Depends(get_db), admin_token: str = Depends(require_admin)):
    configs = db.query(models.VPNConfig).filter(models.VPNConfig.node_id == node_id).all()
    return [schemas.VPNConfigOut.from_orm(cfg) for cfg in configs]


def _enrich_task_telegram(
    db: Session, tasks: list[models.ProvisioningTask]
) -> dict[int, str | None]:
    """Resolve owning telegram_id for each task in one batched pass.

    Walks device and subscription targets up to their user. Done as
    two bulk SELECTs (one per target type) rather than a per-row
    lookup, so the Tasks admin page stays cheap even with 200 rows.
    Node tasks return None (no owner concept).
    """
    device_ids = [t.target_id for t in tasks if t.target_type == "device"]
    sub_ids = [t.target_id for t in tasks if t.target_type == "subscription"]

    device_owner: dict[int, str | None] = {}
    if device_ids:
        rows = (
            db.query(models.Device.id, models.User.telegram_id)
            .join(models.Subscription, models.Device.subscription_id == models.Subscription.id)
            .join(models.User, models.Subscription.user_id == models.User.id)
            .filter(models.Device.id.in_(device_ids))
            .all()
        )
        device_owner = {row[0]: row[1] for row in rows}

    sub_owner: dict[int, str | None] = {}
    if sub_ids:
        rows = (
            db.query(models.Subscription.id, models.User.telegram_id)
            .join(models.User, models.Subscription.user_id == models.User.id)
            .filter(models.Subscription.id.in_(sub_ids))
            .all()
        )
        sub_owner = {row[0]: row[1] for row in rows}

    out: dict[int, str | None] = {}
    for t in tasks:
        if t.target_type == "device":
            out[t.id] = device_owner.get(t.target_id)
        elif t.target_type == "subscription":
            out[t.id] = sub_owner.get(t.target_id)
        else:
            out[t.id] = None
    return out


@router.get("/provisioning/tasks", response_model=list[schemas.ProvisioningTaskOut])
def list_tasks(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    target_type: str | None = None,
    telegram_id: str | None = Query(
        default=None,
        description="Filter tasks to ones owned by this telegram_id (via device→sub→user chain)",
    ),
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

    if telegram_id:
        # Narrow by telegram_id: collect the device/subscription ids
        # owned by that user, then constrain the task query to those.
        user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
        if user is None:
            return []
        sub_ids = [
            s.id for s in db.query(models.Subscription.id)
            .filter(models.Subscription.user_id == user.id).all()
        ]
        dev_ids: list[int] = []
        if sub_ids:
            dev_ids = [
                d.id for d in db.query(models.Device.id)
                .filter(models.Device.subscription_id.in_(sub_ids)).all()
            ]
        from sqlalchemy import or_, and_
        conds = []
        if sub_ids:
            conds.append(
                and_(
                    models.ProvisioningTask.target_type == "subscription",
                    models.ProvisioningTask.target_id.in_(sub_ids),
                )
            )
        if dev_ids:
            conds.append(
                and_(
                    models.ProvisioningTask.target_type == "device",
                    models.ProvisioningTask.target_id.in_(dev_ids),
                )
            )
        if not conds:
            return []
        query = query.filter(or_(*conds))

    tasks = (
        query.order_by(models.ProvisioningTask.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    tg_map = _enrich_task_telegram(db, tasks)
    out: list[schemas.ProvisioningTaskOut] = []
    for t in tasks:
        dto = schemas.ProvisioningTaskOut.from_orm(t)
        dto.telegram_id = tg_map.get(t.id)
        out.append(dto)
    return out


@router.delete("/provisioning/tasks/{task_id}", status_code=204)
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Delete a single ProvisioningTask row.

    Refuses ``running`` tasks — those are in-flight and the orchestrator
    still holds a reference to them. Terminal states (success, failed,
    pending) are free to remove; the history lives in AuditLog anyway.
    """
    task = db.get(models.ProvisioningTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status == models.ProvisioningTaskStatus.running:
        raise HTTPException(
            status_code=409,
            detail="Task is running; wait for it to finish before deleting",
        )
    db.delete(task)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "task_deleted", "provisioning_task", task_id, actor_type=actor_type)
    return None


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
    # Allow rerun on success too: "re-run bootstrap" is a legitimate
    # way to ship updated ansible roles to an already-live node
    # without a round trip through node delete + spawn. Refuse only
    # the still-in-flight states so we don't stomp on a running job.
    if task.status in {
        models.ProvisioningTaskStatus.running,
    }:
        raise HTTPException(
            status_code=409,
            detail="Task is currently running; wait for it to finish",
        )
    orchestrator = ProvisioningOrchestrator(db)
    orchestrator.reset_failed_task(task)
    orchestrator.run_task_async(task, node=None)
    db.refresh(task)
    return schemas.ProvisioningTaskOut.from_orm(task)


@router.post("/provisioning/tasks/batch")
def batch_tasks(
    body: dict,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Batch action on multiple provisioning tasks.

    Body: ``{ "ids": [1,2,3], "action": "delete" | "rerun" }``
    """
    ids = body.get("ids", [])
    action = body.get("action", "")
    if not ids or action not in ("delete", "rerun"):
        raise HTTPException(
            status_code=400,
            detail="ids (list) and action (delete|rerun) required",
        )

    actor, actor_type = _resolve_admin_actor(admin_actor)
    results: dict[str, list[int]] = {"ok": [], "skipped": [], "not_found": []}
    orchestrator = ProvisioningOrchestrator(db)

    for tid in ids:
        task = db.get(models.ProvisioningTask, tid)
        if not task:
            results["not_found"].append(tid)
            continue

        if action == "delete":
            if task.status == models.ProvisioningTaskStatus.running:
                results["skipped"].append(tid)
                continue
            db.delete(task)
            results["ok"].append(tid)

        elif action == "rerun":
            if task.status == models.ProvisioningTaskStatus.running:
                results["skipped"].append(tid)
                continue
            orchestrator.reset_failed_task(task)
            orchestrator.run_task_async(task, node=None)
            results["ok"].append(tid)

    db.commit()
    _audit(
        db, actor, f"tasks_batch_{action}", "provisioning_task", None,
        actor_type=actor_type,
        metadata={"ids": ids, "results": results},
    )
    return results


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
            plan_id=sub.plan_id,
            node=sub.node.name,
            region=sub.node.region,
            expires_at=sub.expires_at,
            status=sub.status.value,
            auto_renew=sub.auto_renew or False,
            sub_token=sub.sub_token,
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
            balance_kopecks=u.balance_kopecks or 0,
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


@router.get("/users/by_telegram/{telegram_id}/balance")
def get_balance_by_telegram(
    telegram_id: str,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Bot-side balance lookup (stage 4).

    Returns the user's current balance + per-active-sub days_remaining
    so the bot can render ``/balance`` without re-implementing the
    daily-cost math. Admin-token gated because the bot speaks
    server-to-server with the API token, not WebApp JWT.
    """
    from .services import balance as balance_svc

    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    subs = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.user_id == user.id,
            models.Subscription.status.in_(
                [
                    models.SubscriptionStatus.active,
                    models.SubscriptionStatus.frozen,
                ]
            ),
        )
        .all()
    )

    sub_summaries: list[dict] = []
    min_days: int | None = None
    for sub in subs:
        plan = sub.plan
        device_count = sum(
            1 for d in sub.devices
            if d.status not in (
                models.DeviceStatus.revoked, models.DeviceStatus.disabled,
            )
        )
        billable = max(device_count, 1)
        try:
            daily = balance_svc._daily_cost_kopecks(plan, billable) if plan else None
        except RuntimeError:
            daily = None
        days = (
            balance_svc.days_remaining(user, plan, billable)
            if plan and plan.daily_rate_kopecks
            else None
        )
        sub_summaries.append({
            "id": sub.id,
            "plan_name": plan.name if plan else "",
            "status": sub.status.value,
            "daily_cost_kopecks": daily,
            "days_remaining": days,
            "next_charge_at": sub.next_charge_at.isoformat() if sub.next_charge_at else None,
            "frozen_until": sub.frozen_until.isoformat() if sub.frozen_until else None,
        })
        if days is not None and sub.status == models.SubscriptionStatus.active:
            min_days = days if min_days is None else min(min_days, days)

    return {
        "user_id": user.id,
        "balance_kopecks": user.balance_kopecks or 0,
        "balance_rub": round((user.balance_kopecks or 0) / 100, 2),
        "min_days_remaining": min_days,
        "subscriptions": sub_summaries,
    }


class AdminTopupRequest(BaseModel):
    # Kopecks (integer), positive only. We route through balance_svc.topup
    # which enforces >0 — no negatives here. For debits/clawbacks there
    # should be a separate explicit endpoint, it's a much rarer operation
    # and we want the intent to be obvious in audit logs.
    amount_kopecks: int = Field(gt=0, le=10_000_000)
    note: str | None = Field(default=None, max_length=200)


class AdminTopupResponse(BaseModel):
    user_id: int
    telegram_id: str | None
    balance_kopecks: int
    tx_id: int


@router.post(
    "/users/by_telegram/{telegram_id}/topup",
    response_model=AdminTopupResponse,
)
def admin_topup_by_telegram(
    telegram_id: str,
    body: AdminTopupRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    actor_header: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Credit a user's balance from the admin panel.

    Intended for test accounts, comped friend accounts, compensating a
    failed payment we confirmed out-of-band, etc. Writes a
    ``kind=adjust`` ledger row with a unique ``admin_topup:<user>:<ts>``
    reference (so repeated clicks create distinct transactions — this is
    not an idempotent upsert, each click is a real new credit) and an
    audit log entry attributing the action to the admin token that made
    the call.
    """
    from .services import balance as balance_svc

    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ts = int(utcnow().timestamp())
    reference = f"admin_topup:{user.id}:{ts}"
    try:
        tx = balance_svc.topup(
            db,
            user.id,
            body.amount_kopecks,
            reference=reference,
            kind=models.BalanceTxKind.adjust,
            note=body.note or "admin topup",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor, actor_type = _resolve_admin_actor(actor_header)
    db.add(
        models.AuditLog(
            actor=actor,
            actor_type=actor_type,
            action="admin_topup",
            target_type="user",
            target_id=user.id,
            extra={
                "telegram_id": user.telegram_id,
                "amount_kopecks": body.amount_kopecks,
                "reference": reference,
                "note": body.note,
            },
        )
    )
    db.commit()
    db.refresh(user)
    return AdminTopupResponse(
        user_id=user.id,
        telegram_id=user.telegram_id,
        balance_kopecks=user.balance_kopecks or 0,
        tx_id=tx.id,
    )


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
    # NB: we do NOT refund ``sub.prepaid_kopecks`` on disable — the
    # bucket stays intact so an /enable call resumes billing from where
    # it left off. A separate /cancel action (refund + terminate) can
    # be added later; this keeps disable ↔ enable symmetric.
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "subscription_disabled", "subscription", subscription_id, actor_type=actor_type)
    return {
        "subscription_id": sub.id,
        "status": sub.status.value,
        "revocation_tasks": [task.id for task in tasks],
    }


@router.post("/subscriptions/{subscription_id}/devices", response_model=schemas.ProvisioningTaskOut)
def admin_add_device(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Admin counterpart to webapp add-device — no wallet/prepaid gate.

    The webapp endpoint guards on ``sub.prepaid_kopecks`` so a user
    can't silently over-provision past their paid runway. For admin
    we skip that check entirely: operators may need to re-bind a
    device that was revoked by mistake, or manually add a slot for a
    friend account. The resulting extra device still burns the
    prepaid bucket faster; the admin is expected to know that.
    """
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(
            status_code=400,
            detail=f"Subscription is {sub.status.value}, must be active",
        )

    current = (
        db.query(models.Device)
        .filter(
            models.Device.subscription_id == sub.id,
            models.Device.status.notin_(
                [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
            ),
        )
        .count()
    )
    new_count = current + 1
    orchestrator = ProvisioningOrchestrator(db)
    try:
        _device, task = orchestrator.reprovision_subscription(
            sub, device_name=f"device-{new_count}"
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "admin_device_added",
        "subscription",
        sub.id,
        actor_type=actor_type,
        metadata={"new_device_count": new_count},
    )
    db.refresh(task)
    return schemas.ProvisioningTaskOut.from_orm(task)


@router.post("/subscriptions/{subscription_id}/enable")
def enable_subscription(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Resume a non-active subscription.

    * ``frozen`` → routes through ``balance.unfreeze_subscription`` so
      the year-budget accounting runs and the original sub_token is
      preserved.
    * ``blocked`` / ``expired`` → flipped back to ``active`` and
      reprovisioned. Caller is responsible for having topped up the
      balance first — we don't gate on it, just resume.
    """
    from .services import balance as balance_svc

    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status == models.SubscriptionStatus.active:
        raise HTTPException(status_code=400, detail="Subscription is already active")

    if sub.status == models.SubscriptionStatus.frozen:
        try:
            balance_svc.unfreeze_subscription(db, sub, auto=False)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        sub.status = models.SubscriptionStatus.active
        sub.notes = None
        sub.next_charge_at = utcnow()
        db.add(sub)
        db.flush()
        orchestrator = ProvisioningOrchestrator(db)
        try:
            orchestrator.reprovision_subscription(sub)
        except Exception:
            logger.exception(
                "enable_subscription: reprovision failed sub=%s — left active w/o device",
                sub.id,
            )

    db.commit()
    db.refresh(sub)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "subscription_enabled",
        "subscription",
        subscription_id,
        actor_type=actor_type,
    )
    return {
        "subscription_id": sub.id,
        "status": sub.status.value,
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
    request: Request,
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


@router.post("/plans", response_model=schemas.PlanOut, status_code=201)
def create_plan(
    payload: schemas.PlanCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    existing = db.query(models.Plan).filter(models.Plan.name == payload.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Plan with this name already exists")
    plan = models.Plan(**payload.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return schemas.PlanOut.from_orm(plan)


@router.put("/plans/{plan_id}", response_model=schemas.PlanOut)
def update_plan(
    plan_id: int,
    payload: schemas.PlanUpdate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    plan = db.get(models.Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"] != plan.name:
        clash = db.query(models.Plan).filter(models.Plan.name == updates["name"]).first()
        if clash:
            raise HTTPException(status_code=409, detail="Plan with this name already exists")
    for field, value in updates.items():
        setattr(plan, field, value)
    db.commit()
    db.refresh(plan)
    return schemas.PlanOut.from_orm(plan)


@router.delete("/plans/{plan_id}", status_code=204)
def delete_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    plan = db.get(models.Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    # Refuse to delete a plan that still has subscriptions — soft-hide with
    # is_visible=false instead. This is cheaper than cascading and avoids
    # orphaning historical records.
    has_subs = (
        db.query(models.Subscription)
        .filter(models.Subscription.plan_id == plan_id)
        .first()
    )
    if has_subs:
        raise HTTPException(
            status_code=409,
            detail="Plan has existing subscriptions; set is_visible=false instead",
        )
    db.delete(plan)
    db.commit()
    return None


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

    # ── Stage 4: balance topup branch ───────────────────────────────
    # ``kind=topup`` means this invoice is just a wallet load — no
    # subscription provisioning, no plan to honor. Credit the user's
    # balance, mark the invoice paid, and return early. The legacy
    # path below still services ``kind=subscription`` invoices for
    # any in-flight purchases or admin-created plan invoices.
    if invoice.kind == "topup":
        from .services import balance as balance_svc

        amount_kopecks = int(round(float(invoice.amount) * 100))
        if amount_kopecks <= 0:
            raise HTTPException(status_code=400, detail="Topup invoice has non-positive amount")

        # Referrer payout: runs strictly BEFORE we write the user's own
        # topup row so "first kind=topup" detection is unambiguous. If
        # the user was attributed to a referrer (via /users/register)
        # and has never completed a real topup before, credit
        # REFERRAL_BONUS_KOPECKS to the referrer. Idempotent by
        # reference — a retried webhook can't double-pay.
        topup_user = db.get(models.User, invoice.user_id)
        if topup_user and topup_user.referred_by_id is not None:
            prior = (
                db.query(models.BalanceTransaction)
                .filter_by(
                    user_id=topup_user.id,
                    kind=models.BalanceTxKind.topup,
                )
                .first()
            )
            if prior is None:
                ref_key = f"referral_payout:{topup_user.id}"
                already = (
                    db.query(models.BalanceTransaction)
                    .filter_by(reference=ref_key)
                    .first()
                )
                if already is None:
                    try:
                        balance_svc.referral_bonus(
                            db,
                            topup_user.referred_by_id,
                            reference=ref_key,
                        )
                    except Exception:
                        # Don't fail the whole topup over a referral
                        # bonus write — log and move on. Payout will
                        # be retried by a nightly reconciliation if we
                        # ever add one; for now it's fire-and-forget.
                        logger.exception(
                            "referral payout failed for user=%s",
                            topup_user.id,
                        )

        try:
            balance_svc.topup(
                db,
                invoice.user_id,
                amount_kopecks,
                reference=f"invoice:{invoice.id}",
                kind=models.BalanceTxKind.topup,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to topup balance for invoice %s", invoice_id)
            raise HTTPException(status_code=500, detail="Failed to credit balance") from exc

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
            metadata={"kind": "topup", "amount_kopecks": amount_kopecks},
        )
        return _invoice_with_credentials(invoice, [])

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


@router.post("/invoices/{invoice_id}/cancel", response_model=schemas.InvoiceOut)
def cancel_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Mark a stale pending invoice as failed (cancelled).

    Only works on pending invoices — paid ones should be mark_unpaid'd
    first if you truly need to void them.
    """
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status != models.InvoiceStatus.pending:
        raise HTTPException(
            status_code=400,
            detail=f"Can only cancel pending invoices, this one is {invoice.status.value}",
        )
    invoice.status = models.InvoiceStatus.failed
    db.commit()
    db.refresh(invoice)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "invoice_cancelled", "invoice", invoice.id, actor_type=actor_type)
    return schemas.InvoiceOut.from_orm(invoice)


@router.post("/invoices/batch")
def batch_invoices(
    body: dict,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Batch action on multiple invoices.

    Body: ``{ "ids": [1,2,3], "action": "cancel" | "mark_paid" | "mark_unpaid" }``
    """
    ids = body.get("ids", [])
    action = body.get("action", "")
    if not ids or action not in ("cancel", "mark_paid", "mark_unpaid"):
        raise HTTPException(status_code=400, detail="ids (list) and action (cancel|mark_paid|mark_unpaid) required")

    actor, actor_type = _resolve_admin_actor(admin_actor)
    results: dict[str, list[int]] = {"ok": [], "skipped": [], "not_found": []}

    for inv_id in ids:
        invoice = db.get(models.Invoice, inv_id)
        if not invoice:
            results["not_found"].append(inv_id)
            continue

        if action == "cancel":
            if invoice.status != models.InvoiceStatus.pending:
                results["skipped"].append(inv_id)
                continue
            invoice.status = models.InvoiceStatus.failed
            _audit(db, actor, "invoice_cancelled", "invoice", inv_id, actor_type=actor_type)

        elif action == "mark_paid":
            if invoice.status != models.InvoiceStatus.pending:
                results["skipped"].append(inv_id)
                continue
            try:
                _mark_invoice_paid_core(db, inv_id, actor=actor, actor_type=actor_type)
            except HTTPException:
                results["skipped"].append(inv_id)
                continue

        elif action == "mark_unpaid":
            if invoice.status == models.InvoiceStatus.pending:
                results["skipped"].append(inv_id)
                continue
            invoice.status = models.InvoiceStatus.pending
            _audit(db, actor, "invoice_marked_unpaid", "invoice", inv_id, actor_type=actor_type)

        results["ok"].append(inv_id)

    db.commit()
    return results


@router.post("/invoices/{invoice_id}/mark_unpaid", response_model=schemas.InvoiceOut)
def mark_invoice_unpaid(
    invoice_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Revert a mistakenly marked invoice back to pending.

    Does NOT touch the provisioned subscription/devices — if you also need
    to revoke access, do that separately. This is a bookkeeping fix.
    """
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == models.InvoiceStatus.pending:
        return schemas.InvoiceOut.from_orm(invoice)
    invoice.status = models.InvoiceStatus.pending
    db.commit()
    db.refresh(invoice)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "invoice_marked_unpaid", "invoice", invoice.id, actor_type=actor_type)
    return schemas.InvoiceOut.from_orm(invoice)


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
    result = migrate_subscriptions_off(db, node, reason="manual migration")
    migrated = result["subscription_ids"]
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_migrated",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={
            "migrated_count": len(migrated),
            "device_task_ids": result["device_task_ids"],
            "resync_task_ids": result["resync_task_ids"],
        },
    )
    # Combined task id list — admin UI polls /api/provisioning/tasks
    # and filters by these to render a grouped progress banner.
    # Revokes go first (old node, best-effort), then device applies
    # (new node), then resync mops up any drift.
    task_ids = (
        result["revoke_task_ids"]
        + result["device_task_ids"]
        + result["resync_task_ids"]
    )
    return {
        "node_id": node.id,
        "migrated_subscriptions": migrated,
        "task_ids": task_ids,
        "revoke_task_ids": result["revoke_task_ids"],
        "device_task_ids": result["device_task_ids"],
        "resync_task_ids": result["resync_task_ids"],
    }


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
            description=f"Order #{invoice.id}",
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
