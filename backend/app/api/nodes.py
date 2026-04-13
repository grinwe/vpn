"""VPN node admin endpoints: ``/api/nodes/*``.

Covers the CRUD surface for VPN nodes plus all the operations layered
on top of them: bootstrap/rerun, diagnose, resync, active toggle,
configs, spawn/destroy, health lookup and force-migrate. The probe
ingest and traffic endpoints that also live under ``/nodes/{id}/...``
are factored into ``probes.py`` and ``traffic.py`` respectively —
FastAPI stitches them back together when the master router includes
both modules.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..services.ansible_runner import (
    InvalidNodeIdentity,
    validate_node_identity_fields,
    validate_node_name,
)
from ..services.health import recompute_node_health
from ..services.node_spawner import NodeSpawnError, destroy_node, spawn_node
from ..services.provisioning import ProvisioningOrchestrator
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


@router.post("/nodes", response_model=schemas.VPNNodeOut)
def create_node(
    payload: schemas.VPNNodeCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    # #55 — whitelist the three fields that end up in a dynamically
    # rendered ansible inventory (name/host/ssh_port). Defence-in-depth:
    # the service layer re-validates at render time, but catching it here
    # gives the operator a 400 with a clear reason instead of a bootstrap
    # task that crashes later.
    try:
        validate_node_identity_fields(payload.name, payload.host, payload.ssh_port)
    except InvalidNodeIdentity as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    """Force re-push of all vless-family users onto a node.

    Safety net for the "empty clients after site.yml" class of bugs:
    if an operator suspects the node has drifted from the backend's
    view of who's provisioned (e.g. after a manual config edit, a
    restore-from-backup, or a half-broken bootstrap), this endpoint
    enqueues a resync task that re-adds every active credential via
    manage_vless_*_user.sh. The helper is idempotent so running this
    in any node state is safe.

    Covers vless_reality, vless_xhttp and vless_ws_cdn — anything
    that shares the "invalid request user id" failure mode — and
    also pushes warm-pool bundles that are bound to the node but
    not yet assigned to a subscription.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.resync_node_clients(node)
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
    payload = task.payload or {}
    clients_by_proto = payload.get("clients_by_proto") or {}
    total = sum(len(v) for v in clients_by_proto.values()) if clients_by_proto else len(
        payload.get("clients", [])
    )
    return {
        "node_id": node.id,
        "task_id": task.id,
        "clients": total,
        "clients_by_proto": {
            proto: len(v) for proto, v in clients_by_proto.items()
        },
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


@router.post("/nodes/{node_id}/diagnose")
def diagnose_node(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Run check_node_health against the node without touching configs."""
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task("node", node.id, "diagnose", {})
    db.commit()
    orchestrator.run_task_async(task, node=node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_diagnose",
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
        from ..services.node_spawner import ensure_reality_config
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
        from ..services.node_spawner import ensure_shadowtls_config
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
    from ..services import warm_pool
    warm_pool.invalidate_node_warm_pool(db, node.id, reason="config added")
    # Run site.yml so Ansible installs the new protocol on the node.
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task(
        "node", node.id, "bootstrap",
        {"pool_id": node.pool_id, "config_change": True},
    )
    db.commit()
    orchestrator.run_task_async(task, node=node)
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
    from ..services import warm_pool
    warm_pool.invalidate_node_warm_pool(db, node_id, reason="config removed")
    # Re-run site.yml so Ansible stops/removes the deleted protocol's service.
    node = db.get(models.VPNNode, node_id)
    if node:
        orchestrator = ProvisioningOrchestrator(db)
        task = orchestrator.create_task(
            "node", node.id, "bootstrap",
            {"pool_id": node.pool_id, "config_change": True},
        )
        db.commit()
        orchestrator.run_task_async(task, node=node)
    return None


@router.get("/nodes/{node_id}/configs", response_model=list[schemas.VPNConfigOut])
def list_configs(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    configs = db.query(models.VPNConfig).filter(models.VPNConfig.node_id == node_id).all()
    return [schemas.VPNConfigOut.from_orm(cfg) for cfg in configs]


# ---------------------------------------------------------------------------
# Cloud-spawned nodes
# ---------------------------------------------------------------------------


@router.post("/nodes/spawn", response_model=schemas.VPNNodeOut)
def spawn_node_route(
    payload: schemas.NodeSpawnRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    # #55 — same DNS-safe whitelist as /nodes POST. Only ``name`` can be
    # checked at this point; the host is resolved later by the cloud
    # driver and re-validated inside spawn_node() itself before the
    # VPNNode row is written.
    try:
        validate_node_name(payload.name)
    except InvalidNodeIdentity as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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


@router.delete("/nodes/{node_id}", status_code=200)
def delete_node(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Remove a node from the database.

    Refuses if the node still has active/frozen subscriptions — migrate
    them first. For cloud-provisioned nodes use POST /destroy instead.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    active_subs = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.node_id == node.id,
            models.Subscription.status.in_([
                models.SubscriptionStatus.active,
                models.SubscriptionStatus.frozen,
            ]),
        )
        .count()
    )
    if active_subs > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Node has {active_subs} active subscription(s). Migrate them first.",
        )

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "node_deleted", "vpn_node", node.id, actor_type=actor_type)
    db.delete(node)
    db.commit()
    return {"node_id": node_id, "deleted": True}


# ---------------------------------------------------------------------------
# Health + migration
# ---------------------------------------------------------------------------


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
    from ..services.health import migrate_subscriptions_off

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
