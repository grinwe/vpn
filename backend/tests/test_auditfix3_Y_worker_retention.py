"""Audit-fix wave 3 — кластер worker/warm-pool/diagnostics (ключ Y_worker_retention).

Покрывает три отгруженных фикса:

* #247 — ``run_retention_tick`` чистит безлимитно растущие ``audit_logs``
  (только ``subscription_fetch`` + ``*:delivered``) и ``node_traffic_samples``
  старше N дней батчами, не трогая свежие строки и прочие audit-события.
* #71  — ``run_warm_pool_revoke_tick`` — воркер-обёртка над стадией 2 отзыва
  warm-пула (``warm_pool.run_warm_pool_revoke_sweep``).
* #103 — ``_probe_traceroute`` определяет достижимость по резолвнутому IP, а
  не по доменной строке host (иначе ``-n`` числовой вывод → вечное «warn»).
"""
from __future__ import annotations

import types
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import models, worker
from app import queue as app_queue
from app.services import admin_notify, diagnostics
from app.services.provisioning import ProvisioningOrchestrator
from app.time_utils import utcnow
from tests.factories import make_node


@pytest.fixture(autouse=True)
def _no_reschedule(monkeypatch: pytest.MonkeyPatch):
    """Гасим self-reschedule — тесты гоняют тик синхронно, без RQ."""
    monkeypatch.setattr(app_queue, "schedule_tick", lambda *a, **k: None)


def _audit(db: Session, action: str, *, age_days: float, target_id: int = 1) -> int:
    row = models.AuditLog(
        actor="system",
        actor_type=models.AuditActor.system,
        action=action,
        target_type="subscription",
        target_id=target_id,
    )
    db.add(row)
    db.flush()
    row.created_at = utcnow() - timedelta(days=age_days)
    db.commit()
    return row.id


def _sample(db: Session, node_id: int, *, age_days: float) -> int:
    row = models.NodeTrafficSample(
        node_id=node_id,
        interval_seconds=300,
        uplink_bytes=1,
        downlink_bytes=1,
        active_users=0,
    )
    db.add(row)
    db.flush()
    row.observed_at = utcnow() - timedelta(days=age_days)
    db.commit()
    return row.id


# ── #247 retention ──────────────────────────────────────────────────────

def test_retention_prunes_old_noise_keeps_the_rest(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIT_LOG_RETENTION_DAYS", "90")
    monkeypatch.setenv("TRAFFIC_SAMPLE_RETENTION_DAYS", "30")

    node = make_node(db_session, name="ret-1", host="198.51.100.40")

    old_fetch = _audit(db_session, "subscription_fetch", age_days=120)
    old_delivered = _audit(db_session, "renewal_reminder:delivered", age_days=100)
    # Свежий шум — не трогаем.
    fresh_fetch = _audit(db_session, "subscription_fetch", age_days=1)
    # Старое, но НЕ шум (операционный аудит) — сохраняем несмотря на возраст.
    old_provisioning = _audit(db_session, "provisioning_dlq", age_days=200)

    old_sample = _sample(db_session, node.id, age_days=60)
    fresh_sample = _sample(db_session, node.id, age_days=2)

    summary = worker.run_retention_tick()

    assert summary["audit_deleted"] == 2
    assert summary["traffic_deleted"] == 1

    db_session.expire_all()
    assert db_session.get(models.AuditLog, old_fetch) is None
    assert db_session.get(models.AuditLog, old_delivered) is None
    assert db_session.get(models.AuditLog, fresh_fetch) is not None
    assert db_session.get(models.AuditLog, old_provisioning) is not None
    assert db_session.get(models.NodeTrafficSample, old_sample) is None
    assert db_session.get(models.NodeTrafficSample, fresh_sample) is not None


def test_retention_batching_deletes_all_over_multiple_batches(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIT_LOG_RETENTION_DAYS", "30")
    monkeypatch.setenv("RETENTION_DELETE_BATCH", "2")  # 5 строк → 3 батча

    ids = [_audit(db_session, "subscription_fetch", age_days=100) for _ in range(5)]

    summary = worker.run_retention_tick()

    assert summary["audit_deleted"] == 5
    db_session.expire_all()
    assert all(db_session.get(models.AuditLog, i) is None for i in ids)


def test_retention_disabled_by_zero_days(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUDIT_LOG_RETENTION_DAYS", "0")
    monkeypatch.setenv("TRAFFIC_SAMPLE_RETENTION_DAYS", "0")

    node = make_node(db_session, name="ret-off", host="198.51.100.41")
    a = _audit(db_session, "subscription_fetch", age_days=999)
    s = _sample(db_session, node.id, age_days=999)

    summary = worker.run_retention_tick()

    assert summary == {"audit_deleted": 0, "traffic_deleted": 0}
    db_session.expire_all()
    assert db_session.get(models.AuditLog, a) is not None
    assert db_session.get(models.NodeTrafficSample, s) is not None


# ── #71 warm-pool revoke tick ───────────────────────────────────────────

def test_warm_pool_revoke_tick_drives_the_sweep(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import warm_pool

    called = {}

    def _fake_sweep(session, batch_limit=None):  # noqa: ARG001
        called["hit"] = True
        return {"7:warm-7-abcd": True}

    monkeypatch.setattr(warm_pool, "run_warm_pool_revoke_sweep", _fake_sweep)

    result = worker.run_warm_pool_revoke_tick()

    assert called.get("hit") is True
    assert result == {"7:warm-7-abcd": True}


# ── #103 traceroute reachability by resolved IP ─────────────────────────

def test_traceroute_reached_matches_resolved_ip(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # FQDN host, числовой (-n) вывод traceroute: последний hop — IP назначения.
    trace = (
        "traceroute to node.example (203.0.113.9), 12 hops max\n"
        " 1  10.0.0.1  0.4 ms\n"
        " 2  203.0.113.9  8.1 ms\n"
    )

    class _CP:
        stdout = trace
        returncode = 0

    monkeypatch.setattr(diagnostics, "_run", lambda cmd, timeout: _CP())
    monkeypatch.setattr(
        diagnostics, "_resolve_host_ips", lambda host: {"203.0.113.9"}
    )

    check = diagnostics._probe_traceroute("node.example")
    assert check["status"] == "info"
    assert "НЕ доходит" not in check["message"]


def test_traceroute_warns_when_destination_not_in_last_hop(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = (
        "traceroute to node.example (203.0.113.9), 12 hops max\n"
        " 1  10.0.0.1  0.4 ms\n"
        " 2  * * *\n"
    )

    class _CP:
        stdout = trace
        returncode = 0

    monkeypatch.setattr(diagnostics, "_run", lambda cmd, timeout: _CP())
    monkeypatch.setattr(
        diagnostics, "_resolve_host_ips", lambda host: {"203.0.113.9"}
    )

    check = diagnostics._probe_traceroute("node.example")
    assert check["status"] == "warn"
    assert "НЕ доходит" in check["message"]


def test_traceroute_resolve_failure_degrades_to_string_match(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # Резолв провалился → деградация в сравнение по строке host + пометка.
    trace = (
        "traceroute to node.example, 12 hops max\n"
        " 1  10.0.0.1  0.4 ms\n"
        " 2  node.example  8.1 ms\n"
    )

    class _CP:
        stdout = trace
        returncode = 0

    monkeypatch.setattr(diagnostics, "_run", lambda cmd, timeout: _CP())
    monkeypatch.setattr(diagnostics, "_resolve_host_ips", lambda host: set())

    check = diagnostics._probe_traceroute("node.example")
    assert check["status"] == "info"  # host-строка найдена в последнем hop


# ── #98 крауд-инцидент не автозакрывается на SSH-живой ноде ─────────────

def _ok_probe(host, **k):
    return diagnostics.PathProbeResult(
        checks=[], ping_ok=True, ssh_ok=True, summary="ok"
    )


def test_guard_false_when_no_incident() -> None:
    now = utcnow()
    t = types.SimpleNamespace(diagnose_incident_open_at=None)
    assert worker._incident_auto_close_blocked(t, now) is False


def test_guard_false_for_plain_open_incident() -> None:
    now = utcnow()
    # Инцидент открыт, оператор не трогал, ноды нет в cooldown → закрывать МОЖНО.
    t = types.SimpleNamespace(
        diagnose_incident_open_at=now - timedelta(minutes=10),
        diagnose_acked_at=None,
        diagnose_follow_mode=None,
        cooldown_until=None,
    )
    assert worker._incident_auto_close_blocked(t, now) is False


def test_guard_true_when_acked_after_open() -> None:
    now = utcnow()
    open_at = now - timedelta(minutes=10)
    t = types.SimpleNamespace(
        diagnose_incident_open_at=open_at,
        diagnose_acked_at=open_at + timedelta(minutes=1),
        diagnose_follow_mode=None,
        cooldown_until=None,
    )
    assert worker._incident_auto_close_blocked(t, now) is True


def test_guard_stale_ack_before_open_does_not_block() -> None:
    now = utcnow()
    open_at = now - timedelta(minutes=10)
    # ack старше открытия текущего инцидента — не считается взятием в работу.
    t = types.SimpleNamespace(
        diagnose_incident_open_at=open_at,
        diagnose_acked_at=open_at - timedelta(minutes=5),
        diagnose_follow_mode=None,
        cooldown_until=None,
    )
    assert worker._incident_auto_close_blocked(t, now) is False


def test_guard_true_when_follow_mode() -> None:
    now = utcnow()
    t = types.SimpleNamespace(
        diagnose_incident_open_at=now - timedelta(minutes=10),
        diagnose_acked_at=None,
        diagnose_follow_mode="exponential",
        cooldown_until=None,
    )
    assert worker._incident_auto_close_blocked(t, now) is True


def test_guard_true_when_node_in_crowd_cooldown() -> None:
    now = utcnow()
    t = types.SimpleNamespace(
        diagnose_incident_open_at=now - timedelta(minutes=10),
        diagnose_acked_at=None,
        diagnose_follow_mode=None,
        cooldown_until=now + timedelta(hours=1),  # выведена крауд-порогом
    )
    assert worker._incident_auto_close_blocked(t, now) is True


def test_guard_false_when_cooldown_expired() -> None:
    now = utcnow()
    t = types.SimpleNamespace(
        diagnose_incident_open_at=now - timedelta(minutes=10),
        diagnose_acked_at=None,
        diagnose_follow_mode=None,
        cooldown_until=now - timedelta(minutes=1),  # cooldown истёк
    )
    assert worker._incident_auto_close_blocked(t, now) is False


def test_guard_exit_target_without_cooldown_attr() -> None:
    now = utcnow()
    # WGExitNode не имеет cooldown_until — getattr вернёт None, ветка не сработает.
    t = types.SimpleNamespace(
        diagnose_incident_open_at=now - timedelta(minutes=10),
        diagnose_acked_at=None,
        diagnose_follow_mode=None,
    )
    assert worker._incident_auto_close_blocked(t, now) is False


def _patch_tick_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODE_REACHABILITY_ENABLED", "true")
    monkeypatch.setattr(admin_notify, "notify_node_diagnosis", lambda *a, **k: [])
    monkeypatch.setattr(
        ProvisioningOrchestrator, "create_task",
        lambda self, *a, **k: types.SimpleNamespace(id=1),
    )
    monkeypatch.setattr(
        ProvisioningOrchestrator, "run_task_async", lambda self, *a, **k: None
    )
    monkeypatch.setattr(diagnostics, "run_local_path_probe", _ok_probe)


def test_crowd_incident_survives_ssh_ok_tick(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Крауд-инцидент (нода в cooldown) + ack оператора: ssh_ok-тик НЕ закрывает,
    ack сохранён (finding #98). До фикса close_incident стирал ack каждый тик, и
    порог пушил заново после cooldown."""
    node = make_node(
        db_session, name="crowd-live", host="203.0.113.60",
        status=models.VPNNodeStatus.active,
    )
    nid = node.id
    now = utcnow()
    open_at = now - timedelta(minutes=10)
    node.diagnose_incident_open_at = open_at
    node.diagnose_acked_at = open_at + timedelta(minutes=1)   # оператор нажал «вижу»
    node.cooldown_until = now + timedelta(hours=1)            # выведена крауд-порогом
    node.unreachable_since = None
    db_session.commit()

    _patch_tick_env(monkeypatch)
    s = worker.run_node_reachability_tick()
    db_session.expire_all()

    n = db_session.get(models.VPNNode, nid)
    # ssh_ok прошёл, но инцидент и ack на месте — НЕ recovered/reconciled.
    assert n.diagnose_incident_open_at is not None
    assert n.diagnose_acked_at is not None
    assert f"node:{nid}" not in s["recovered"]
    assert f"node:{nid}" not in s["reconciled"]


def test_plain_incident_still_closes_on_ssh_ok(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контроль: обычный reachability-инцидент (не крауд, оператор не трогал) по
    ssh_ok закрывается как раньше — гвард не ломает штатное восстановление."""
    node = make_node(
        db_session, name="plain-recover", host="203.0.113.61",
        status=models.VPNNodeStatus.active,
    )
    nid = node.id
    now = utcnow()
    node.diagnose_incident_open_at = now - timedelta(minutes=10)
    node.diagnose_acked_at = None
    node.diagnose_follow_mode = None
    node.cooldown_until = None
    node.unreachable_since = now - timedelta(minutes=10)
    db_session.commit()

    _patch_tick_env(monkeypatch)
    s = worker.run_node_reachability_tick()
    db_session.expire_all()

    n = db_session.get(models.VPNNode, nid)
    assert n.diagnose_incident_open_at is None
    assert f"node:{nid}" in s["recovered"]
