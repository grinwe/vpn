"""Stage 7 — capacity is counted per-device, not per-subscription.

A Family sub with 3 devices must occupy 3 capacity slots in
``choose_node`` and the autoscaler's denominator, otherwise the system
under-provisions once users start adding extras.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import autoscale
from app.services.provisioning import choose_node
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
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
    pool = models.ServerPool(
        name="dev-pool",
        autoscale_enabled=True,
        autoscale_provider_id=1,
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
    pool = models.ServerPool(
        name="rev-pool",
        autoscale_enabled=True,
        autoscale_provider_id=1,
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
