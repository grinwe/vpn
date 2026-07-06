"""Тесты защиты от дублей при создании рассылки (finding #43).

Двойной клик по Send / ретрай HTTP-клиента не должен создавать две
одинаковые queued-рассылки — иначе dispatch-тик разошлёт обе всей базе.
"""
from __future__ import annotations

from app import models


def _make_user(db_session, i: int) -> models.User:
    u = models.User(telegram_id=f"dup-user-{i}", email=f"dup{i}@x")
    db_session.add(u)
    db_session.commit()
    return u


def test_duplicate_create_rejected_with_409(client, db_session) -> None:
    _make_user(db_session, 1)
    payload = {"text": "Одинаковый текст", "target_filter": {"type": "all"}}

    r1 = client.post("/api/broadcasts", json=payload)
    assert r1.status_code == 201, r1.text
    first_id = r1.json()["id"]

    # Повторный идентичный вызов (двойной клик) отклоняется 409,
    # id уже существующей рассылки — в detail.
    r2 = client.post("/api/broadcasts", json=payload)
    assert r2.status_code == 409, r2.text
    assert str(first_id) in r2.json()["detail"]

    # В базе ровно одна рассылка.
    rows = db_session.query(models.Broadcast).all()
    assert len(rows) == 1


def test_different_text_not_deduped(client, db_session) -> None:
    _make_user(db_session, 2)
    r1 = client.post(
        "/api/broadcasts",
        json={"text": "Текст A", "target_filter": {"type": "all"}},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/broadcasts",
        json={"text": "Текст B", "target_filter": {"type": "all"}},
    )
    assert r2.status_code == 201


def test_different_filter_not_deduped(client, db_session) -> None:
    u = _make_user(db_session, 3)
    payload_all = {"text": "Тот же текст", "target_filter": {"type": "all"}}
    payload_ids = {
        "text": "Тот же текст",
        "target_filter": {"type": "ids", "ids": [u.id]},
    }
    r1 = client.post("/api/broadcasts", json=payload_all)
    assert r1.status_code == 201
    r2 = client.post("/api/broadcasts", json=payload_ids)
    assert r2.status_code == 201


def test_dedup_only_against_active_statuses(client, db_session) -> None:
    _make_user(db_session, 4)
    payload = {"text": "Повтор после отмены", "target_filter": {"type": "all"}}

    r1 = client.post("/api/broadcasts", json=payload)
    assert r1.status_code == 201
    first_id = r1.json()["id"]

    # Отменяем первую — она уходит из queued/sending, дедуп больше
    # не должен её учитывать.
    rc = client.post(f"/api/broadcasts/{first_id}/cancel")
    assert rc.status_code == 200, rc.text

    r2 = client.post("/api/broadcasts", json=payload)
    assert r2.status_code == 201, r2.text
