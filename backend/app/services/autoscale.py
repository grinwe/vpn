"""Pool-level autoscaling for VPN nodes.

The scaler walks every :class:`ServerPool` with ``autoscale_enabled=True``
and decides whether a new node should be created. A pool is considered
saturated when::

    active_subscriptions / sum(max_users of healthy nodes) >= high_watermark

If that holds and the pool has fewer than ``autoscale_max_nodes`` nodes, we
call :func:`services.node_spawner.spawn_node` with the pool's default
provider/region/plan. Nodes that are in cooldown or unhealthy are excluded
from the denominator — they would not be picked by :func:`choose_node`
anyway, so counting them would hide real saturation.

We intentionally do not auto-destroy nodes here. Shrinking a live pool is
risky (active users get migrated around) and not worth the complexity
before real operational data shows it's needed.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..time_utils import utcnow
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from .. import models
from .node_spawner import NodeSpawnError, spawn_node

logger = logging.getLogger(__name__)

DEFAULT_HIGH_WATERMARK = float(os.getenv("AUTOSCALE_HIGH_WATERMARK", "0.8"))
DEFAULT_MAX_NODES = int(os.getenv("AUTOSCALE_MAX_NODES", "10"))
# Without explicit per-node capacity we fall back to this so that brand-new
# pools still have a sane denominator.
FALLBACK_NODE_CAPACITY = int(os.getenv("AUTOSCALE_FALLBACK_CAPACITY", "50"))
# After a spawn failure we park the pool for this many seconds so we don't
# hammer the provider API (or our own wallet) in a tight loop. In-memory
# only — on process restart we'll retry immediately, which is what we want.
SPAWN_BACKOFF_SECONDS = int(os.getenv("AUTOSCALE_SPAWN_BACKOFF", "600"))
_spawn_backoff: dict[int, datetime] = {}


@dataclass
class PoolDecision:
    pool_id: int
    pool_name: str
    utilization: float
    total_capacity: int
    active_subs: int
    node_count: int
    scaled_up: bool
    new_node_id: int | None = None
    reason: str | None = None


def _pool_high_watermark(pool: models.ServerPool) -> float:
    if pool.autoscale_high_watermark is not None:
        value = pool.autoscale_high_watermark
        return float(value) if not isinstance(value, Decimal) else float(value)
    return DEFAULT_HIGH_WATERMARK


def _pool_max_nodes(pool: models.ServerPool) -> int:
    return pool.autoscale_max_nodes or DEFAULT_MAX_NODES


def _eligible_nodes(pool: models.ServerPool) -> list[models.VPNNode]:
    """Return nodes that `choose_node` would consider usable right now."""
    now = utcnow()
    from .provisioning import MIN_HEALTHY_SCORE

    out: list[models.VPNNode] = []
    for node in pool.nodes:
        if not node.is_active:
            continue
        if node.status not in (
            models.VPNNodeStatus.active,
            models.VPNNodeStatus.registering,
        ):
            continue
        if node.cooldown_until and node.cooldown_until > now:
            continue
        if node.health_score is not None and node.health_score < MIN_HEALTHY_SCORE:
            continue
        out.append(node)
    return out


def _node_capacity(node: models.VPNNode) -> int:
    return node.max_users if node.max_users is not None else FALLBACK_NODE_CAPACITY


def _active_subs_on_nodes(db: Session, nodes: Iterable[models.VPNNode]) -> int:
    ids = [n.id for n in nodes]
    if not ids:
        return 0
    return (
        db.query(models.Subscription)
        .filter(
            models.Subscription.node_id.in_(ids),
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .count()
    )


def evaluate_pool(db: Session, pool: models.ServerPool) -> PoolDecision:
    if not pool.autoscale_enabled:
        return PoolDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=0.0,
            total_capacity=0,
            active_subs=0,
            node_count=len(pool.nodes),
            scaled_up=False,
            reason="autoscale disabled",
        )

    if not pool.autoscale_provider_id or not pool.autoscale_region or not pool.autoscale_plan:
        return PoolDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=0.0,
            total_capacity=0,
            active_subs=0,
            node_count=len(pool.nodes),
            scaled_up=False,
            reason="autoscale misconfigured (provider/region/plan missing)",
        )

    eligible = _eligible_nodes(pool)
    capacity = sum(_node_capacity(n) for n in eligible)
    active = _active_subs_on_nodes(db, eligible)
    utilization = (active / capacity) if capacity else 1.0

    # Exclude nodes that died during spawn or are otherwise broken from the
    # "we already have N nodes" count — otherwise a single failed spawn can
    # wedge the pool at its cap forever with no healthy nodes.
    counted_nodes = [
        n for n in pool.nodes if n.status != models.VPNNodeStatus.error
    ]

    backoff_until = _spawn_backoff.get(pool.id)
    if backoff_until and backoff_until > utcnow():
        return PoolDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            total_capacity=capacity,
            active_subs=active,
            node_count=len(counted_nodes),
            scaled_up=False,
            reason=f"spawn backoff until {backoff_until.isoformat()}",
        )

    max_nodes = _pool_max_nodes(pool)
    if len(counted_nodes) >= max_nodes:
        return PoolDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            total_capacity=capacity,
            active_subs=active,
            node_count=len(counted_nodes),
            scaled_up=False,
            reason=f"max_nodes={max_nodes} reached",
        )

    watermark = _pool_high_watermark(pool)
    if utilization < watermark:
        return PoolDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            total_capacity=capacity,
            active_subs=active,
            node_count=len(pool.nodes),
            scaled_up=False,
            reason=f"utilization {utilization:.2f} < watermark {watermark:.2f}",
        )

    name = f"{pool.name}-auto-{int(utcnow().timestamp())}"
    logger.warning(
        "Autoscale: pool %s saturated (%.2f), spawning node %s via provider %s",
        pool.name,
        utilization,
        name,
        pool.autoscale_provider_id,
    )
    try:
        node, _task = spawn_node(
            db,
            provider_id=pool.autoscale_provider_id,
            name=name,
            region=pool.autoscale_region,
            plan=pool.autoscale_plan,
            image=pool.autoscale_image,
            pool_id=pool.id,
            notes=f"auto-scaled at {utcnow().isoformat()}",
        )
    except NodeSpawnError as exc:
        logger.exception("Autoscale spawn failed for pool %s", pool.name)
        _spawn_backoff[pool.id] = utcnow() + timedelta(seconds=SPAWN_BACKOFF_SECONDS)
        return PoolDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            total_capacity=capacity,
            active_subs=active,
            node_count=len(counted_nodes),
            scaled_up=False,
            reason=f"spawn failed: {exc}",
        )

    _spawn_backoff.pop(pool.id, None)

    return PoolDecision(
        pool_id=pool.id,
        pool_name=pool.name,
        utilization=utilization,
        total_capacity=capacity,
        active_subs=active,
        node_count=len(pool.nodes) + 1,
        scaled_up=True,
        new_node_id=node.id,
        reason="scaled up",
    )


def evaluate_all_pools(db: Session) -> list[PoolDecision]:
    pools = db.query(models.ServerPool).all()
    return [evaluate_pool(db, p) for p in pools]
