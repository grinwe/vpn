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
