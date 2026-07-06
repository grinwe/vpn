"""auditfix3 — resolve_pending_reports: гонка fail↔inconclusive (#104) и
перечитывание тяжёлых JSONB-сэмплов по разу на каждый репорт (#255).

Отдельный файл (существующий test_operator_report.py не трогаем).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from app import models
from app.services import operator_reports
from app.services.operator_reports import resolve_pending_reports
from app.time_utils import utcnow
from tests.factories import make_node, make_user


def _report(db, user, node, username, *, reported_at=None, outcome="pending"):
    r = models.OperatorNodeReport(
        user_id=user.id,
        target_node_id=node.id,
        target_access_username=username,
        outcome=outcome,
        reported_at=reported_at or utcnow(),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _sample(db, node, details, *, observed_at=None):
    s = models.NodeTrafficSample(
        node_id=node.id,
        observed_at=observed_at or utcnow(),
        details=details,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ── #104: watcher не затирает явный fail, поставленный в гонке ────────────────
def test_resolve_preserves_concurrent_fail(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если между выборкой pending и conditional-UPDATE юзер тапнул «всё равно
    не работает» (report_still_broken пишет outcome='fail' в отдельной сессии),
    watcher НЕ перезаписывает fail своим ok/inconclusive."""
    from app.db import SessionLocal

    monkeypatch.setenv("OPERATOR_RECONNECT_WINDOW_MIN", "10")
    user = make_user(db_session)
    node = make_node(db_session, name="race-1", region="ru")
    r = _report(
        db_session, user, node, "dev-user",
        reported_at=utcnow() - timedelta(minutes=15),
    )
    _sample(
        db_session, node, {"vless-reality": {"users": ["dev-user"]}},
        observed_at=utcnow() - timedelta(minutes=14),
    )

    # Хук на _username_in_details имитирует гонку: ровно в момент детекта
    # reconnect независимая сессия ставит fail и коммитит (как эндпоинт
    # report-still-broken). Реставрируем оригинал сразу — гонка одноразовая.
    orig = operator_reports._username_in_details

    def _racing(details, username):
        other = SessionLocal()
        try:
            rep = other.get(models.OperatorNodeReport, r.id)
            rep.outcome = "fail"
            rep.resolved_at = utcnow()
            other.commit()
        finally:
            other.close()
        monkeypatch.setattr(operator_reports, "_username_in_details", orig)
        return orig(details, username)

    monkeypatch.setattr(operator_reports, "_username_in_details", _racing)

    res = resolve_pending_reports(db_session)

    db_session.expire(r)
    db_session.refresh(r)
    assert r.outcome == "fail"          # явный негатив сохранён
    assert res["resolved_ok"] == 0      # conditional-UPDATE не сматчил строку
    assert res["resolved_inconclusive"] == 0


# ── #255: сэмплы каждой ноды тянутся ОДИН раз за тик, не по разу на репорт ────
def test_resolve_loads_node_samples_once_per_tick(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import engine

    monkeypatch.setenv("OPERATOR_RECONNECT_WINDOW_MIN", "10")
    user = make_user(db_session)
    node = make_node(db_session, name="grp-1", region="ru")
    reported = utcnow() - timedelta(minutes=15)
    reports = {
        uname: _report(db_session, user, node, uname, reported_at=reported)
        for uname in ("u1", "u2", "u3")
    }
    _sample(
        db_session, node, {"vless-reality": {"users": ["u1", "u3"]}},
        observed_at=utcnow() - timedelta(minutes=14),
    )

    counter = {"n": 0}

    def _before(conn, cursor, statement, params, context, executemany):
        s = statement.lower()
        if s.lstrip().startswith("select") and "node_traffic_samples" in s:
            counter["n"] += 1

    sqlalchemy.event.listen(engine, "before_cursor_execute", _before)
    try:
        res = resolve_pending_reports(db_session)
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", _before)

    # три репорта по ОДНОЙ ноде → ровно один SELECT сэмплов, а не три
    assert counter["n"] == 1

    for uname in ("u1", "u2", "u3"):
        db_session.refresh(reports[uname])
    assert reports["u1"].outcome == "ok"
    assert reports["u3"].outcome == "ok"
    assert reports["u2"].outcome == "inconclusive"
    assert res["resolved_ok"] == 2
    assert res["resolved_inconclusive"] == 1


def test_resolve_groups_do_not_cross_contaminate(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сэмпл одной ноды не должен зачесться репорту на другой ноде."""
    monkeypatch.setenv("OPERATOR_RECONNECT_WINDOW_MIN", "10")
    user = make_user(db_session)
    node_a = make_node(db_session, name="grp-a", region="ru")
    node_b = make_node(db_session, name="grp-b", region="ru", host="10.0.0.9")
    reported = utcnow() - timedelta(minutes=15)
    ra = _report(db_session, user, node_a, "shared-user", reported_at=reported)
    rb = _report(db_session, user, node_b, "shared-user", reported_at=reported)
    # трафик «shared-user» есть только на ноде A
    _sample(
        db_session, node_a, {"vless-reality": {"users": ["shared-user"]}},
        observed_at=utcnow() - timedelta(minutes=14),
    )

    resolve_pending_reports(db_session)
    db_session.refresh(ra)
    db_session.refresh(rb)
    assert ra.outcome == "ok"
    assert rb.outcome == "inconclusive"  # на B того же username не видели
