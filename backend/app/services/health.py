"""Node health aggregation and auto-migration.

Two sides:

* **Ingestion** — probers (active checks from RU / KZ / EU rigs, or passive
  reports from clients) POST probe results to
  ``POST /api/nodes/{id}/probes``. The route delegates to
  :func:`record_probe`, which stores a :class:`HealthProbe` row and refreshes
  aggregated health on the node.

* **Reaction** — :func:`recompute_node_health` recalculates ``health_score``
  and ``blocked_regions`` from the most recent probes and, when a node is
  considered dead in a region, calls :func:`migrate_subscriptions_off` to
  move affected subscriptions to a healthy node and notify the users.

The "healthy node" selector reuses :func:`services.provisioning.choose_node`
so capacity, cooldown and pool constraints are respected identically to the
purchase flow.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from ..time_utils import utcnow
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models

logger = logging.getLogger(__name__)

# How far back we look when computing aggregate health.
LOOKBACK = timedelta(minutes=15)
# Region is "blocked" if success rate from that region drops below this.
BLOCK_THRESHOLD = 0.3
# Node is unhealthy overall if average success rate across all regions is
# below this — triggers migration even if blocks are not region-specific.
DEAD_THRESHOLD = 0.2
# Cooldown when a node is auto-disabled after a block.
DEFAULT_COOLDOWN = timedelta(days=3)


def record_probe(
    db: Session,
    *,
    node: models.VPNNode,
    source_region: str,
    result: models.ProbeResult,
    latency_ms: int | None = None,
    source_kind: str | None = None,
    details: dict | None = None,
) -> models.HealthProbe:
    probe = models.HealthProbe(
        node_id=node.id,
        source_region=source_region,
        source_kind=source_kind,
        result=result,
        latency_ms=latency_ms,
        details=details,
    )
    db.add(probe)
    db.flush()
    return probe


# Minimum probe count required before we trust an aggregate result.
# With only a handful of samples a single failed probe blows the success
# rate under DEAD_THRESHOLD and triggers a bogus migration. The gate is
# applied both globally and per-region.
MIN_SAMPLES = 5


def _aggregate(
    db: Session, node_id: int, since: datetime
) -> tuple[float, dict[str, float], int, dict[str, int]]:
    """Return (overall_success_rate, per_region_success_rate, total_samples, per_region_samples)."""
    rows = (
        db.query(
            models.HealthProbe.source_region,
            models.HealthProbe.result,
            func.count(models.HealthProbe.id),
        )
        .filter(
            models.HealthProbe.node_id == node_id,
            models.HealthProbe.observed_at >= since,
        )
        .group_by(models.HealthProbe.source_region, models.HealthProbe.result)
        .all()
    )

    per_region_total: dict[str, int] = defaultdict(int)
    per_region_ok: dict[str, int] = defaultdict(int)
    total = 0
    ok_total = 0
    for region, result, count in rows:
        per_region_total[region] += count
        total += count
        if result == models.ProbeResult.ok:
            per_region_ok[region] += count
            ok_total += count

    per_region = {
        region: (per_region_ok[region] / per_region_total[region])
        for region in per_region_total
        if per_region_total[region] > 0
    }
    overall = (ok_total / total) if total else 1.0
    return overall, per_region, total, dict(per_region_total)


def recompute_node_health(
    db: Session,
    node: models.VPNNode,
    *,
    auto_migrate: bool = True,
) -> dict:
    """Refresh ``health_score``/``blocked_regions`` and optionally migrate.

    Returns a small dict with the computed numbers — useful for API output.
    """
    since = utcnow() - LOOKBACK
    overall, per_region, total_samples, per_region_samples = _aggregate(
        db, node.id, since
    )

    # Only consider regions with enough samples for a block decision —
    # a single failed probe should not flag an entire region as blocked.
    blocked = sorted(
        region
        for region, rate in per_region.items()
        if rate < BLOCK_THRESHOLD and per_region_samples.get(region, 0) >= MIN_SAMPLES
    )
    node.blocked_regions = blocked or None
    node.health_score = int(round(overall * 100))
    node.last_health_check_at = utcnow()

    migrated: list[int] = []
    # Same gate as per-region: need enough total samples before we'll
    # pronounce the node dead. Without this, a node that just came up
    # gets killed by the first probe if it fails.
    global_death = overall < DEAD_THRESHOLD and total_samples >= MIN_SAMPLES
    if auto_migrate and (global_death or blocked):
        logger.warning(
            "Node %s degraded: health=%.2f blocked_regions=%s — considering migration",
            node.name,
            overall,
            blocked,
        )
        if global_death:
            node.status = models.VPNNodeStatus.error
            node.is_active = False
            node.cooldown_until = utcnow() + DEFAULT_COOLDOWN
            migrated = migrate_subscriptions_off(db, node, reason="node unreachable")[
                "subscription_ids"
            ]

    db.add(node)
    db.commit()
    return {
        "node_id": node.id,
        "health_score": node.health_score,
        "blocked_regions": blocked,
        "overall_success_rate": overall,
        "per_region": per_region,
        "migrated_subscriptions": migrated,
    }


def migrate_subscriptions_off(
    db: Session, node: models.VPNNode, *, reason: str
) -> dict:
    """Move all active subscriptions off ``node`` to a healthy alternative.

    Returns a dict with the migrated subscription IDs, the per-target
    resync task IDs, and the per-device provisioning task IDs. Callers
    that only care about the migrated count (legacy health-monitor
    path) can read ``["subscription_ids"]``; the admin migrate route
    surfaces ``task_ids`` so the UI can render a grouped progress
    banner for the batch.
    """
    from .provisioning import (
        ProvisioningOrchestrator,
        _node_has_vless_family,
        choose_node,
    )

    subs: Iterable[models.Subscription] = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.node_id == node.id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .all()
    )
    migrated_ids: list[int] = []
    orchestrator = ProvisioningOrchestrator(db)
    # Unique target nodes this batch migrated to — we run a single
    # resync per target at the end. Per-sub resync would be O(subs²)
    # and redundant: manage_vless_user.sh is idempotent and the resync
    # reads the full active-subs list from the DB, not the per-sub
    # delta, so one call after the whole batch is exactly enough.
    # Also tracks the provisioning task IDs we create so the admin UI
    # can show a progress banner grouped by this migration run.
    resync_targets: dict[int, models.VPNNode] = {}
    device_task_ids: list[int] = []
    revoke_task_ids: list[int] = []

    for sub in subs:
        try:
            target = choose_node(db, sub.plan, exclude_node_ids=[node.id])
        except RuntimeError as exc:
            logger.error("No healthy node available for sub %s: %s", sub.id, exc)
            continue

        # Revoke the old devices on the (possibly already-dead) node. Best
        # effort — if the node is unreachable Ansible will fail, that's okay.
        for device in list(sub.devices):
            try:
                revoke_task = orchestrator.revoke_device(device, reason=reason, background=True)
                revoke_task_ids.append(revoke_task.id)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to revoke device %s during migration", device.id)

        # In-place migration: flip node_id on the existing Subscription row
        # and reprovision. This preserves sub_token (dynamic sub-link keeps
        # working) and avoids the "two cards in webapp" UX bug where the
        # old blocked row and new active row both showed up.
        sub.node_id = target.id
        sub.notes = f"migrated: {reason}"
        db.add(sub)
        db.commit()
        db.refresh(sub)

        try:
            _device, _task = orchestrator.reprovision_subscription(sub)
            migrated_ids.append(sub.id)
            resync_targets[target.id] = target
            if _task is not None:
                device_task_ids.append(_task.id)
            new_sub = sub
            # Notify the user. The bot polls /notifications/pending for
            # audit_log rows with action="migration_notice" and delivers
            # them as Telegram messages (see api_extensions.py). Without
            # telegram_id in extra the poller silently drops the row, so
            # skip the write for non-Telegram users (e.g. email-only).
            if sub.user and sub.user.telegram_id:
                db.add(
                    models.AuditLog(
                        actor="health_monitor",
                        actor_type=models.AuditActor.system,
                        action="migration_notice",
                        target_type="subscription",
                        target_id=new_sub.id,
                        extra={
                            "telegram_id": sub.user.telegram_id,
                            "old_node": node.name,
                            "new_node": target.name,
                            "reason": reason,
                        },
                    )
                )
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to re-provision sub %s on node %s", sub.id, target.id)

    # Resync every target node the batch touched. This covers the
    # "user migrated but still gets `invalid request user id`" case:
    # the per-device apply task *does* add the user, but if the target
    # node's config.json is in a degraded state (partial wipe, stale
    # cache, race with another provisioning task on the same node) the
    # resync re-pushes the full authoritative client list via
    # manage_vless_*_user.sh. Covers every vless-family protocol
    # (reality / xhttp / ws_cdn) in one shot. Idempotent, so re-running
    # after the individual applies is safe and costs one extra ansible
    # run per target node — cheap vs. hunting down flakiness.
    resync_task_ids: list[int] = []
    for target_node in resync_targets.values():
        try:
            if not _node_has_vless_family(target_node):
                continue
            resync_task = orchestrator.resync_node_clients(target_node)
            if resync_task is not None:
                resync_task_ids.append(resync_task.id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Post-migration resync failed for target node %s", target_node.id
            )

    return {
        "subscription_ids": migrated_ids,
        "revoke_task_ids": revoke_task_ids,
        "device_task_ids": device_task_ids,
        "resync_task_ids": resync_task_ids,
    }
