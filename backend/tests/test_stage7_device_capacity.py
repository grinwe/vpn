"""Stage 7 — capacity is counted per-device, not per-subscription.

A Family sub with 3 devices must occupy 3 capacity slots in
``choose_node`` and the autoscaler's denominator, otherwise the system
under-provisions once users start adding extras.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import models
from app.services import autoscale
from app.services.provisioning import choose_node
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_provider,
    make_subscription,
    make_subscription_with_device,
    make_user,
)


def _attach(node: models.VPNNode, pool: models.ServerPool, db: Session) -> None:
    node.pool_id = pool.id
    db.add(node)
    db.commit()
    db.refresh(node)
    db.refresh(pool)


def test_choose_node_skips_node_full_of_devices(db_session: Session) -> None:
    """One sub, three devices → fills a max_users=3 node, choose_node
    must move on to a second node."""
    plan = make_plan(db_session, max_devices=3)
    full = make_node(db_session, name="full", host="198.51.100.70", max_users=3)
    spare = make_node(db_session, name="spare", host="198.51.100.71", max_users=3)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, full)
    cfg = make_config(db_session, full)
    make_device(db_session, sub, cfg, access_username="d1")
    make_device(db_session, sub, cfg, access_username="d2")
    make_device(db_session, sub, cfg, access_username="d3")

    picked = choose_node(db_session, plan)
    assert picked.id == spare.id


def test_choose_node_ignores_revoked_devices(db_session: Session) -> None:
    """Revoked / disabled devices don't count toward capacity."""
    plan = make_plan(db_session, max_devices=3)
    node = make_node(db_session, name="rev", host="198.51.100.72", max_users=2)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, node)
    cfg = make_config(db_session, node)
    make_device(db_session, sub, cfg, access_username="alive")
    dead = make_device(db_session, sub, cfg, access_username="dead")
    dead.status = models.DeviceStatus.revoked
    db_session.add(dead)
    db_session.commit()

    # 1 active device + 1 capacity left → must succeed and pick this node.
    picked = choose_node(db_session, plan)
    assert picked.id == node.id


def test_autoscale_counts_devices_not_subs(db_session: Session) -> None:
    """A single sub with 3 devices on a max_users=3 node → 100% util."""
    # ServerPool.autoscale_provider_id — FK на cloud_providers.id, поэтому
    # сначала засеваем провайдера и берём его реальный id.
    provider = make_provider(db_session)
    pool = models.ServerPool(
        name="dev-pool",
        autoscale_enabled=True,
        autoscale_provider_id=provider.id,
        autoscale_region="fsn1",
        autoscale_plan="cx11",
        autoscale_high_watermark=0.5,
        autoscale_max_nodes=10,
    )
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    node = make_node(db_session, name="dev-1", host="198.51.100.73", max_users=3)
    _attach(node, pool, db_session)
    user = make_user(db_session)
    plan = make_plan(db_session, max_devices=3)
    sub = make_subscription(db_session, user, plan, node)
    cfg = make_config(db_session, node)
    make_device(db_session, sub, cfg, access_username="d1")
    make_device(db_session, sub, cfg, access_username="d2")
    make_device(db_session, sub, cfg, access_username="d3")

    count = autoscale._active_subs_on_nodes(db_session, [node])
    assert count == 3  # devices, not the 1 sub


def test_autoscale_counts_zero_when_only_revoked_devices(
    db_session: Session,
) -> None:
    # FK на cloud_providers.id — засеваем провайдера перед пулом.
    provider = make_provider(db_session)
    pool = models.ServerPool(
        name="rev-pool",
        autoscale_enabled=True,
        autoscale_provider_id=provider.id,
        autoscale_region="fsn1",
        autoscale_plan="cx11",
    )
    db_session.add(pool)
    db_session.commit()

    node = make_node(db_session, name="rev-1", host="198.51.100.74", max_users=3)
    _attach(node, pool, db_session)
    user = make_user(db_session)
    plan = make_plan(db_session)
    sub = make_subscription(db_session, user, plan, node)
    cfg = make_config(db_session, node)
    d = make_device(db_session, sub, cfg, access_username="zombie")
    d.status = models.DeviceStatus.revoked
    db_session.add(d)
    db_session.commit()

    assert autoscale._active_subs_on_nodes(db_session, [node]) == 0


def _diverse_cred(
    db: Session, node: models.VPNNode, device: models.Device, username: str
) -> models.Credential:
    c = models.Credential(
        node_id=node.id,
        device_id=device.id,
        is_active=True,
        proto="vless-reality",
        config_text="enc-uri",
        access_username=username,
        pool_state=models.CredentialPoolState.assigned,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_choose_node_sees_diverse_creds(db_session: Session) -> None:
    """Нода без primary-подписок, но с diverse-кредом — НЕ пустая.

    Регресс на диверс-слепоту балансировщика: N×M кладёт креды на другие
    ноды, не меняя Subscription.node_id, и старая метрика считала такие
    ноды пустыми (см. docs/NODES.md «Балансировка новых юзеров»)."""
    plan = make_plan(db_session, max_devices=3)
    primary = make_node(db_session, name="prim", host="198.51.100.77", max_users=10)
    diverse = make_node(db_session, name="divr", host="198.51.100.78", max_users=1)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, primary)
    cfg = make_config(db_session, primary)
    dev = make_device(db_session, sub, cfg, access_username="d1")
    _diverse_cred(db_session, diverse, dev, "d1@divr")

    # diverse-нода забита своим diverse-кредом (1/1) → выбор обязан упасть
    # на primary (1/10); старая метрика видела diverse пустой и брала её.
    picked = choose_node(db_session, plan)
    assert picked.id == primary.id


def test_diverse_device_counts_once_per_node(db_session: Session) -> None:
    """Primary-девайс с кредом на СВОЕЙ же ноде не задваивается (union-дедуп)."""
    plan = make_plan(db_session, max_devices=3)
    node = make_node(db_session, name="dedup", host="198.51.100.79", max_users=3)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, node)
    cfg = make_config(db_session, node)
    dev = make_device(db_session, sub, cfg, access_username="d1")
    _diverse_cred(db_session, node, dev, "d1@own")

    assert autoscale._active_subs_on_nodes(db_session, [node]) == 1


def test_autoscale_counts_diverse_creds(db_session: Session) -> None:
    """Утилизация пула видит diverse-креды чужих (по primary) подписок."""
    plan = make_plan(db_session, max_devices=3)
    primary = make_node(db_session, name="prim-2", host="198.51.100.80", max_users=10)
    diverse = make_node(db_session, name="divr-2", host="198.51.100.81", max_users=10)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, primary)
    cfg = make_config(db_session, primary)
    dev = make_device(db_session, sub, cfg, access_username="d2")
    _diverse_cred(db_session, diverse, dev, "d2@divr")

    # На diverse-ноде нет ни одной primary-подписки, но слот занят.
    assert autoscale._active_subs_on_nodes(db_session, [diverse]) == 1


def test_choose_node_picks_least_loaded_by_devices(db_session: Session) -> None:
    """Two healthy nodes, one has 1 device, one has 2 → pick the
    lighter one."""
    plan = make_plan(db_session, max_devices=3)
    light = make_node(db_session, name="light", host="198.51.100.75", max_users=10)
    heavy = make_node(db_session, name="heavy", host="198.51.100.76", max_users=10)
    user = make_user(db_session)
    make_subscription_with_device(
        db_session, user, plan, light, access_username="light-1"
    )
    sub_h = make_subscription(db_session, user, plan, heavy)
    cfg_h = make_config(db_session, heavy)
    make_device(db_session, sub_h, cfg_h, access_username="heavy-1")
    make_device(db_session, sub_h, cfg_h, access_username="heavy-2")

    picked = choose_node(db_session, plan)
    assert picked.id == light.id
