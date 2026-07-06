"""Аудит-фикс в backend/app/api/tasks.py (находка 40).

POST /api/provisioning/tasks/batch раньше принимал сырой ``body: dict``:
список ids был не типизирован и не ограничен по длине. Строка вместо
числа доезжала до ``db.get`` и роняла запрос 500-кой посреди батча
(часть аудит-коммитов уже прошла), а гигантский список крутил тысячи
одиночных ``db.get``. Теперь тело — Pydantic-модель BatchTasksRequest
с ``ids: list[int] = Field(min_length=1, max_length=500)`` и
``action: Literal["delete", "rerun"]``, так что мусор отсекается на
валидации (422) ещё до БД, а не 500-кой в середине.
"""
from __future__ import annotations


def test_batch_tasks_rejects_non_int_id_before_db(client) -> None:
    # Строка вместо числа: раньше доехала бы до db.get → 500. Теперь 422.
    resp = client.post(
        "/api/provisioning/tasks/batch",
        json={"ids": ["oops"], "action": "delete"},
    )
    assert resp.status_code == 422


def test_batch_tasks_rejects_empty_ids(client) -> None:
    resp = client.post(
        "/api/provisioning/tasks/batch",
        json={"ids": [], "action": "delete"},
    )
    assert resp.status_code == 422


def test_batch_tasks_rejects_oversized_ids(client) -> None:
    # 501 > max_length=500 → отсекается на валидации, а не крутит батч.
    resp = client.post(
        "/api/provisioning/tasks/batch",
        json={"ids": list(range(1, 502)), "action": "delete"},
    )
    assert resp.status_code == 422


def test_batch_tasks_rejects_unknown_action(client) -> None:
    resp = client.post(
        "/api/provisioning/tasks/batch",
        json={"ids": [1], "action": "nuke"},
    )
    assert resp.status_code == 422


def test_batch_tasks_valid_payload_reports_not_found(client) -> None:
    # Валидное тело с несуществующим id: проходит валидацию, доходит до
    # хендлера и штатно попадает в not_found (без 500).
    resp = client.post(
        "/api/provisioning/tasks/batch",
        json={"ids": [999999999], "action": "delete"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["not_found"] == [999999999]
    assert body["ok"] == []
