"""Decision-tree tests for ``services.autoscale.evaluate_pool_downscale``.

Mirrors the upscale test layout: each branch the operator cares about
gets one focused case, and we assert against the returned
``DrainDecision`` plus the per-outcome counter on ``DRAIN_EVENTS``.

The drain tick itself (worker.run_drain_tick) is exercised separately
in `test_drain_tick`. Here we only care about the pure mark phase.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import autoscale
from tests.factories import (
    make_node,
    make_plan,
    make_subscription,
    make_subscription_with_device,
    make_user,
)


# ── helpers ──────────────────────────────────────────────────────────


def _make_pool(
    db: Session,
    *,
    name: str = "down-pool",
    enabled: bool = True,
    low_watermark: float | None = 0.3,
    min_nodes: int | None = 1,
    high_watermark: float | None = 0.8,
    max_nodes: int | None = 10,
) -> models.ServerPool:
    pool = models.ServerPool(
        name=name,
        autoscale_enabled=enabled,
        autoscale_provider_id=1,
        autoscale_region="fsn1",
        autoscale_plan="cx11",
        autoscale_high_watermark=high_watermark,
        autoscale_low_watermark=low_watermark,
        autoscale_max_nodes=max_nodes,
        autoscale_min_nodes=min_nodes,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)
    return pool


def _attach(node: models.VPNNode, pool: models.ServerPool, db: Session) -> None:
    node.pool_id = pool.id
    db.add(node)
    db.commit()
    db.refresh(node)
    db.refresh(pool)


def _make_auto_node(
    db: Session, pool: models.ServerPool, *, name: str, host: str, max_users: int = 100
) -> models.VPNNode:
    """Auto-spawned node — ``provider_id`` set + notes match the
    spawn_node convention so ``_pick_drain_candidate`` will pick it."""
    node = make_node(db, name=name, host=host, max_users=max_users)
    node.provider_id = 1
    node.notes = "auto-scaled at 2026-04-08T00:00:00"
    db.add(node)
    db.commit()
    db.refresh(node)
    _attach(node, pool, db)
    return node


def _drain_counter(pool_name: str, outcome: str) -> float:
    return autoscale.DRAIN_EVENTS.labels(pool=pool_name, outcome=outcome)._value.get()


@pytest.fixture(autouse=True)
def _enable_downscale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default test posture: master switch ON. Individual tests flip
    it back off when they want to verify the kill-switch."""
    monkeypatch.setattr(autoscale, "DOWNSCALE_ENABLED", True)


# ── kill switch ──────────────────────────────────────────────────────


def test_master_switch_off_blocks_everything(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(autoscale, "DOWNSCALE_ENABLED", False)
    pool = _make_pool(db_session, name="off-down")
    _make_auto_node(db_session, pool, name="off-down-1", host="198.51.100.50")
    before = _drain_counter("off-down", "disabled")

    decision = autoscale.evaluate_pool_downscale(db_session, pool)

    assert decision.marked_node_id is None
    assert "downscale disabled" in decision.reason
    assert _drain_counter("off-down", "disabled") == before + 1


# ── min_nodes floor ──────────────────────────────────────────────────


def test_min_nodes_floor_protects_last_node(db_session: Session) -> None:
    pool = _make_pool(db_session, name="floor-pool", min_nodes=1)
    _make_auto_node(db_session, pool, name="floor-1", host="198.51.100.51")
    before = _drain_counter("floor-pool", "min_nodes_floor")

    decision = autoscale.evaluate_pool_downscale(db_session, pool)

    assert decision.marked_node_id is None
    assert "min_nodes" in decision.reason
    assert _drain_counter("floor-pool", "min_nodes_floor") == before + 1


# ── above low watermark ──────────────────────────────────────────────


def test_above_low_watermark_does_not_drain(db_session: Session) -> None:
    pool = _make_pool(db_session, name="busy-pool", low_watermark=0.3, min_nodes=1)
    n1 = _make_auto_node(db_session, pool, name="busy-1", host="198.51.100.52", max_users=10)
    _make_auto_node(db_session, pool, name="busy-2", host="198.51.100.53", max_users=10)
    user = make_user(db_session)
    plan = make_plan(db_session)
    # 8 active devices across 20 capacity = 0.4 → above low (0.3)
    for i in range(8):
        u = make_user(db_session, telegram_id=f"tg-busy-{i}")
        make_subscription_with_device(db_session, u, plan, n1, access_username=f"u-busy-{i}")
    before = _drain_counter("busy-pool", "above_low_watermark")

    decision = autoscale.evaluate_pool_downscale(db_session, pool)

    assert decision.marked_node_id is None
    assert _drain_counter("busy-pool", "above_low_watermark") == before + 1


# ── happy path: marks the youngest auto node ────────────────────────


def test_below_low_watermark_marks_youngest_auto_node(db_session: Session) -> None:
    pool = _make_pool(db_session, name="quiet-pool", low_watermark=0.3, min_nodes=1)
    older = _make_auto_node(db_session, pool, name="quiet-old", host="198.51.100.54", max_users=10)
    younger = _make_auto_node(db_session, pool, name="quiet-new", host="198.51.100.55", max_users=10)
    # Two subs across 20 capacity = 0.1 → well under low (0.3).
    user = make_user(db_session)
    plan = make_plan(db_session)
    make_subscription_with_device(db_session, user, plan, older, access_username="q-1")
    make_subscription_with_device(
        db_session,
        make_user(db_session, telegram_id="tg-quiet-2"),
        plan,
        older,
        access_username="q-2",
    )
    before = _drain_counter("quiet-pool", "marked_draining")

    decision = autoscale.evaluate_pool_downscale(db_session, pool)

    assert decision.marked_node_id == younger.id
    db_session.refresh(younger)
    assert younger.status == models.VPNNodeStatus.draining
    assert _drain_counter("quiet-pool", "marked_draining") == before + 1


# ── manual node never gets drained ──────────────────────────────────


def test_manual_nodes_are_never_drained(db_session: Session) -> None:
    pool = _make_pool(db_session, name="manual-pool", low_watermark=0.3, min_nodes=1)
    # Two manual nodes (no provider_id, no auto- notes). Plenty of slack.
    n1 = make_node(db_session, name="manual-1", host="198.51.100.56", max_users=10)
    n2 = make_node(db_session, name="manual-2", host="198.51.100.57", max_users=10)
    _attach(n1, pool, db_session)
    _attach(n2, pool, db_session)
    before = _drain_counter("manual-pool", "no_drain_target")

    decision = autoscale.evaluate_pool_downscale(db_session, pool)

    assert decision.marked_node_id is None
    assert "no auto-spawned candidate" in decision.reason
    assert _drain_counter("manual-pool", "no_drain_target") == before + 1


# ── only one node draining at a time ────────────────────────────────


def test_already_draining_blocks_second_mark(db_session: Session) -> None:
    pool = _make_pool(db_session, name="busy-drain", low_watermark=0.3, min_nodes=1)
    drainer = _make_auto_node(db_session, pool, name="bd-1", host="198.51.100.58", max_users=10)
    _make_auto_node(db_session, pool, name="bd-2", host="198.51.100.59", max_users=10)
    _make_auto_node(db_session, pool, name="bd-3", host="198.51.100.60", max_users=10)
    # Pre-mark one node draining.
    drainer.status = models.VPNNodeStatus.draining
    db_session.add(drainer)
    db_session.commit()
    db_session.refresh(pool)
    before = _drain_counter("busy-drain", "already_draining")

    decision = autoscale.evaluate_pool_downscale(db_session, pool)

    assert decision.marked_node_id is None
    assert decision.already_draining == 1
    assert _drain_counter("busy-drain", "already_draining") == before + 1


# ── shrink would re-spawn → refuse ──────────────────────────────────


def test_shrink_that_would_trip_high_watermark_is_refused(db_session: Session) -> None:
    """Pathological hysteresis case: low=0.3, high=0.5, two nodes of
    cap 10 each, 2 subs (utilization 0.1). Removing one node leaves
    cap=10 / 2 subs = 0.2, still under high — should drain. But if we
    bump it to 5 subs (utilization 0.25, still under low=0.3),
    removing one would push us to 5/10 = 0.5, hitting high → refuse.
    """
    pool = _make_pool(
        db_session, name="thrash-pool", low_watermark=0.3, high_watermark=0.5, min_nodes=1
    )
    n1 = _make_auto_node(db_session, pool, name="th-1", host="198.51.100.61", max_users=10)
    _make_auto_node(db_session, pool, name="th-2", host="198.51.100.62", max_users=10)
    user = make_user(db_session)
    plan = make_plan(db_session)
    # 5/20 = 0.25 → under low; but shrink → 5/10 = 0.5 → hits high.
    for i in range(5):
        u = make_user(db_session, telegram_id=f"tg-thrash-{i}")
        make_subscription_with_device(db_session, u, plan, n1, access_username=f"th-{i}")

    decision = autoscale.evaluate_pool_downscale(db_session, pool)

    assert decision.marked_node_id is None
    assert "shrinking would push" in decision.reason
