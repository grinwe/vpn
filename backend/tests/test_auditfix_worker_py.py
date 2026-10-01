"""Аудит-фиксы worker.py (находки 59, 95, 120).

* #59  — транзиентная ошибка в balance.renew_subscription больше НЕ
  экспайрит оплаченную auto_renew-подписку (экспайр только на явное
  «недостаточно средств», т.е. renew вернул False).
* #95  — reachability-тик обходит цели в порядке «дольше всех не
  пробовалась» (last_probe_at ASC, NULLS FIRST), чтобы лежащие ноды в
  начале списка не выедали бюджет и хвост не голодал между тиками.
* #120 — ops-план не зависает в executing навсегда: crash в
  run_ops_plan_execute помечает план failed, а реапер-тик добивает
  планы, чей воркер умер / джобу убил job_timeout.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from app import models, worker
from tests.factories import make_node, make_plan, make_subscription, make_user


# ── #59: renewal_check не экспайрит подписку на транзиентной ошибке ────

def _make_overdue_autorenew_sub(db):
    user = make_user(db, telegram_id="59001")
    plan = make_plan(db)
    node = make_node(db)
    sub = make_subscription(db, user, plan, node)
    sub.auto_renew = True
    sub.expires_at = datetime.utcnow() - timedelta(hours=1)
    db.add(sub)
    db.commit()
    return sub


def test_renewal_transient_error_keeps_sub_active(db_session, monkeypatch):
    """renew_subscription кидает исключение → подписка остаётся active,
    следующий тик повторит попытку (раньше проваливались в expired)."""
    sub = _make_overdue_autorenew_sub(db_session)

    def _boom(session, s):
        raise RuntimeError("transient db error")

    monkeypatch.setattr("app.services.balance.renew_subscription", _boom)

    stats = worker.run_renewal_check()

    db_session.expire_all()
    sub2 = db_session.get(models.Subscription, sub.id)
    assert sub2.status == models.SubscriptionStatus.active
    assert stats["expired"] == 0
    assert stats["errors"] >= 1


def test_renewal_insufficient_funds_still_expires(db_session, monkeypatch):
    """Регрессия-гард: явное «недостаточно средств» (renew вернул False)
    по-прежнему экспайрит подписку."""
    sub = _make_overdue_autorenew_sub(db_session)

    monkeypatch.setattr(
        "app.services.balance.renew_subscription", lambda session, s: False
    )

    stats = worker.run_renewal_check()

    db_session.expire_all()
    sub2 = db_session.get(models.Subscription, sub.id)
    assert sub2.status == models.SubscriptionStatus.expired
    assert stats["expired"] == 1


# ── #95: reachability-тик пробует самые «голодные» цели первыми ────────

def test_reachability_probes_least_recently_probed_first(db_session, monkeypatch):
    """Порядок пробинга: NULL (никогда не пробовалась) → самая старая →
    самая свежая, независимо от порядка выборки из БД."""
    from app.services.diagnostics import PathProbeResult

    now = datetime.utcnow()
    fresh = make_node(db_session, name="fresh-node", host="198.51.100.11")
    stale = make_node(db_session, name="stale-node", host="198.51.100.12")
    never = make_node(db_session, name="never-node", host="198.51.100.13")
    fresh.last_probe_at = now - timedelta(minutes=1)
    stale.last_probe_at = now - timedelta(hours=2)
    never.last_probe_at = None
    db_session.add_all([fresh, stale, never])
    db_session.commit()

    order: list[str] = []

    def _fake_probe(host, *, ssh_port=22, extra_tcp_ports=None,
                    traceroute_on_fail=True):
        order.append(host)
        r = PathProbeResult()
        r.ping_ok = True
        r.ssh_ok = True
        return r

    monkeypatch.setattr(
        "app.services.diagnostics.run_local_path_probe", _fake_probe
    )

    summary = worker.run_node_reachability_tick()

    assert order == [never.host, stale.host, fresh.host]
    # Все цели пробились в этом тике → голодающих нет.
    assert summary["stale_targets"] == 0


def test_reachability_reports_starving_targets(db_session, monkeypatch):
    """Цель, не попавшая в бюджет (проб протух), попадает в stale_targets."""
    from app.services.diagnostics import PathProbeResult

    node = make_node(db_session, name="starved-node", host="198.51.100.21")
    node.last_probe_at = datetime.utcnow() - timedelta(hours=3)
    db_session.add(node)
    db_session.commit()

    def _fake_probe(host, *, ssh_port=22, extra_tcp_ports=None,
                    traceroute_on_fail=True):
        r = PathProbeResult()
        r.ping_ok = True
        r.ssh_ok = True
        return r

    monkeypatch.setattr(
        "app.services.diagnostics.run_local_path_probe", _fake_probe
    )
    # Бюджет 0 секунд → цикл прерывается до первого проба.
    monkeypatch.setenv("NODE_REACHABILITY_BUDGET_SEC", "-1")

    summary = worker.run_node_reachability_tick()

    assert summary["checked"] == 0
    assert summary["stale_targets"] == 1


# ── #120: ops-план не зависает в executing ─────────────────────────────

def _make_ops_plan(db, status: str = "executing") -> models.OpsPlan:
    body = {"steps": []}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
    plan = models.OpsPlan(
        actor="test-admin",
        command="закажи ноду",
        plan=body,
        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        status=status,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def test_ops_plan_crash_marks_failed(db_session, monkeypatch):
    """Любое исключение кроме OpsExecError → план failed (phase=crash),
    а не вечный executing."""
    plan = _make_ops_plan(db_session)

    def _boom(session, p):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(
        "app.services.agent.ops_execution.execute_plan", _boom
    )

    res = worker.run_ops_plan_execute(plan.id)

    assert res["ok"] is False
    db_session.expire_all()
    p2 = db_session.get(models.OpsPlan, plan.id)
    assert p2.status == "failed"
    assert p2.execution["phase"] == "crash"
    assert "worker exploded" in p2.execution["reason"]


def test_ops_plan_rejected_does_not_stay_executing(db_session, monkeypatch):
    """OpsExecError без выставленного терминального статуса (ветка
    «исполнение выключено») тоже не оставляет план в executing."""
    from app.services.agent.ops_execution import OpsExecError

    plan = _make_ops_plan(db_session)

    def _reject(session, p):
        raise OpsExecError("исполнение выключено (OPS_EXECUTE_ENABLED=0)")

    monkeypatch.setattr(
        "app.services.agent.ops_execution.execute_plan", _reject
    )

    worker.run_ops_plan_execute(plan.id)

    db_session.expire_all()
    p2 = db_session.get(models.OpsPlan, plan.id)
    assert p2.status == "failed"
    assert p2.execution["phase"] == "rejected"


def test_ops_plan_terminal_status_not_overwritten(db_session, monkeypatch):
    """Терминальный статус, выставленный execute_plan (например expired по
    TTL), НЕ затирается страховкой — условный UPDATE его не трогает."""
    from app.services.agent.ops_execution import OpsExecError

    plan = _make_ops_plan(db_session)

    def _expire(session, p):
        p.status = "expired"
        session.commit()
        raise OpsExecError("план протух (TTL) — построй заново")

    monkeypatch.setattr(
        "app.services.agent.ops_execution.execute_plan", _expire
    )

    worker.run_ops_plan_execute(plan.id)

    db_session.expire_all()
    p2 = db_session.get(models.OpsPlan, plan.id)
    assert p2.status == "expired"
    assert p2.execution is None


def test_ops_plan_reaper_fails_stale_executing(db_session):
    """Реапер добивает executing-план, армленный дольше job_timeout+grace
    назад (воркер умер / джоба убита), и не трогает свежий."""
    stale = _make_ops_plan(db_session)
    fresh = _make_ops_plan(db_session)
    db_session.add(
        models.AuditLog(
            actor="admin",
            actor_type=models.AuditActor.system,
            action="agent_ops_execute_armed",
            target_type="ops_plan",
            target_id=stale.id,
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
    )
    db_session.add(
        models.AuditLog(
            actor="admin",
            actor_type=models.AuditActor.system,
            action="agent_ops_execute_armed",
            target_type="ops_plan",
            target_id=fresh.id,
            created_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    summary = worker.run_ops_plan_reaper_tick()

    assert stale.id in summary["reaped"]
    assert fresh.id not in summary["reaped"]
    db_session.expire_all()
    assert db_session.get(models.OpsPlan, stale.id).status == "failed"
    assert db_session.get(models.OpsPlan, stale.id).execution["phase"] == "crash"
    assert db_session.get(models.OpsPlan, fresh.id).status == "executing"
    reap_log = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.action == "agent_ops_execute_reaped",
            models.AuditLog.target_id == stale.id,
        )
        .first()
    )
    assert reap_log is not None


def test_ops_plan_reaper_skips_recent_without_armed_log(db_session):
    """Без armed-лога fallback идёт на expires_at/created_at: свежесозданный
    executing-план реапер не трогает."""
    plan = _make_ops_plan(db_session)

    summary = worker.run_ops_plan_reaper_tick()

    assert plan.id not in summary["reaped"]
    db_session.expire_all()
    assert db_session.get(models.OpsPlan, plan.id).status == "executing"
