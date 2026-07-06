"""Auditfix2 client_control_py — находка 254 (perf).

Крауд-эскалация и per-sub throttle раньше считали сигналы JSONB-containment'ом
/ full-scan'ом по ``audit_logs`` внутри клиентского HTTP-запроса. Теперь оба
считаются по ``operator_node_reports`` (``failed_node_id`` / ``reported_at`` —
btree-индексы). Эти тесты фиксируют, что счётчики читают именно
``operator_node_reports`` (в таблице аудита при этом НЕТ ни одной строки).
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app import models
from app.api.client_control import _do_failover, _escalate_node_failure_reports
from app.time_utils import utcnow
from tests.factories import make_node, make_plan, make_subscription, make_user


def _add_report(
    db: Session,
    user,
    sub,
    failed_node,
    *,
    reported_at=None,
) -> models.OperatorNodeReport:
    r = models.OperatorNodeReport(
        user_id=user.id,
        subscription_id=sub.id,
        failed_node_id=failed_node.id,
        target_node_id=None,
        outcome="pending",
        reported_at=reported_at or utcnow(),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def test_escalate_counts_from_operator_node_reports_not_audit(
    db_session: Session, monkeypatch,
) -> None:
    """4 репорта от РАЗНЫХ подписок по одной ноде → cooldown. Считается по
    operator_node_reports (audit_logs пустой) — доказывает переход с JSONB."""
    monkeypatch.setenv("NODE_FAILURE_BAN_THRESHOLD", "4")
    monkeypatch.setenv("NODE_FAILURE_REPORT_WINDOW_MIN", "60")

    plan = make_plan(db_session)
    bad = make_node(db_session, name="crowd254-bad", region="ru", host="10.9.9.1")

    for i in range(3):
        u = make_user(db_session, telegram_id=f"tg-254-{i}")
        s = make_subscription(db_session, u, plan, bad)
        _add_report(db_session, u, s, bad)

    # Под порогом (3 < 4) — cooldown не ставится.
    _escalate_node_failure_reports(db_session, bad.id)
    db_session.expire_all()
    assert db_session.get(models.VPNNode, bad.id).cooldown_until is None

    # Аудита нет вообще — счётчик берёт данные из operator_node_reports.
    assert (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "client_reported_failure")
        .count()
        == 0
    )

    # Четвёртая подписка добивает порог → нода в cooldown.
    u4 = make_user(db_session, telegram_id="tg-254-3")
    s4 = make_subscription(db_session, u4, plan, bad)
    _add_report(db_session, u4, s4, bad)

    _escalate_node_failure_reports(db_session, bad.id)
    db_session.expire_all()
    node = db_session.get(models.VPNNode, bad.id)
    assert node.cooldown_until is not None
    assert node.cooldown_until > utcnow()


def test_escalate_counts_distinct_subscription(
    db_session: Session, monkeypatch,
) -> None:
    """5 репортов ОДНОЙ подписки считаются как 1 (DISTINCT subscription_id) —
    один нетерпеливый юзер не перебивает порог сам."""
    monkeypatch.setenv("NODE_FAILURE_BAN_THRESHOLD", "4")
    monkeypatch.setenv("NODE_FAILURE_REPORT_WINDOW_MIN", "60")

    plan = make_plan(db_session)
    bad = make_node(db_session, name="crowd254-solo", region="ru", host="10.9.9.2")
    u = make_user(db_session, telegram_id="tg-254-solo")
    s = make_subscription(db_session, u, plan, bad)
    for _ in range(5):
        _add_report(db_session, u, s, bad)

    _escalate_node_failure_reports(db_session, bad.id)
    db_session.expire_all()
    assert db_session.get(models.VPNNode, bad.id).cooldown_until is None


def test_escalate_ignores_reports_outside_window(
    db_session: Session, monkeypatch,
) -> None:
    """Старые репорты (вне окна reported_at) не считаются."""
    monkeypatch.setenv("NODE_FAILURE_BAN_THRESHOLD", "2")
    monkeypatch.setenv("NODE_FAILURE_REPORT_WINDOW_MIN", "60")

    plan = make_plan(db_session)
    bad = make_node(db_session, name="crowd254-old", region="ru", host="10.9.9.3")
    old = utcnow() - timedelta(minutes=120)
    for i in range(3):
        u = make_user(db_session, telegram_id=f"tg-254-old-{i}")
        s = make_subscription(db_session, u, plan, bad)
        _add_report(db_session, u, s, bad, reported_at=old)

    _escalate_node_failure_reports(db_session, bad.id)
    db_session.expire_all()
    assert db_session.get(models.VPNNode, bad.id).cooldown_until is None


def test_do_failover_throttle_via_operator_node_reports(
    db_session: Session,
) -> None:
    """Свежий OperatorNodeReport по подписке троттлит повторный failover
    (без обращения к audit_logs)."""
    plan = make_plan(db_session)
    node = make_node(db_session, name="thr254", region="ru", host="10.9.9.4")
    u = make_user(db_session, telegram_id="tg-254-thr")
    sub = make_subscription(db_session, u, plan, node)
    _add_report(db_session, u, sub, node)  # reported_at = now → в 5-мин окне

    resp = _do_failover(db_session, sub, kind="user_reported", actor="test")
    assert resp.action == "throttled"
    assert resp.ok is True


def test_do_failover_no_throttle_when_report_is_old(
    db_session: Session, monkeypatch,
) -> None:
    """Репорт старше 5 мин не троттлит (окно истекло) — доходим до миграции."""
    from app.services.provisioning import ProvisioningOrchestrator

    plan = make_plan(db_session)
    node = make_node(db_session, name="thr254-old", region="ru", host="10.9.9.5")
    u = make_user(db_session, telegram_id="tg-254-thr-old")
    sub = make_subscription(db_session, u, plan, node)
    _add_report(
        db_session, u, sub, node, reported_at=utcnow() - timedelta(minutes=10)
    )

    # Свободной ноды нет → мигратор бросает RuntimeError → no_target_available.
    # Ключевое: троттл-проверка пропустила старый репорт (иначе вернулся бы
    # throttled ещё до вызова миграции).
    def _no_free(self, sub, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("no free node")

    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node", _no_free
    )

    resp = _do_failover(db_session, sub, kind="user_reported", actor="test")
    assert resp.action == "no_target_available"
