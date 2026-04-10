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

Stage 5 adds the symmetric **downscale** path: when a pool's utilization
drops below ``low_watermark`` and we still have more than ``min_nodes``
eligible nodes, the youngest auto-spawned node is flipped to
``draining``. The drain tick (``run_drain_tick`` in worker.py) then walks
its active subscriptions, migrates each to a different node in the same
pool's plan, and finally calls ``destroy_node`` once the live-sub count
hits zero AND the grace window has elapsed. Hysteresis (low << high)
plus the per-pool 1-draining-node cap keep the algorithm from flapping.
The whole downscale path is gated by ``AUTOSCALE_DOWNSCALE_ENABLED`` —
default 0, opt-in per environment.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..time_utils import utcnow
from decimal import Decimal
from typing import Iterable

from prometheus_client import Counter, Gauge
from sqlalchemy.orm import Session

from .. import models
from .node_spawner import NodeSpawnError, spawn_node

logger = logging.getLogger(__name__)

# ── Metrics ──────────────────────────────────────────────────────────
# Operators need to see why autoscale did or did not act. Three views:
#   * utilization gauge — current load per pool, lets us tune watermark
#   * eligible nodes gauge — confirms unhealthy nodes really get excluded
#   * scale event counter labelled by outcome — separates spawned vs.
#     blocked-by-cap vs. spawn-failed vs. backoff vs. quiet ticks

POOL_UTILIZATION = Gauge(
    "vpn_autoscale_pool_utilization",
    "active_subs / sum(max_users of healthy nodes) per pool, last tick",
    ["pool"],
)
POOL_ELIGIBLE_NODES = Gauge(
    "vpn_autoscale_pool_eligible_nodes",
    "Count of healthy non-cooldown nodes per pool, last tick",
    ["pool"],
)
SCALE_EVENTS = Counter(
    "vpn_autoscale_events_total",
    "Autoscale tick outcomes per pool",
    ["pool", "outcome"],
)
# Downscale outcomes are tracked under a separate counter so dashboards
# can plot growth and shrinkage independently. Outcomes:
#   * disabled / misconfigured / above_low_watermark / min_nodes_floor
#   * already_draining (we only mark one node per pool at a time)
#   * marked_draining (just flipped status)
#   * no_drain_target (pool has no auto-spawned nodes to shrink)
DRAIN_EVENTS = Counter(
    "vpn_autoscale_drain_events_total",
    "Downscale tick outcomes per pool",
    ["pool", "outcome"],
)
# Per-node remaining-subs gauge — drives the "drain progress" panel.
# Stays at 0 for nodes that aren't draining (we still emit so a stale
# series doesn't pin Grafana to a dead node).
DRAIN_SUBS_REMAINING = Gauge(
    "vpn_autoscale_drain_subs_remaining",
    "Active subscriptions still on a draining node",
    ["pool", "node"],
)

DEFAULT_HIGH_WATERMARK = float(os.getenv("AUTOSCALE_HIGH_WATERMARK", "0.8"))
DEFAULT_LOW_WATERMARK = float(os.getenv("AUTOSCALE_LOW_WATERMARK", "0.3"))
DEFAULT_MAX_NODES = int(os.getenv("AUTOSCALE_MAX_NODES", "10"))
DEFAULT_MIN_NODES = int(os.getenv("AUTOSCALE_MIN_NODES", "1"))
# Master switch for the downscale path. Off by default — operators
# enable it once after watching the metrics for a cycle. Set to "1"
# (or true/yes) to let the drain tick actually flip nodes to draining
# and destroy emptied ones.
DOWNSCALE_ENABLED = os.getenv("AUTOSCALE_DOWNSCALE_ENABLED", "0").lower() not in {
    "0",
    "false",
    "no",
    "",
}
# Grace window between "node has zero active subs" and "destroy_node".
# Buys time for in-flight provisioning tasks to settle and for any
# unfreezes to discover the node before it disappears.
DRAIN_GRACE_HOURS = int(os.getenv("AUTOSCALE_DRAIN_GRACE_HOURS", "24"))
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


def _pool_low_watermark(pool: models.ServerPool) -> float:
    if pool.autoscale_low_watermark is not None:
        value = pool.autoscale_low_watermark
        return float(value) if not isinstance(value, Decimal) else float(value)
    return DEFAULT_LOW_WATERMARK


def _pool_max_nodes(pool: models.ServerPool) -> int:
    return pool.autoscale_max_nodes or DEFAULT_MAX_NODES


def _pool_min_nodes(pool: models.ServerPool) -> int:
    return pool.autoscale_min_nodes or DEFAULT_MIN_NODES


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
    """Count of **active devices** on the given nodes.

    Stage 7: capacity bookkeeping is per-device, not per-subscription —
    a Family sub with 3 devices occupies 3 slots in the ``max_users``
    math, otherwise the autoscaler under-provisions once users start
    adding extra devices to their plans. The function name is
    historical; the return value is now ``COUNT(devices)``.
    """
    ids = [n.id for n in nodes]
    if not ids:
        return 0
    return (
        db.query(models.Device)
        .join(models.Subscription, models.Subscription.id == models.Device.subscription_id)
        .filter(
            models.Subscription.node_id.in_(ids),
            models.Subscription.status == models.SubscriptionStatus.active,
            models.Device.status.notin_(
                [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
            ),
        )
        .count()
    )


def _record(pool_name: str, outcome: str, utilization: float, eligible: int) -> None:
    """Single point that touches Prometheus so every return path stays consistent."""
    POOL_UTILIZATION.labels(pool=pool_name).set(utilization)
    POOL_ELIGIBLE_NODES.labels(pool=pool_name).set(eligible)
    SCALE_EVENTS.labels(pool=pool_name, outcome=outcome).inc()


def evaluate_pool(db: Session, pool: models.ServerPool) -> PoolDecision:
    if not pool.autoscale_enabled:
        _record(pool.name, "disabled", 0.0, 0)
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
        _record(pool.name, "misconfigured", 0.0, 0)
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
        _record(pool.name, "backoff", utilization, len(eligible))
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
        _record(pool.name, "max_nodes_reached", utilization, len(eligible))
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
        _record(pool.name, "below_watermark", utilization, len(eligible))
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
    # Stage 6 — walk the provider chain. Primary first, then any
    # configured fallbacks. Each entry is just a CloudProvider id;
    # spawn_node already validates is_active and existence, so we just
    # collect errors and try the next one.
    provider_chain: list[int] = [pool.autoscale_provider_id]
    fallbacks = pool.autoscale_fallback_provider_ids or []
    if isinstance(fallbacks, list):
        for pid in fallbacks:
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                continue
            if pid_int and pid_int not in provider_chain:
                provider_chain.append(pid_int)

    last_exc: NodeSpawnError | None = None
    node = None
    for idx, provider_id in enumerate(provider_chain):
        attempt_name = name if idx == 0 else f"{name}-fb{idx}"
        logger.warning(
            "Autoscale: pool %s saturated (%.2f), spawning %s via provider %s "
            "(chain pos %d/%d)",
            pool.name,
            utilization,
            attempt_name,
            provider_id,
            idx + 1,
            len(provider_chain),
        )
        try:
            node, _task = spawn_node(
                db,
                provider_id=provider_id,
                name=attempt_name,
                region=pool.autoscale_region,
                plan=pool.autoscale_plan,
                image=pool.autoscale_image,
                pool_id=pool.id,
                notes=f"auto-scaled at {utcnow().isoformat()} via provider={provider_id}",
            )
            break
        except NodeSpawnError as exc:
            last_exc = exc
            logger.warning(
                "Autoscale: provider %s failed for pool %s: %s — trying next in chain",
                provider_id,
                pool.name,
                exc,
            )
            SCALE_EVENTS.labels(pool=pool.name, outcome="provider_failed").inc()
            continue

    if node is None:
        logger.exception(
            "Autoscale: all %d providers in chain failed for pool %s",
            len(provider_chain),
            pool.name,
            exc_info=last_exc,
        )
        _spawn_backoff[pool.id] = utcnow() + timedelta(seconds=SPAWN_BACKOFF_SECONDS)
        _record(pool.name, "spawn_failed", utilization, len(eligible))
        return PoolDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            total_capacity=capacity,
            active_subs=active,
            node_count=len(counted_nodes),
            scaled_up=False,
            reason=f"all {len(provider_chain)} providers failed; last: {last_exc}",
        )

    _spawn_backoff.pop(pool.id, None)
    _record(pool.name, "scaled_up", utilization, len(eligible))

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


# ── Downscale ────────────────────────────────────────────────────────


@dataclass
class DrainDecision:
    pool_id: int
    pool_name: str
    utilization: float
    eligible_nodes: int
    already_draining: int
    marked_node_id: int | None
    reason: str


def _record_drain(
    pool_name: str, outcome: str, utilization: float, eligible: int
) -> None:
    POOL_UTILIZATION.labels(pool=pool_name).set(utilization)
    POOL_ELIGIBLE_NODES.labels(pool=pool_name).set(eligible)
    DRAIN_EVENTS.labels(pool=pool_name, outcome=outcome).inc()


def _pick_drain_candidate(
    pool: models.ServerPool, eligible: list[models.VPNNode]
) -> models.VPNNode | None:
    """Pick the best node to drain from the eligible set.

    Heuristic: prefer auto-spawned nodes (notes start with ``auto-scaled``
    OR have a non-null ``provider_id`` and a name matching the spawn
    suffix) over manual ones — operators usually want their hand-rolled
    nodes left alone. Within the eligible candidates, pick the youngest
    (created_at desc) so we shrink the most recently added capacity, and
    tiebreak on the lowest active-subscription count to minimize the
    migration cost.
    """
    candidates: list[models.VPNNode] = []
    for n in eligible:
        if n.provider_id is None:
            continue  # manual node, never auto-destroy
        if not (n.notes or "").startswith("auto-scaled") and "auto-" not in (n.name or ""):
            continue  # operator-spawned via UI, leave alone
        candidates.append(n)
    if not candidates:
        return None
    candidates.sort(
        key=lambda n: (n.created_at or utcnow(), len(n.subscriptions)),
        reverse=True,  # newest first
    )
    return candidates[0]


def evaluate_pool_downscale(db: Session, pool: models.ServerPool) -> DrainDecision:
    """Decide whether to flip a node in this pool to ``draining``.

    Pure decision function: it only mutates the chosen node's status
    (and commits). The actual subscription migration + node destroy are
    done by ``run_drain_tick``. Keeping the two split mirrors the
    spawn/bootstrap split on the upscale side.
    """
    if not DOWNSCALE_ENABLED:
        _record_drain(pool.name, "disabled", 0.0, 0)
        return DrainDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=0.0,
            eligible_nodes=0,
            already_draining=0,
            marked_node_id=None,
            reason="downscale disabled (AUTOSCALE_DOWNSCALE_ENABLED=0)",
        )

    if not pool.autoscale_enabled:
        _record_drain(pool.name, "disabled", 0.0, 0)
        return DrainDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=0.0,
            eligible_nodes=0,
            already_draining=0,
            marked_node_id=None,
            reason="autoscale disabled",
        )

    eligible = _eligible_nodes(pool)
    capacity = sum(_node_capacity(n) for n in eligible)
    active = _active_subs_on_nodes(db, eligible)
    utilization = (active / capacity) if capacity else 0.0

    already_draining = sum(
        1 for n in pool.nodes if n.status == models.VPNNodeStatus.draining
    )
    if already_draining > 0:
        _record_drain(pool.name, "already_draining", utilization, len(eligible))
        return DrainDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            eligible_nodes=len(eligible),
            already_draining=already_draining,
            marked_node_id=None,
            reason=f"{already_draining} node(s) already draining",
        )

    min_nodes = _pool_min_nodes(pool)
    if len(eligible) <= min_nodes:
        _record_drain(pool.name, "min_nodes_floor", utilization, len(eligible))
        return DrainDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            eligible_nodes=len(eligible),
            already_draining=0,
            marked_node_id=None,
            reason=f"eligible={len(eligible)} <= min_nodes={min_nodes}",
        )

    low = _pool_low_watermark(pool)
    if utilization >= low:
        _record_drain(pool.name, "above_low_watermark", utilization, len(eligible))
        return DrainDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            eligible_nodes=len(eligible),
            already_draining=0,
            marked_node_id=None,
            reason=f"utilization {utilization:.2f} >= low_watermark {low:.2f}",
        )

    # Sanity check: would shrinking by one still keep us under the
    # high_watermark? If not, draining now would just trigger a re-spawn
    # on the next upscale tick — pointless thrash.
    high = _pool_high_watermark(pool)
    remaining_capacity = capacity - max(
        (_node_capacity(n) for n in eligible), default=0
    )
    if remaining_capacity > 0 and (active / remaining_capacity) >= high:
        _record_drain(pool.name, "above_low_watermark", utilization, len(eligible))
        return DrainDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            eligible_nodes=len(eligible),
            already_draining=0,
            marked_node_id=None,
            reason=(
                f"shrinking would push utilization to "
                f"{active / remaining_capacity:.2f} >= {high:.2f}, refusing"
            ),
        )

    candidate = _pick_drain_candidate(pool, eligible)
    if candidate is None:
        _record_drain(pool.name, "no_drain_target", utilization, len(eligible))
        return DrainDecision(
            pool_id=pool.id,
            pool_name=pool.name,
            utilization=utilization,
            eligible_nodes=len(eligible),
            already_draining=0,
            marked_node_id=None,
            reason="no auto-spawned candidate to drain (only manual nodes)",
        )

    logger.warning(
        "Downscale: pool %s utilization=%.2f < low=%.2f, marking node %s draining",
        pool.name,
        utilization,
        low,
        candidate.name,
    )
    candidate.status = models.VPNNodeStatus.draining
    candidate.updated_at = utcnow()
    db.add(candidate)
    db.commit()

    _record_drain(pool.name, "marked_draining", utilization, len(eligible))
    return DrainDecision(
        pool_id=pool.id,
        pool_name=pool.name,
        utilization=utilization,
        eligible_nodes=len(eligible),
        already_draining=0,
        marked_node_id=candidate.id,
        reason=f"marked node {candidate.name} draining",
    )


def evaluate_all_downscale(db: Session) -> list[DrainDecision]:
    pools = db.query(models.ServerPool).all()
    return [evaluate_pool_downscale(db, p) for p in pools]
