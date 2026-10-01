"""Traffic accounting endpoints.

Two routes that share a helper:

* ``POST /api/subscriptions/{id}/traffic`` — admin-facing one-off
  increment (used by tests and manual adjustments).
* ``POST /api/nodes/{id}/traffic`` — node-side batch ingest from the
  traffic collector.

Both funnel through ``_apply_traffic_delta`` so the over-limit
block+revoke path is identical on both ends.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import (
    SCOPE_TRAFFIC_WRITE,
    AuthPrincipal,
    require_admin,
    require_scope,
)
from ..services.provisioning import ProvisioningOrchestrator
from ..time_utils import utcnow
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


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
