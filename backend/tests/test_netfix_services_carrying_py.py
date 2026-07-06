"""Netfix audit #8 — carrying_fraction: знаменатель не должен считать креды
девайсов протухших/замороженных подписок и отключённых девайсов, иначе доля
занижается и выглядит как широкий блок ноды."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import models
from app.services.carrying import compute_carrying_fractions
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


def _row(db, node):
    return next(
        r for r in compute_carrying_fractions(db) if r["node_id"] == node.id
    )


def test_expired_subscription_excluded_from_eligible(db_session: Session) -> None:
    """Девайс протухшей подписки с ещё-активным кредом не должен раздувать
    знаменатель eligible и топить carrying_fraction."""
    plan = make_plan(db_session)
    node = make_node(db_session, name="cf-exp", region="ru")
    cfg = make_config(db_session, node)

    # живая подписка + девайс, реально несущий трафик
    u_live = make_user(db_session, telegram_id="tg-live")
    sub_live = make_subscription(db_session, u_live, plan, node)
    dev_live = make_device(db_session, sub_live, cfg, access_username="u-live")
    _active_cred(db_session, node, dev_live, "u-live")

    # протухшая подписка + девайс с НЕподметённым активным кредом,
    # трафика не даёт (в сэмпле его нет)
    u_exp = make_user(db_session, telegram_id="tg-exp")
    sub_exp = make_subscription(db_session, u_exp, plan, node)
    sub_exp.status = models.SubscriptionStatus.expired
    db_session.commit()
    dev_exp = make_device(db_session, sub_exp, cfg, access_username="u-exp")
    _active_cred(db_session, node, dev_exp, "u-exp")

    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id, details={"vless-reality": {"users": ["u-live"]}}
        )
    )
    db_session.commit()

    row = _row(db_session, node)
    # только живой девайс в знаменателе → доля 1.0, а не 0.5
    assert row["eligible_devices"] == 1
    assert row["carrying_devices"] == 1
    assert row["carrying_fraction"] == 1.0


def test_frozen_subscription_and_disabled_device_excluded(
    db_session: Session,
) -> None:
    """Замороженная подписка и отключённый девайс тоже вне знаменателя."""
    plan = make_plan(db_session)
    node = make_node(db_session, name="cf-frozen", region="ru")
    cfg = make_config(db_session, node)

    u_live = make_user(db_session, telegram_id="tg-live2")
    sub_live = make_subscription(db_session, u_live, plan, node)
    dev_live = make_device(db_session, sub_live, cfg, access_username="u-live2")
    _active_cred(db_session, node, dev_live, "u-live2")

    # замороженная подписка
    u_frozen = make_user(db_session, telegram_id="tg-frozen")
    sub_frozen = make_subscription(db_session, u_frozen, plan, node)
    sub_frozen.status = models.SubscriptionStatus.frozen
    db_session.commit()
    dev_frozen = make_device(
        db_session, sub_frozen, cfg, access_username="u-frozen"
    )
    _active_cred(db_session, node, dev_frozen, "u-frozen")

    # активная подписка, но отключённый девайс
    u_dis = make_user(db_session, telegram_id="tg-dis")
    sub_dis = make_subscription(db_session, u_dis, plan, node)
    dev_dis = make_device(db_session, sub_dis, cfg, access_username="u-dis")
    dev_dis.status = models.DeviceStatus.disabled
    db_session.commit()
    _active_cred(db_session, node, dev_dis, "u-dis")

    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id, details={"vless-reality": {"users": ["u-live2"]}}
        )
    )
    db_session.commit()

    row = _row(db_session, node)
    assert row["eligible_devices"] == 1
    assert row["carrying_devices"] == 1
    assert row["carrying_fraction"] == 1.0


def test_carrying_never_exceeds_one_with_dead_sub_in_sample(
    db_session: Session,
) -> None:
    """Даже если access_username мёртвой подписки светится в сэмпле, числитель
    остаётся строгим подмножеством знаменателя (доля <= 1.0)."""
    plan = make_plan(db_session)
    node = make_node(db_session, name="cf-le1", region="ru")
    cfg = make_config(db_session, node)

    u_live = make_user(db_session, telegram_id="tg-l3")
    sub_live = make_subscription(db_session, u_live, plan, node)
    dev_live = make_device(db_session, sub_live, cfg, access_username="u-l3")
    _active_cred(db_session, node, dev_live, "u-l3")

    u_exp = make_user(db_session, telegram_id="tg-e3")
    sub_exp = make_subscription(db_session, u_exp, plan, node)
    sub_exp.status = models.SubscriptionStatus.expired
    db_session.commit()
    dev_exp = make_device(db_session, sub_exp, cfg, access_username="u-e3")
    _active_cred(db_session, node, dev_exp, "u-e3")

    # оба юзера в сэмпле — но мёртвый не должен вылезти в числитель сверх
    # знаменателя
    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id,
            details={"vless-reality": {"users": ["u-l3", "u-e3"]}},
        )
    )
    db_session.commit()

    row = _row(db_session, node)
    assert row["eligible_devices"] == 1
    assert row["carrying_devices"] == 1
    assert row["carrying_fraction"] == 1.0
