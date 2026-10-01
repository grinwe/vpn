"""Phase C — carrying_fraction (read-only детект по поведению клиентов)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import models
from app.services.carrying import _sample_usernames, compute_carrying_fractions
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _active_cred(db, node, device, username):
    c = models.Credential(
        node_id=node.id, device_id=device.id, is_active=True,
        proto="vless-reality", config_text="x", access_username=username,
        pool_state=models.CredentialPoolState.assigned,
    )
    db.add(c)
    db.commit()
    return c


def test_sample_usernames_extraction() -> None:
    s = models.NodeTrafficSample(
        node_id=1,
        details={
            "vless-reality": {"users": ["u1", "u2"]},
            "hysteria2": {"users": ["u2"]},
            "_errors": {"x": 1},
            "legacy": {"users": 5},  # legacy int — игнор
        },
    )
    assert _sample_usernames(s) == {"u1", "u2"}
    assert _sample_usernames(None) == set()


def test_carrying_fraction_counts(db_session: Session) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session, name="cf-1", region="ru")
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)

    # два девайса с активным credential на ноде → eligible=2
    dev_on = make_device(db_session, sub, cfg, access_username="u-on")
    dev_off = make_device(db_session, sub, cfg, access_username="u-off")
    _active_cred(db_session, node, dev_on, "u-on")
    _active_cred(db_session, node, dev_off, "u-off")

    # последний сэмпл видит трафик только u-on → carrying=1
    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id, details={"vless-reality": {"users": ["u-on"]}}
        )
    )
    db_session.commit()

    rows = compute_carrying_fractions(db_session)
    row = next(r for r in rows if r["node_id"] == node.id)
    assert row["eligible_devices"] == 2
    assert row["carrying_devices"] == 1
    assert row["carrying_fraction"] == 0.5


def test_carrying_fraction_none_when_no_eligible(db_session: Session) -> None:
    node = make_node(db_session, name="cf-empty", region="ru")
    rows = compute_carrying_fractions(db_session)
    row = next(r for r in rows if r["node_id"] == node.id)
    assert row["eligible_devices"] == 0
    assert row["carrying_fraction"] is None
