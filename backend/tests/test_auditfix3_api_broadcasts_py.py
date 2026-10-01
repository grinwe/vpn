"""Регресс на находку id=46.

`GET /api/broadcasts?status=<мусор>` раньше отдавал строку напрямую в
native-enum колонку → ошибка драйвера «invalid input value for enum» → 500.
Теперь строка конвертируется в BroadcastStatus, а невалидное значение даёт 400.
Валидный статус по-прежнему фильтрует корректно.
"""
from __future__ import annotations

from app import models


def _create_users(db_session, n: int) -> None:
    from .factories import make_user

    for i in range(n):
        make_user(db_session, telegram_id=f"bc46-{i}")


def test_list_broadcasts_invalid_status_returns_400(client, db_session) -> None:
    r = client.get("/api/broadcasts", params={"status": "bogus"})
    assert r.status_code == 400, r.text


def test_list_broadcasts_valid_status_filters(client, db_session) -> None:
    _create_users(db_session, 1)
    r = client.post(
        "/api/broadcasts",
        json={"text": "t", "target_filter": {"type": "all"}},
    )
    assert r.status_code == 201, r.text
    # Созданная рассылка в статусе queued.
    r_queued = client.get("/api/broadcasts", params={"status": "queued"})
    assert r_queued.status_code == 200, r_queued.text
    assert r_queued.json()["total"] == 1

    # Другой валидный статус — пусто, но не ошибка.
    r_completed = client.get(
        "/api/broadcasts", params={"status": models.BroadcastStatus.completed.value}
    )
    assert r_completed.status_code == 200, r_completed.text
    assert r_completed.json()["total"] == 0
