"""Decision-tree tests for ``services.autoscale.evaluate_pool``.

Covers each branch the operator cares about:
  * disabled pool → no spawn, ``disabled`` outcome
  * misconfigured (no provider) → ``misconfigured``
  * utilization below watermark → ``below_watermark``
  * utilization at/above watermark, capacity available → ``scaled_up``
    (spawn_node monkey-patched, so we don't touch any cloud API)
  * pool already at ``autoscale_max_nodes`` → ``max_nodes_reached``
  * spawn raises ``NodeSpawnError`` → ``spawn_failed`` + backoff parks
    the pool on the next tick

We assert against the returned ``PoolDecision`` plus the Prometheus
``SCALE_EVENTS`` counter so the metric wiring stays honest.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import autoscale
from app.services.node_spawner import NodeSpawnError
from tests.factories import (
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)


def _make_pool(
    db: Session,
    *,
    name: str = "auto-pool",
    enabled: bool = True,
    provider_id: int | None = 1,
    region: str | None = "fsn1",
    plan: str | None = "cx11",
    high_watermark: float | None = 0.8,
    max_nodes: int | None = 10,
) -> models.ServerPool:
    pool = models.ServerPool(
        name=name,
        autoscale_enabled=enabled,
        autoscale_provider_id=provider_id,
        autoscale_region=region,
        autoscale_plan=plan,
        autoscale_high_watermark=high_watermark,
        autoscale_max_nodes=max_nodes,
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


def _counter(pool_name: str, outcome: str) -> float:
    return autoscale.SCALE_EVENTS.labels(pool=pool_name, outcome=outcome)._value.get()


@pytest.fixture(autouse=True)
def _reset_backoff() -> None:
    autoscale._spawn_backoff.clear()
    yield
    autoscale._spawn_backoff.clear()


def test_disabled_pool_emits_disabled_outcome(db_session: Session) -> None:
    pool = _make_pool(db_session, name="off-pool", enabled=False)
    before = _counter("off-pool", "disabled")

    decision = autoscale.evaluate_pool(db_session, pool)

    assert decision.scaled_up is False
    assert "disabled" in decision.reason
    assert _counter("off-pool", "disabled") == before + 1


def test_misconfigured_pool_does_not_spawn(db_session: Session) -> None:
    pool = _make_pool(db_session, name="bad-pool", provider_id=None)
    before = _counter("bad-pool", "misconfigured")

    decision = autoscale.evaluate_pool(db_session, pool)

    assert decision.scaled_up is False
    assert "misconfigured" in decision.reason
    assert _counter("bad-pool", "misconfigured") == before + 1


def test_below_watermark_does_not_spawn(db_session: Session) -> None:
    pool = _make_pool(db_session, name="quiet-pool")
    node = make_node(db_session, name="quiet-1", host="198.51.100.30", max_users=100)
    _attach(node, pool, db_session)
    # Zero subscriptions on a 100-cap node → utilization 0.0
    before = _counter("quiet-pool", "below_watermark")

    decision = autoscale.evaluate_pool(db_session, pool)

    assert decision.scaled_up is False
    assert decision.utilization == 0.0
    assert _counter("quiet-pool", "below_watermark") == before + 1


def test_saturated_pool_calls_spawn_node(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _make_pool(db_session, name="hot-pool", high_watermark=0.5)
    # Tiny capacity (max_users=2) so 2 active subs = 100% utilization.
    node = make_node(db_session, name="hot-1", host="198.51.100.31", max_users=2)
    _attach(node, pool, db_session)
    user = make_user(db_session)
    plan_row = make_plan(db_session)
    make_subscription_with_device(db_session, user, plan_row, node, access_username="u-1")
    make_subscription_with_device(db_session, make_user(db_session, telegram_id=f"tg-{node.id}-2"), plan_row, node, access_username="u-2")

    spawned: dict = {}

    def fake_spawn(db, **kwargs):
        spawned.update(kwargs)
        new_node = make_node(
            db_session,
            name=kwargs["name"],
            host="198.51.100.99",
            max_users=2,
        )
        return new_node, SimpleNamespace(id=999)

    monkeypatch.setattr(autoscale, "spawn_node", fake_spawn)
    before = _counter("hot-pool", "scaled_up")

    decision = autoscale.evaluate_pool(db_session, pool)

    assert decision.scaled_up is True
    assert spawned["pool_id"] == pool.id
    assert spawned["region"] == "fsn1"
    assert spawned["plan"] == "cx11"
    assert _counter("hot-pool", "scaled_up") == before + 1


def test_max_nodes_blocks_further_spawns(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _make_pool(db_session, name="capped-pool", max_nodes=1, high_watermark=0.5)
    node = make_node(db_session, name="cap-1", host="198.51.100.32", max_users=2)
    _attach(node, pool, db_session)
    user = make_user(db_session)
    plan_row = make_plan(db_session)
    make_subscription_with_device(db_session, user, plan_row, node, access_username="u-1")
    make_subscription_with_device(db_session, make_user(db_session, telegram_id=f"tg-{node.id}-2"), plan_row, node, access_username="u-2")

    monkeypatch.setattr(
        autoscale, "spawn_node", lambda *a, **kw: pytest.fail("must not spawn")
    )
    before = _counter("capped-pool", "max_nodes_reached")

    decision = autoscale.evaluate_pool(db_session, pool)

    assert decision.scaled_up is False
    assert "max_nodes" in decision.reason
    assert _counter("capped-pool", "max_nodes_reached") == before + 1


def test_spawn_failure_parks_pool_in_backoff(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _make_pool(db_session, name="flaky-pool", high_watermark=0.5)
    node = make_node(db_session, name="flaky-1", host="198.51.100.33", max_users=2)
    _attach(node, pool, db_session)
    user = make_user(db_session)
    plan_row = make_plan(db_session)
    make_subscription_with_device(db_session, user, plan_row, node, access_username="u-1")
    make_subscription_with_device(db_session, make_user(db_session, telegram_id=f"tg-{node.id}-2"), plan_row, node, access_username="u-2")

    def boom(*args, **kwargs):
        raise NodeSpawnError("provider angry")

    monkeypatch.setattr(autoscale, "spawn_node", boom)
    fail_before = _counter("flaky-pool", "spawn_failed")

    first = autoscale.evaluate_pool(db_session, pool)

    assert first.scaled_up is False
    assert "provider angry" in first.reason
    assert _counter("flaky-pool", "spawn_failed") == fail_before + 1
    assert pool.id in autoscale._spawn_backoff

    # Second tick within the backoff window must not even reach spawn_node.
    monkeypatch.setattr(
        autoscale,
        "spawn_node",
        lambda *a, **kw: pytest.fail("must not retry while backed off"),
    )
    backoff_before = _counter("flaky-pool", "backoff")

    second = autoscale.evaluate_pool(db_session, pool)

    assert second.scaled_up is False
    assert "backoff" in second.reason
    assert _counter("flaky-pool", "backoff") == backoff_before + 1
