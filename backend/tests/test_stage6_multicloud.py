"""Stage 6 — multi-cloud provider tests.

Covers:
  * ``get_driver`` dispatches the right class for each enum kind
  * Vultr / DO drivers raise DriverError without a token
  * Autoscale walks ``autoscale_fallback_provider_ids`` after the
    primary provider raises NodeSpawnError, and only goes into backoff
    once the entire chain is exhausted.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models
from app.security import encrypt
from app.services import autoscale
from app.services.cloud import DriverError, get_driver
from app.services.cloud.digitalocean import DigitalOceanDriver
from app.services.cloud.hetzner import HetznerDriver
from app.services.cloud.vultr import VultrDriver
from app.services.node_spawner import NodeSpawnError
from tests.factories import (
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)


# ── get_driver dispatch ────────────────────────────────────────────


def _make_provider(
    db: Session, *, kind: models.CloudProviderKind, with_token: bool = True
) -> models.CloudProvider:
    provider = models.CloudProvider(
        name=f"prov-{kind.value}",
        kind=kind,
        api_token_enc=encrypt("dummy-token") if with_token else None,
        is_active=True,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def test_get_driver_dispatches_hetzner(db_session: Session) -> None:
    p = _make_provider(db_session, kind=models.CloudProviderKind.hetzner)
    assert isinstance(get_driver(p), HetznerDriver)


def test_get_driver_dispatches_vultr(db_session: Session) -> None:
    p = _make_provider(db_session, kind=models.CloudProviderKind.vultr)
    assert isinstance(get_driver(p), VultrDriver)


def test_get_driver_dispatches_digitalocean(db_session: Session) -> None:
    p = _make_provider(db_session, kind=models.CloudProviderKind.digitalocean)
    assert isinstance(get_driver(p), DigitalOceanDriver)


def test_get_driver_requires_token_for_vultr(db_session: Session) -> None:
    p = _make_provider(
        db_session, kind=models.CloudProviderKind.vultr, with_token=False
    )
    with pytest.raises(DriverError, match="Vultr"):
        get_driver(p)


def test_get_driver_requires_token_for_do(db_session: Session) -> None:
    p = _make_provider(
        db_session, kind=models.CloudProviderKind.digitalocean, with_token=False
    )
    with pytest.raises(DriverError, match="DigitalOcean"):
        get_driver(p)


# ── autoscale fallback chain ───────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_backoff() -> None:
    autoscale._spawn_backoff.clear()
    yield
    autoscale._spawn_backoff.clear()


def _hot_pool(
    db: Session,
    *,
    primary: int,
    fallbacks: list[int] | None,
) -> models.ServerPool:
    pool = models.ServerPool(
        name=f"chain-pool-{primary}",
        autoscale_enabled=True,
        autoscale_provider_id=primary,
        autoscale_region="fsn1",
        autoscale_plan="cx11",
        autoscale_high_watermark=0.5,
        autoscale_max_nodes=10,
        autoscale_fallback_provider_ids=fallbacks,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)
    # Saturate it: one tiny node + 2 active subs.
    node = make_node(db, name=f"chain-node-{primary}", host="198.51.100.50", max_users=2)
    node.pool_id = pool.id
    db.add(node)
    db.commit()
    user = make_user(db)
    user2 = make_user(db, telegram_id=f"tg-chain-{primary}-2")
    plan_row = make_plan(db)
    make_subscription_with_device(db, user, plan_row, node, access_username=f"chain-{primary}-1")
    make_subscription_with_device(db, user2, plan_row, node, access_username=f"chain-{primary}-2")
    db.commit()
    db.refresh(pool)
    return pool


def test_fallback_chain_tries_secondary_when_primary_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _hot_pool(db_session, primary=1, fallbacks=[2, 3])

    calls: list[int] = []

    def fake_spawn(db, **kwargs):
        provider_id = kwargs["provider_id"]
        calls.append(provider_id)
        if provider_id == 1:
            raise NodeSpawnError("hetzner abuse-locked")
        # Secondary succeeds.
        new_node = make_node(
            db_session,
            name=kwargs["name"],
            host="198.51.100.77",
            max_users=2,
        )
        return new_node, SimpleNamespace(id=999)

    monkeypatch.setattr(autoscale, "spawn_node", fake_spawn)

    decision = autoscale.evaluate_pool(db_session, pool)

    assert decision.scaled_up is True
    assert calls == [1, 2]  # primary failed, secondary succeeded, tertiary skipped
    assert pool.id not in autoscale._spawn_backoff


def test_fallback_chain_exhausted_triggers_backoff(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _hot_pool(db_session, primary=1, fallbacks=[2])

    calls: list[int] = []

    def boom(db, **kwargs):
        calls.append(kwargs["provider_id"])
        raise NodeSpawnError("everything is on fire")

    monkeypatch.setattr(autoscale, "spawn_node", boom)

    decision = autoscale.evaluate_pool(db_session, pool)

    assert decision.scaled_up is False
    assert calls == [1, 2]  # walked the whole chain
    assert "all 2 providers failed" in decision.reason
    assert pool.id in autoscale._spawn_backoff


def test_no_fallback_chain_behaves_like_before(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _hot_pool(db_session, primary=1, fallbacks=None)

    calls: list[int] = []

    def boom(db, **kwargs):
        calls.append(kwargs["provider_id"])
        raise NodeSpawnError("primary down")

    monkeypatch.setattr(autoscale, "spawn_node", boom)

    decision = autoscale.evaluate_pool(db_session, pool)

    assert decision.scaled_up is False
    assert calls == [1]
    assert pool.id in autoscale._spawn_backoff
