"""Сетевой аудит — фиксы reconnect-детекта operator_reports.

Находка #1 (High): report_reconnected/resolve_pending_reports засчитывали
переподключение ТОЛЬКО по target_access_username на target_node_id. У
диверс-девайса после failover активные Credential лежат на N нодах, у
каждой свой access_username; клиент с HAPP url-test оседает на лучшем
соседе — его трафик светится под ДРУГИМ username на ДРУГОЙ ноде. Теперь
reconnect засчитывается по любому активному креду набора.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services.operator_reports import report_reconnected, resolve_pending_reports
from app.time_utils import utcnow
from tests.factories import make_config, make_device, make_node, make_plan, make_subscription, make_user


def _credential(db, device, node, username, *, is_active=True):
    c = models.Credential(
        subscription_id=device.subscription_id,
        device_id=device.id,
        node_id=node.id,
        proto="vless-reality",
        config_text="stub",
        access_username=username,
        is_active=is_active,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _sample(db, node, details, *, observed_at=None):
    s = models.NodeTrafficSample(
        node_id=node.id,
        observed_at=observed_at or utcnow(),
        details=details,
    )
    db.add(s)
    db.commit()
    return s


def _setup_diverse(db):
    plan = make_plan(db)
    user = make_user(db)
    target = make_node(db, name="nf-target", region="ru")
    sibling = make_node(db, name="nf-sibling", region="ru", host="10.9.9.9")
    cfg = make_config(db, target)
    sub = make_subscription(db, user, plan, target)
    device = make_device(db, sub, cfg, access_username="u-on-target")
    # диверс-сосед: активный кред на другой ноде под другим username
    _credential(db, device, sibling, "u-on-sibling")
    r = models.OperatorNodeReport(
        user_id=user.id,
        subscription_id=sub.id,
        device_id=device.id,
        target_node_id=target.id,
        target_access_username="u-on-target",
        outcome="pending",
        reported_at=utcnow() - timedelta(minutes=1),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return user, target, sibling, device, r


def test_reconnect_counts_traffic_on_diverse_sibling(db_session: Session) -> None:
    """Трафик на диверс-соседе (другой username, другая нода) = reconnect,
    хотя на target-ноде тишина."""
    _user, _target, sibling, _device, r = _setup_diverse(db_session)
    # НИКАКОГО трафика на target, но клиент осел на соседе:
    _sample(db_session, sibling, {"vless-reality": {"users": ["u-on-sibling"]}})
    assert report_reconnected(db_session, r) is True


def test_reconnect_ignores_sibling_of_other_device(db_session: Session) -> None:
    """Трафик чужого username на соседе не должен зачитываться."""
    _user, _target, sibling, _device, r = _setup_diverse(db_session)
    _sample(db_session, sibling, {"vless-reality": {"users": ["someone-else"]}})
    assert report_reconnected(db_session, r) is False


def test_reconnect_still_matches_primary_target(db_session: Session) -> None:
    """Обратная совместимость: трафик на самой target-ноде по-прежнему = ok."""
    _user, target, _sibling, _device, r = _setup_diverse(db_session)
    _sample(db_session, target, {"vless-reality": {"users": ["u-on-target"]}})
    assert report_reconnected(db_session, r) is True


def test_resolve_pending_marks_ok_via_diverse_sibling(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Watcher-путь: репорт флипается в ok по трафику на соседе набора."""
    monkeypatch.setenv("OPERATOR_RECONNECT_WINDOW_MIN", "10")
    _user, _target, sibling, _device, r = _setup_diverse(db_session)
    # сдвигаем reported_at за окно резолва
    r.reported_at = utcnow() - timedelta(minutes=15)
    db_session.commit()
    _sample(
        db_session, sibling, {"vless-reality": {"users": ["u-on-sibling"]}},
        observed_at=utcnow() - timedelta(minutes=14),
    )
    res = resolve_pending_reports(db_session)
    db_session.refresh(r)
    assert r.outcome == "ok"
    assert res["resolved_ok"] == 1
