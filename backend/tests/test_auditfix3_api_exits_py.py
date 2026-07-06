"""Находка #35 — одиночные exit-операции не должны отдавать 500 после
успешного изменения БД, если очередь (Redis) недоступна.

``orchestrator.run_task_async(task)`` кидает RuntimeError при недоступном
Redis (без ALLOW_INPROCESS_PROVISIONING). Раньше в single-эндпоинтах
(create_exit / rebootstrap / diagnose / attach / detach / reconnect /
evacuate) этот вызов шёл ПОСЛЕ commit'а и без try/except: клиент получал
500, хотя exit/link уже создан/удалён, а task лежал в ``pending`` (его
подберёт pending-rescue-tick). Админ считал операцию проваленной, ретраил
и ловил 409 (dup attach) или 404 (detach).

Фикс: общий хелпер ``_run_task_best_effort`` логирует сбой и возвращает
``False`` (как это давно делают batch-эндпоинты). Dict-ответы несут
``task_enqueued`` — признак, что таска в pending и её подхватит rescue-tick.
"""
from __future__ import annotations

from app.services.provisioning import ProvisioningOrchestrator


def _raise_queue_down(self, task, node=None):  # type: ignore[no-untyped-def]
    """Симуляция недоступного Redis: run_task_async кидает RuntimeError.

    Перекрывает noop-патч из autouse-фикстуры ``_no_provisioning``
    (наш monkeypatch применяется позже → выигрывает).
    """
    raise RuntimeError("provisioning queue is unavailable (Redis down)")


_EXIT_PAYLOAD = {
    "name": "af35-exit",
    "region": "nl",
    "host": "198.51.100.35",
    "wg_address_v4": "10.77.0.1/24",
}


def test_create_exit_survives_queue_down(client, db_session, monkeypatch):
    """POST /exits при мёртвой очереди → 200 (не 500), exit персиститься."""
    monkeypatch.setattr(
        ProvisioningOrchestrator, "run_task_async", _raise_queue_down
    )

    resp = client.post("/api/exits", json=_EXIT_PAYLOAD)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "af35-exit"

    # Exit реально в БД — операция не откатилась из-за сбоя enqueue.
    from app import models

    row = (
        db_session.query(models.WGExitNode)
        .filter(models.WGExitNode.name == "af35-exit")
        .first()
    )
    assert row is not None
    # bootstrap-таска создана и лежит в pending — rescue-tick её подберёт.
    task = (
        db_session.query(models.ProvisioningTask)
        .filter(
            models.ProvisioningTask.target_type == "exit",
            models.ProvisioningTask.target_id == row.id,
        )
        .first()
    )
    assert task is not None


def test_rebootstrap_reports_task_not_enqueued_on_queue_down(
    client, monkeypatch
):
    """rebootstrap: очередь мертва → 200 + task_enqueued=false."""
    created = client.post("/api/exits", json={**_EXIT_PAYLOAD, "name": "af35-rb"})
    assert created.status_code == 200, created.text
    exit_id = created.json()["id"]

    monkeypatch.setattr(
        ProvisioningOrchestrator, "run_task_async", _raise_queue_down
    )
    resp = client.post(f"/api/exits/{exit_id}/bootstrap")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] is not None
    assert body["task_enqueued"] is False


def test_rebootstrap_task_enqueued_true_when_queue_up(client):
    """Happy path (noop-очередь из conftest) → task_enqueued=true."""
    created = client.post("/api/exits", json={**_EXIT_PAYLOAD, "name": "af35-ok"})
    assert created.status_code == 200, created.text
    exit_id = created.json()["id"]

    resp = client.post(f"/api/exits/{exit_id}/bootstrap")
    assert resp.status_code == 200, resp.text
    assert resp.json()["task_enqueued"] is True


def test_diagnose_exit_survives_queue_down(client, monkeypatch):
    """diagnose: мёртвая очередь → 200 + task_enqueued=false (не 500)."""
    created = client.post("/api/exits", json={**_EXIT_PAYLOAD, "name": "af35-dg"})
    assert created.status_code == 200, created.text
    exit_id = created.json()["id"]

    monkeypatch.setattr(
        ProvisioningOrchestrator, "run_task_async", _raise_queue_down
    )
    resp = client.post(f"/api/exits/{exit_id}/diagnose")
    assert resp.status_code == 200, resp.text
    assert resp.json()["task_enqueued"] is False
