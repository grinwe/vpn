"""run_node_reachability_tick — confirm-окно перед алертом о недоступности.

Анти-спам: единичный непрошедший probe НЕ будит админа. Алерт (диагностика +
пуш) только когда недоступность подтверждена серией >= NODE_ALERT_CONFIRM_MIN.
Recovery чистит unreachable_since → транзиентный блип не накапливается.
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


def _down_probe(host, **k):
    return diagnostics.PathProbeResult(
        checks=[], ping_ok=False, ssh_ok=False, summary="down"
    )


def _ok_probe(host, **k):
    return diagnostics.PathProbeResult(
        checks=[], ping_ok=True, ssh_ok=True, summary="ok"
    )


def test_reachability_confirm_window(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = make_node(
        db_session, name="reach-1", host="203.0.113.50",
        status=models.VPNNodeStatus.active,
    )
    nid = node.id

    monkeypatch.setenv("NODE_REACHABILITY_ENABLED", "true")
    monkeypatch.setenv("NODE_ALERT_CONFIRM_MIN", "20")
    monkeypatch.setattr(app_queue, "schedule_tick", lambda *a, **k: None)
    pushes: list = []
    monkeypatch.setattr(
        admin_notify,
        "notify_node_diagnosis",
        lambda *a, **k: (pushes.append(k.get("target")), [])[1],
    )
    monkeypatch.setattr(
        ProvisioningOrchestrator, "create_task",
        lambda self, *a, **k: types.SimpleNamespace(id=1),
    )
    monkeypatch.setattr(
        ProvisioningOrchestrator, "run_task_async", lambda self, *a, **k: None
    )
    monkeypatch.setattr(diagnostics, "run_local_path_probe", _down_probe)

    # ── tick 1: первый DOWN → suspect, НЕ алертим ──
    s1 = worker.run_node_reachability_tick()
    db_session.expire_all()
    n = db_session.get(models.VPNNode, nid)
    assert n.unreachable_since is not None
    assert n.diagnose_incident_open_at is None      # инцидент НЕ открыт
    assert pushes == []                             # пуша нет
    assert f"node:{nid}" in s1["suspect"]

    # ── недоступность держится >20 мин (бэкдейтим начало серии) ──
    n.unreachable_since = utcnow() - timedelta(minutes=25)
    db_session.commit()

    # ── tick 2: подтверждено → диагностика + пуш + открытие инцидента ──
    s2 = worker.run_node_reachability_tick()
    db_session.expire_all()
    n = db_session.get(models.VPNNode, nid)
    assert n.diagnose_incident_open_at is not None  # инцидент открыт
    assert len(pushes) == 1                         # ровно один пуш
    assert f"node:{nid}" in s2["diagnosed"]

    # ── recovery: ssh_ok → серия и инцидент сброшены ──
    monkeypatch.setattr(diagnostics, "run_local_path_probe", _ok_probe)
    worker.run_node_reachability_tick()
    db_session.expire_all()
    n = db_session.get(models.VPNNode, nid)
    assert n.unreachable_since is None
    assert n.diagnose_incident_open_at is None


def test_reachability_backstop_reconcile(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Бэкстоп закрывает рассинхрон «инцидент открыт + probe ok», когда штатное
    закрытие на ssh_ok было пропущено. Probe держим DOWN — закрыть может ТОЛЬКО
    бэкстоп (до бюджетного цикла), не recovery-ветка ssh_ok."""
    node = make_node(
        db_session, name="reach-recon", host="203.0.113.51",
        status=models.VPNNodeStatus.active,
    )
    nid = node.id

    monkeypatch.setenv("NODE_REACHABILITY_ENABLED", "true")
    monkeypatch.setenv("NODE_ALERT_CONFIRM_MIN", "20")
    monkeypatch.setenv("NODE_INCIDENT_RECONCILE_MAX_AGE_MIN", "30")
    monkeypatch.setattr(app_queue, "schedule_tick", lambda *a, **k: None)
    monkeypatch.setattr(admin_notify, "notify_node_diagnosis", lambda *a, **k: [])
    monkeypatch.setattr(
        ProvisioningOrchestrator, "create_task",
        lambda self, *a, **k: types.SimpleNamespace(id=1),
    )
    monkeypatch.setattr(
        ProvisioningOrchestrator, "run_task_async", lambda self, *a, **k: None
    )
    monkeypatch.setattr(diagnostics, "run_local_path_probe", _down_probe)

    # Рассинхрон: инцидент открыт, но последний probe == ok, серии падений нет,
    # телеметрия свежая → бэкстоп закрывает.
    node.diagnose_incident_open_at = utcnow() - timedelta(minutes=5)
    node.last_probe_status = "ok"
    node.last_probe_at = utcnow()
    node.unreachable_since = None
    db_session.commit()

    s = worker.run_node_reachability_tick()
    db_session.expire_all()
    n = db_session.get(models.VPNNode, nid)
    assert f"node:{nid}" in s["reconciled"]
    assert n.diagnose_incident_open_at is None

    # Протухшая телеметрия (last_probe_at старше max_age) → бэкстоп НЕ трогает.
    n.diagnose_incident_open_at = utcnow() - timedelta(minutes=5)
    n.last_probe_status = "ok"
    n.last_probe_at = utcnow() - timedelta(minutes=90)
    n.unreachable_since = None
    db_session.commit()

    s2 = worker.run_node_reachability_tick()
    db_session.expire_all()
    n = db_session.get(models.VPNNode, nid)
    assert f"node:{nid}" not in s2["reconciled"]
    assert n.diagnose_incident_open_at is not None
