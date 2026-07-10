"""Хвосты аудита 2026-07 — допиленные после верификации волн фиксов.

Покрывает:
  * #70 (хвост) — sweep_stuck_spawns реально привязан к worker-тику:
    run_spawn_sweep_tick + enqueue_spawn_finalize + resume_stuck_spawn
    (достройка едет персистентной RQ-джобой, не daemon-потоком).
  * net-audit #97 (хвост) — reachability-тик не метит цель DOWN при
    ssh_skipped-пробе (нет paramiko/ключа = конфиг-ошибка контроллера,
    а не недоступность хоста) + агрегированный алерт.
  * #78 (хвост) — reinstall инвалидирует warm-пул ноды: строки pool_state=warm
    при стёртом диске — мёртвые бандлы, try_assign_bundle их выдавать не должен.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import models
from app import queue as app_queue
from app import worker
from app.services import node_spawner
from app.services.node_spawner import SPAWN_PLACEHOLDER_HOST
from app.time_utils import utcnow
from tests.factories import make_node, make_provider


@pytest.fixture(autouse=True)
def _no_reschedule(monkeypatch: pytest.MonkeyPatch):
    """Тики не должны реально планировать себя в RQ из тестов."""
    monkeypatch.setattr(app_queue, "schedule_tick", lambda *a, **k: None)


def _stuck_node(
    db: Session, *, name: str = "stuck-1", external_id: str | None = "ext-1"
) -> models.VPNNode:
    provider = make_provider(db, name=f"prov-{name}")
    node = make_node(
        db, name=name, host=SPAWN_PLACEHOLDER_HOST,
        status=models.VPNNodeStatus.registering,
    )
    node.provider_id = provider.id
    node.provider_external_id = external_id
    # Старше порога NODE_SPAWN_STUCK_MINUTES (30) — кандидат свипа.
    node.updated_at = utcnow() - timedelta(hours=2)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


class _ResumableDriver:
    """Драйвер с capability wait_for_ipv4 — заказ возобновляем."""

    def wait_for_ipv4(self, *a, **k):  # pragma: no cover — не должен зваться
        raise AssertionError("sweep в queue-режиме не должен звать драйвер")


# ── #70 — sweep в queue-режиме ставит RQ-джобу, не поток ─────────────────
def test_sweep_enqueues_job_and_bumps_updated_at(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _stuck_node(db_session)
    monkeypatch.setattr(
        node_spawner, "get_driver", lambda provider: _ResumableDriver()
    )
    calls: list[tuple[str, int]] = []

    def _enqueue(kind: str, entity_id: int) -> str:
        calls.append((kind, entity_id))
        return f"spawn-finalize-{kind}-{entity_id}"

    before = node.updated_at
    out = node_spawner.sweep_stuck_spawns(db_session, enqueue=_enqueue)

    assert calls == [("node", node.id)]
    assert out["relay_resumed"] == 1
    assert out["enqueue_failed"] == 0
    db_session.refresh(node)
    # updated_at сдвинут — следующий тик не откроет вторую достройку.
    assert node.updated_at > before
    # Статус НЕ трогаем — финализирует RQ-джоба.
    assert node.status == models.VPNNodeStatus.registering


def test_sweep_enqueue_failure_leaves_node_stuck(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Очередь недоступна (enqueue → None): updated_at НЕ двигаем, нода
    остаётся кандидатом для следующего тика — достройка не теряется молча."""
    node = _stuck_node(db_session, name="stuck-2", external_id="ext-2")
    monkeypatch.setattr(
        node_spawner, "get_driver", lambda provider: _ResumableDriver()
    )
    before = node.updated_at

    out = node_spawner.sweep_stuck_spawns(db_session, enqueue=lambda k, i: None)

    assert out["enqueue_failed"] == 1
    assert out["relay_resumed"] == 0
    db_session.refresh(node)
    assert node.updated_at == before


def test_spawn_sweep_tick_wires_persistent_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тик передаёт свипу именно enqueue_spawn_finalize (RQ-джоба), а не
    None (поток умер бы вместе с work-horse сразу после return тика)."""
    captured: dict = {}

    def _fake_sweep(session, *, enqueue=None):
        captured["enqueue"] = enqueue
        return {"relay_resumed": 0}

    monkeypatch.setattr(node_spawner, "sweep_stuck_spawns", _fake_sweep)
    summary = worker.run_spawn_sweep_tick()

    assert summary == {"relay_resumed": 0}
    assert captured["enqueue"] is app_queue.enqueue_spawn_finalize


def test_spawn_sweep_tick_registered_in_queue_maps() -> None:
    """Без записи в TICK_IDS/TICK_TIMEOUTS тик не дедупится и живёт без
    hard-timeout — обе карты обязаны знать tick-spawn-sweep."""
    assert (
        app_queue.TICK_IDS["app.worker.run_spawn_sweep_tick"]
        == "tick-spawn-sweep"
    )
    assert "tick-spawn-sweep" in app_queue.TICK_TIMEOUTS


def test_resume_stuck_spawn_guards(db_session: Session) -> None:
    """Идемпотентность RQ-джобы: чужой id / уже финализированная нода → no-op."""
    assert node_spawner.resume_stuck_spawn("node", 999_999) == {
        "resumed": False, "reason": "not_found",
    }

    done = make_node(db_session, name="done-1", host="198.51.100.77")
    assert node_spawner.resume_stuck_spawn("node", done.id) == {
        "resumed": False, "reason": "already_finalized",
    }

    orphan = _stuck_node(db_session, name="orphan-1", external_id=None)
    assert node_spawner.resume_stuck_spawn("node", orphan.id) == {
        "resumed": False, "reason": "not_resumable",
    }


def test_run_spawn_finalize_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        node_spawner, "resume_stuck_spawn",
        lambda kind, entity_id: calls.append((kind, entity_id)) or {"resumed": True},
    )
    assert worker.run_spawn_finalize("exit", 7) == {"resumed": True}
    assert calls == [("exit", 7)]


# ── net-audit #97 — ssh_skipped не кладёт флот в ложный DOWN ─────────────
def test_reachability_ssh_skipped_is_not_down(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import admin_notify, diagnostics

    node = make_node(db_session, name="skip-1", host="198.51.100.88")
    monkeypatch.setattr(worker, "_controller_has_network", lambda: True)

    def _skipped_probe(host, **kwargs):
        return diagnostics.PathProbeResult(
            checks=[], ping_ok=False, ssh_ok=False, ssh_skipped=True,
            summary="ssh skip",
        )

    monkeypatch.setattr(diagnostics, "run_local_path_probe", _skipped_probe)
    alerts: list[str] = []
    monkeypatch.setattr(
        admin_notify, "notify_admins",
        lambda session, *, kind, **kw: alerts.append(kind),
    )

    summary = worker.run_node_reachability_tick()

    assert summary["ssh_skipped"] == [f"node:{node.id}"]
    assert summary["down"] == []
    assert summary["diagnosed"] == []
    db_session.expire_all()
    fresh = db_session.get(models.VPNNode, node.id)
    # Проба недиагностична: ни статус, ни серию, ни last_probe_at не трогаем.
    assert fresh.last_probe_status is None
    assert fresh.unreachable_since is None
    assert fresh.last_probe_at is None
    # Один агрегированный алерт «мониторинг слеп», а не шторм пер-нодовых.
    assert alerts == ["reachability_ssh_skipped"]


def test_reachability_real_down_still_detected(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контроль: ssh_skipped=False + ssh_ok=False — прежний DOWN-путь жив."""
    from app.services import admin_notify, diagnostics

    node = make_node(db_session, name="down-1", host="198.51.100.89")
    monkeypatch.setattr(worker, "_controller_has_network", lambda: True)
    monkeypatch.setattr(
        diagnostics, "run_local_path_probe",
        lambda host, **kw: diagnostics.PathProbeResult(
            checks=[], ping_ok=False, ssh_ok=False, ssh_skipped=False,
            summary="down",
        ),
    )
    monkeypatch.setattr(
        admin_notify, "notify_admins", lambda session, **kw: None
    )

    summary = worker.run_node_reachability_tick()

    assert summary["down"] == [f"node:{node.id}"]
    assert "ssh_skipped" not in summary
    db_session.expire_all()
    fresh = db_session.get(models.VPNNode, node.id)
    assert fresh.last_probe_status == "unreachable"
    assert fresh.unreachable_since is not None


# ── #78 (хвост) — reinstall инвалидирует warm-пул ноды ───────────────────
class _ReinstallDriver:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def reinstall_server(self, external_id, image, *, password=None):
        self.calls.append((external_id, image))


def test_reinstall_invalidates_warm_pool(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = make_provider(db_session, name="prov-reinst")
    node = make_node(db_session, name="reinst-1", host="198.51.100.90")
    node.provider_id = provider.id
    node.provider_external_id = "srv-42"
    db_session.add(node)

    warm = models.Credential(
        node_id=node.id,
        proto=models.VPNConfigProtocol.hysteria2.value,
        config_text="hy2://secret@198.51.100.90:443/",
        pool_state=models.CredentialPoolState.warm,
        access_username="warm-u1",
        is_active=True,
    )
    db_session.add(warm)
    db_session.commit()

    driver = _ReinstallDriver()
    monkeypatch.setattr(node_spawner, "get_driver", lambda p: driver)
    # Фоновая достройка (ждёт SSH) в юнит-тесте не нужна.
    monkeypatch.setattr(node_spawner, "_reinstall_finalize", lambda *a, **k: None)

    node, _task = node_spawner.reinstall_node(db_session, node)

    assert driver.calls, "reinstall_server должен был вызваться"
    db_session.expire_all()
    fresh = db_session.get(models.Credential, warm.id)
    # Диск стёрт → warm-строка обязана уйти в revoked, иначе try_assign_bundle
    # выдаст юзеру бандл с мёртвым hy2-легом.
    assert fresh.pool_state == models.CredentialPoolState.revoked
    assert fresh.is_active is False


def test_reinstall_keeps_pool_when_provider_call_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-вызов reinstall упал → диск цел, warm-пул трогать нельзя."""
    from app.services.cloud import DriverError

    provider = make_provider(db_session, name="prov-reinst2")
    node = make_node(db_session, name="reinst-2", host="198.51.100.91")
    node.provider_id = provider.id
    node.provider_external_id = "srv-43"
    db_session.add(node)
    warm = models.Credential(
        node_id=node.id,
        proto=models.VPNConfigProtocol.hysteria2.value,
        config_text="hy2://secret@198.51.100.91:443/",
        pool_state=models.CredentialPoolState.warm,
        access_username="warm-u2",
        is_active=True,
    )
    db_session.add(warm)
    db_session.commit()

    class _FailingDriver:
        def reinstall_server(self, *a, **k):
            raise DriverError("api down")

    monkeypatch.setattr(node_spawner, "get_driver", lambda p: _FailingDriver())

    with pytest.raises(node_spawner.NodeSpawnError):
        node_spawner.reinstall_node(db_session, node)

    db_session.expire_all()
    fresh = db_session.get(models.Credential, warm.id)
    assert fresh.pool_state == models.CredentialPoolState.warm
