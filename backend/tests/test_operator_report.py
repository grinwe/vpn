"""operator-routing P1 — _do_failover пишет OperatorNodeReport.

Корень пустой карты: webapp-путь «VPN не работает» (_do_failover) переселял
юзера, но НЕ создавал OperatorNodeReport (его писал только бот-флоу). Теперь
каждый user-reported failover пишет репорт (operator=None, проставляется
отдельным тапом). См. docs/operations/operator_routing_roadmap.md.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services.operator_reports import report_reconnected, resolve_pending_reports
from app.services.provisioning import ProvisioningOrchestrator
from app.time_utils import utcnow
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def test_do_failover_creates_operator_report(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.client_control import _do_failover

    plan = make_plan(db_session)
    user = make_user(db_session)
    a = make_node(db_session, name="op-a", region="ru")
    b = make_node(db_session, name="op-b", region="ru", host="10.0.0.2")
    cfg_b = make_config(db_session, b)
    sub = make_subscription(db_session, user, plan, a)
    new_dev = make_device(db_session, sub, cfg_b, access_username="u-on-b")

    # migrate подменяем — нас интересует факт создания репорта, не ansible.
    monkeypatch.setattr(
        ProvisioningOrchestrator,
        "migrate_subscription_to_free_node",
        lambda self, s, **k: (b, new_dev, None, True),
    )

    res = _do_failover(db_session, sub, kind="user_reported", actor=f"user:{user.id}")
    assert res.action == "migrated"
    assert res.report_id is not None

    report = db_session.get(models.OperatorNodeReport, res.report_id)
    assert report is not None
    assert report.user_id == user.id
    assert report.failed_node_id == a.id          # упала старая нода
    assert report.target_node_id == b.id          # пересадили на новую
    assert report.target_access_username == "u-on-b"
    assert report.operator is None                # карьер ещё не выбран
    assert report.outcome == "pending"


# ── reconnect-чек: вложенный путь details[<proto>]["users"] (Q1 фикс) ────────

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


def test_report_reconnected_reads_nested_per_protocol(db_session: Session) -> None:
    """Regression: usernames лежат в details[<proto>]["users"], а не в
    верхнеуровневом details["users"]. Чтение не того ключа делало функцию
    всегда False (бот слал «всё ещё не работает» даже переподключившимся)."""
    user = make_user(db_session)
    node = make_node(db_session, name="rr-1", region="ru")
    r = _report(
        db_session, user, node, "dev3-user",
        reported_at=utcnow() - timedelta(minutes=1),
    )
    _sample(
        db_session, node,
        {"vless-reality": {"uplink": 1, "downlink": 2, "users": ["dev3-user"], "user_count": 1}},
    )
    assert report_reconnected(db_session, r) is True


def test_report_reconnected_per_device_sibling_does_not_match(
    db_session: Session,
) -> None:
    user = make_user(db_session)
    node = make_node(db_session, name="rr-2", region="ru")
    r = _report(
        db_session, user, node, "dev3-user",
        reported_at=utcnow() - timedelta(minutes=1),
    )
    # трафик ДРУГОГО устройства на той же ноде — не должен зачесться
    _sample(db_session, node, {"vless-reality": {"users": ["other-device-user"]}})
    assert report_reconnected(db_session, r) is False


def test_report_reconnected_tolerates_errors_and_legacy_int(
    db_session: Session,
) -> None:
    user = make_user(db_session)
    node = make_node(db_session, name="rr-3", region="ru")
    r = _report(
        db_session, user, node, "dev3-user",
        reported_at=utcnow() - timedelta(minutes=1),
    )
    _sample(db_session, node, {
        "_errors": {"vless-reality": "ssh timeout"},  # служебный ключ — skip
        "hysteria2": {"users": 7},                    # legacy int — не list, skip
        "vless-reality": {"users": ["dev3-user"]},
    })
    assert report_reconnected(db_session, r) is True


def test_report_reconnected_ignores_samples_before_report(
    db_session: Session,
) -> None:
    user = make_user(db_session)
    node = make_node(db_session, name="rr-4", region="ru")
    r = _report(db_session, user, node, "dev3-user", reported_at=utcnow())
    _sample(
        db_session, node, {"vless-reality": {"users": ["dev3-user"]}},
        observed_at=utcnow() - timedelta(minutes=5),  # ДО репорта
    )
    assert report_reconnected(db_session, r) is False


def test_resolve_pending_reports_marks_ok_on_reconnect(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPERATOR_RECONNECT_WINDOW_MIN", "10")
    user = make_user(db_session)
    node = make_node(db_session, name="rr-5", region="ru")
    r = _report(
        db_session, user, node, "dev-user",
        reported_at=utcnow() - timedelta(minutes=15),  # старше окна
    )
    _sample(
        db_session, node, {"vless-reality": {"users": ["dev-user"]}},
        observed_at=utcnow() - timedelta(minutes=14),
    )
    res = resolve_pending_reports(db_session)
    db_session.refresh(r)
    assert r.outcome == "ok"
    assert res["resolved_ok"] == 1


# ── POST /report-ok: позитивный user-driven исход (Q2) ───────────────────────

def test_report_ok_pending_to_ok(client, db_session: Session) -> None:
    user = make_user(db_session)
    node = make_node(db_session, name="rok-1", region="ru")
    r = _report(db_session, user, node, "u", outcome="pending")
    resp = client.post("/api/admin/client-control/report-ok", json={"report_id": r.id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "ok"
    db_session.refresh(r)
    assert r.outcome == "ok"
    assert r.resolved_at is not None


def test_report_ok_upgrades_inconclusive(client, db_session: Session) -> None:
    user = make_user(db_session)
    node = make_node(db_session, name="rok-2", region="ru")
    r = _report(db_session, user, node, "u", outcome="inconclusive")
    resp = client.post("/api/admin/client-control/report-ok", json={"report_id": r.id})
    assert resp.json()["outcome"] == "ok"


def test_report_ok_does_not_downgrade_fail(client, db_session: Session) -> None:
    user = make_user(db_session)
    node = make_node(db_session, name="rok-3", region="ru")
    r = _report(db_session, user, node, "u", outcome="fail")
    resp = client.post("/api/admin/client-control/report-ok", json={"report_id": r.id})
    assert resp.json()["outcome"] == "fail"  # явный негатив не перетираем
    db_session.refresh(r)
    assert r.outcome == "fail"


def test_report_ok_404(client) -> None:
    resp = client.post("/api/admin/client-control/report-ok", json={"report_id": 999999})
    assert resp.status_code == 404
