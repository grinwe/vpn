"""Audit-fix #101 — carrying_fraction: выравнивание фильтров числителя и
знаменателя (is_active) + отбраковка протухшего traffic-сэмпла."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app import models
from app.services.carrying import compute_carrying_fractions
from app.time_utils import utcnow
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _active_cred(db, node, device, username, *, is_active=True):
    c = models.Credential(
        node_id=node.id, device_id=device.id, is_active=is_active,
        proto="vless-reality", config_text="x", access_username=username,
        pool_state=models.CredentialPoolState.assigned,
    )
    db.add(c)
    db.commit()
    return c


def test_inactive_credential_not_counted_in_carrying(db_session: Session) -> None:
    """Девайс с деактивированным кредом, чей username ещё в сэмпле, не должен
    попадать в carrying → fraction не превышает 1.0."""
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session, name="cf-inactive", region="ru")
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)

    dev_on = make_device(db_session, sub, cfg, access_username="u-on")
    dev_gone = make_device(db_session, sub, cfg, access_username="u-gone")
    _active_cred(db_session, node, dev_on, "u-on")
    # креды деактивированы (девайс отозван), но username ещё светится в сэмпле
    _active_cred(db_session, node, dev_gone, "u-gone", is_active=False)

    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id,
            details={"vless-reality": {"users": ["u-on", "u-gone"]}},
        )
    )
    db_session.commit()

    row = next(
        r for r in compute_carrying_fractions(db_session) if r["node_id"] == node.id
    )
    assert row["eligible_devices"] == 1  # только активный
    assert row["carrying_devices"] == 1  # u-gone отброшен фильтром is_active
    assert row["carrying_fraction"] == 1.0
    assert row["carrying_fraction"] <= 1.0
    assert row["stale"] is False


def test_stale_sample_yields_none_fraction(db_session: Session) -> None:
    """Сэмпл старше порога → carrying_fraction=None + stale=True."""
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session, name="cf-stale", region="ru")
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)

    dev = make_device(db_session, sub, cfg, access_username="u1")
    _active_cred(db_session, node, dev, "u1")

    old = models.NodeTrafficSample(
        node_id=node.id, details={"vless-reality": {"users": ["u1"]}}
    )
    old.observed_at = utcnow() - timedelta(hours=6)
    db_session.add(old)
    db_session.commit()

    row = next(
        r for r in compute_carrying_fractions(db_session) if r["node_id"] == node.id
    )
    assert row["stale"] is True
    assert row["carrying_fraction"] is None
