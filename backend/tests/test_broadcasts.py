"""Тесты для admin broadcast-фичи.

Покрываем:
  * POST /api/broadcasts создаёт row с заполненным total_recipients,
    target_filter, text. Валидация: text>4000 → 422, ids>5000 → 422.
  * POST /api/broadcasts/preview считает recipient_count по фильтру.
  * GET /api/broadcasts (list) и /api/broadcasts/{id} (detail).
  * POST /api/broadcasts/{id}/cancel в нужных статусах.
  * Dispatch-тик `run_broadcast_dispatch_tick`:
      - заливает AuditLog(admin_broadcast) по N юзерам;
      - продвигает last_user_id_cursor;
      - финализирует в completed когда фильтр иссяк;
      - уважает cancelled — не пишет ничего.
"""
from __future__ import annotations

from typing import Any

import pytest

from app import models
from app.worker import run_broadcast_dispatch_tick

from .factories import (
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


# ──────────────────────────────────────────────────────────────
# API tests
# ──────────────────────────────────────────────────────────────


def _create_user_batch(db_session, n: int, *, tg_prefix: str = "user-") -> list[models.User]:
    out: list[models.User] = []
    for i in range(n):
        u = make_user(db_session, telegram_id=f"{tg_prefix}{i}")
        out.append(u)
    return out


def test_preview_counts_all_users_with_telegram(
    client, db_session
) -> None:
    _create_user_batch(db_session, 5)
    # Юзер без telegram_id — не должен попадать в all/active.
    orphan = models.User(telegram_id=None, email="x@x")
    db_session.add(orphan)
    db_session.commit()

    r = client.post(
        "/api/broadcasts/preview", json={"target_filter": {"type": "all"}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["recipient_count"] == 5


def test_preview_active_only_counts_subscribers(
    client, db_session
) -> None:
    users = _create_user_batch(db_session, 3)
    node = make_node(db_session)
    plan = make_plan(db_session)
    # Два юзера с active sub, один без.
    make_subscription(db_session, users[0], plan, node)
    make_subscription(db_session, users[1], plan, node)

    r = client.post(
        "/api/broadcasts/preview",
        json={"target_filter": {"type": "active"}},
    )
    assert r.status_code == 200
    assert r.json()["recipient_count"] == 2


def test_preview_ids_returns_exact_count(client, db_session) -> None:
    users = _create_user_batch(db_session, 4)
    ids = [users[0].id, users[2].id]
    r = client.post(
        "/api/broadcasts/preview",
        json={"target_filter": {"type": "ids", "ids": ids}},
    )
    assert r.status_code == 200
    assert r.json()["recipient_count"] == 2


def test_preview_ids_empty_list_returns_zero(
    client, db_session
) -> None:
    _create_user_batch(db_session, 3)
    r = client.post(
        "/api/broadcasts/preview",
        json={"target_filter": {"type": "ids", "ids": []}},
    )
    assert r.status_code == 200
    assert r.json()["recipient_count"] == 0


def test_create_broadcast_persists_row(client, db_session) -> None:
    _create_user_batch(db_session, 3)

    r = client.post(
        "/api/broadcasts",
        json={
            "text": "Привет!",
            "target_filter": {"type": "all"},
        },
    )
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["text"] == "Привет!"
    assert payload["status"] == "queued"
    assert payload["total_recipients"] == 3
    assert payload["sent_count"] == 0
    assert payload["last_user_id_cursor"] == 0
    assert payload["target_filter"] == {"type": "all"}

    db_session.expire_all()
    bc = db_session.get(models.Broadcast, payload["id"])
    assert bc is not None
    assert bc.status == models.BroadcastStatus.queued


def test_create_broadcast_rejects_oversize_text(
    client, db_session
) -> None:
    _create_user_batch(db_session, 1)
    r = client.post(
        "/api/broadcasts",
        json={
            "text": "x" * 4001,
            "target_filter": {"type": "all"},
        },
    )
    assert r.status_code == 422


def test_create_broadcast_rejects_ids_overflow(
    client, db_session
) -> None:
    r = client.post(
        "/api/broadcasts",
        json={
            "text": "x",
            "target_filter": {"type": "ids", "ids": list(range(5001))},
        },
    )
    assert r.status_code == 422


def test_list_and_get_broadcast(client, db_session) -> None:
    _create_user_batch(db_session, 1)
    r = client.post(
        "/api/broadcasts",
        json={"text": "t", "target_filter": {"type": "all"}},
    )
    bc_id = r.json()["id"]

    r2 = client.get("/api/broadcasts")
    assert r2.status_code == 200
    data = r2.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == bc_id

    r3 = client.get(f"/api/broadcasts/{bc_id}")
    assert r3.status_code == 200
    assert r3.json()["id"] == bc_id


def test_get_missing_broadcast_returns_404(client) -> None:
    r = client.get("/api/broadcasts/999999")
    assert r.status_code == 404


def test_cancel_queued_broadcast(client, db_session) -> None:
    _create_user_batch(db_session, 1)
    r = client.post(
        "/api/broadcasts",
        json={"text": "t", "target_filter": {"type": "all"}},
    )
    bc_id = r.json()["id"]

    r2 = client.post(
        f"/api/broadcasts/{bc_id}/cancel", json={"reason": "mistake"}
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_reason"] == "mistake"


def test_cancel_completed_broadcast_returns_409(
    client, db_session
) -> None:
    _create_user_batch(db_session, 1)
    r = client.post(
        "/api/broadcasts",
        json={"text": "t", "target_filter": {"type": "all"}},
    )
    bc_id = r.json()["id"]
    # Завершим вручную и попробуем отменить.
    bc = db_session.get(models.Broadcast, bc_id)
    bc.status = models.BroadcastStatus.completed
    db_session.commit()

    r2 = client.post(f"/api/broadcasts/{bc_id}/cancel")
    assert r2.status_code == 409


# ──────────────────────────────────────────────────────────────
# Dispatch tick tests
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def stub_schedule_tick(monkeypatch: pytest.MonkeyPatch):
    """Тик вызывает schedule_tick в самом начале — в тестах Redis
    не поднят, просто no-op'аем чтобы не упасть."""
    import app.queue as q

    def _noop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(q, "schedule_tick", _noop)
    # worker импортирует schedule_tick локально внутри функции —
    # патчим и там (если уже импорт кешировался, monkeypatch сработает).
    from app import worker as w

    monkeypatch.setattr(w, "schedule_tick", _noop, raising=False)


def _count_broadcast_audits(db_session, bc_id: int) -> int:
    return (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.action == "admin_broadcast",
            models.AuditLog.target_id == bc_id,
        )
        .count()
    )


def test_dispatch_tick_writes_audit_per_user_and_advances_cursor(
    db_session, stub_schedule_tick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BROADCAST_BATCH_SIZE", "50")
    users = _create_user_batch(db_session, 3)

    bc = models.Broadcast(
        created_by="admin",
        text="hi",
        target_filter={"type": "all"},
        status=models.BroadcastStatus.queued,
    )
    db_session.add(bc)
    db_session.commit()

    summary: dict[str, Any] = run_broadcast_dispatch_tick()
    assert summary["sent"] == 3

    db_session.expire_all()
    bc = db_session.get(models.Broadcast, bc.id)
    assert bc.status == models.BroadcastStatus.sending
    assert bc.sent_count == 3
    assert bc.last_user_id_cursor == users[-1].id
    assert bc.started_at is not None

    assert _count_broadcast_audits(db_session, bc.id) == 3


def test_dispatch_tick_completes_when_filter_empty(
    db_session, stub_schedule_tick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BROADCAST_BATCH_SIZE", "50")
    _create_user_batch(db_session, 2)

    bc = models.Broadcast(
        created_by="admin",
        text="hi",
        target_filter={"type": "all"},
        status=models.BroadcastStatus.queued,
    )
    db_session.add(bc)
    db_session.commit()

    # Первый тик отправит обоим юзерам.
    run_broadcast_dispatch_tick()
    # Второй тик — подкурсор, юзеров нет → completed.
    run_broadcast_dispatch_tick()

    db_session.expire_all()
    bc = db_session.get(models.Broadcast, bc.id)
    assert bc.status == models.BroadcastStatus.completed
    assert bc.completed_at is not None
    assert bc.sent_count == 2


def test_dispatch_tick_respects_batch_size(
    db_session, stub_schedule_tick, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BROADCAST_BATCH_SIZE", "2")
    _create_user_batch(db_session, 5)

    bc = models.Broadcast(
        created_by="admin",
        text="hi",
        target_filter={"type": "all"},
        status=models.BroadcastStatus.queued,
    )
    db_session.add(bc)
    db_session.commit()

    run_broadcast_dispatch_tick()

    db_session.expire_all()
    bc = db_session.get(models.Broadcast, bc.id)
    assert bc.sent_count == 2
    assert bc.status == models.BroadcastStatus.sending
    assert _count_broadcast_audits(db_session, bc.id) == 2


def test_dispatch_tick_skips_cancelled(
    db_session, stub_schedule_tick, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_user_batch(db_session, 3)

    bc = models.Broadcast(
        created_by="admin",
        text="hi",
        target_filter={"type": "all"},
        status=models.BroadcastStatus.cancelled,
    )
    db_session.add(bc)
    db_session.commit()

    summary = run_broadcast_dispatch_tick()
    assert summary["sent"] == 0
    assert _count_broadcast_audits(db_session, bc.id) == 0


def test_dispatch_tick_marks_broken_filter_as_failed(
    db_session, stub_schedule_tick
) -> None:
    _create_user_batch(db_session, 1)
    bc = models.Broadcast(
        created_by="admin",
        text="hi",
        target_filter={"type": "NOPE"},
        status=models.BroadcastStatus.queued,
    )
    db_session.add(bc)
    db_session.commit()

    run_broadcast_dispatch_tick()

    db_session.expire_all()
    bc = db_session.get(models.Broadcast, bc.id)
    assert bc.status == models.BroadcastStatus.failed
    assert bc.completed_at is not None


def test_dispatch_tick_processes_one_broadcast_per_tick(
    db_session, stub_schedule_tick, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Два broadcast-а в очереди: тик берёт один, второй остаётся queued."""
    monkeypatch.setenv("BROADCAST_BATCH_SIZE", "50")
    _create_user_batch(db_session, 2)

    b1 = models.Broadcast(
        created_by="admin",
        text="first",
        target_filter={"type": "all"},
        status=models.BroadcastStatus.queued,
    )
    b2 = models.Broadcast(
        created_by="admin",
        text="second",
        target_filter={"type": "all"},
        status=models.BroadcastStatus.queued,
    )
    db_session.add_all([b1, b2])
    db_session.commit()

    run_broadcast_dispatch_tick()

    db_session.expire_all()
    b1 = db_session.get(models.Broadcast, b1.id)
    b2 = db_session.get(models.Broadcast, b2.id)
    assert b1.status == models.BroadcastStatus.sending
    assert b1.sent_count == 2
    # Второй не трогаем, пока первый не закроется.
    assert b2.status == models.BroadcastStatus.queued
    assert b2.sent_count == 0
