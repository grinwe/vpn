"""Находка #34 — строгая валидация тела POST /nodes/{id}/active и
PATCH /nodes/{id}/status.

До фикса эндпоинты принимали сырой ``body: dict`` и делали
``bool(body["is_active"])`` — из-за чего JSON ``{"is_active": "false"}``
(строка) трактовался как ``True``: нода НЕВЕРНО включалась и заодно
сбрасывались защитные гейты cooldown_until/suspect_since/blocked_regions.
Теперь тело — pydantic-схема со StrictBool, а строковый статус проходит
str-схему + whitelist, поэтому битый тип отбивается 422-ым ДО побочек.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import models

from .factories import make_node


def test_set_active_string_false_rejected(client, db_session):
    """{"is_active": "false"} (строка) → 422, а не «нода включена»."""
    node = make_node(db_session, name="n34-str-false", host="203.0.113.34")
    # Заранее ставим ноду в suspect/cooldown, чтобы поймать сброс гейтов,
    # если бы строка «false» ошибочно активировала.
    node.is_active = False
    node.cooldown_until = datetime.utcnow() + timedelta(hours=1)
    node.suspect_since = datetime.utcnow()
    node.blocked_regions = ["ru"]
    db_session.commit()

    resp = client.post(f"/api/nodes/{node.id}/active", json={"is_active": "false"})
    assert resp.status_code == 422

    db_session.refresh(node)
    # Побочек не случилось: гейты на месте, нода по-прежнему выключена.
    assert node.is_active is False
    assert node.cooldown_until is not None
    assert node.suspect_since is not None
    assert node.blocked_regions == ["ru"]


def test_set_active_true_clears_gates(client, db_session):
    """Настоящий bool true — happy path: включает + чистит гейты."""
    node = make_node(db_session, name="n34-true", host="203.0.113.35")
    node.is_active = False
    node.cooldown_until = datetime.utcnow() + timedelta(hours=1)
    node.suspect_since = datetime.utcnow()
    node.blocked_regions = ["ru"]
    db_session.commit()

    resp = client.post(f"/api/nodes/{node.id}/active", json={"is_active": True})
    assert resp.status_code == 200

    db_session.refresh(node)
    assert node.is_active is True
    assert node.cooldown_until is None
    assert node.suspect_since is None
    assert node.blocked_regions is None


def test_set_active_false_bool_keeps_gates(client, db_session):
    """bool false выключает ноду, гейты не трогает (сброс только на promote)."""
    node = make_node(db_session, name="n34-false", host="203.0.113.36")
    node.is_active = True
    node.cooldown_until = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()

    resp = client.post(f"/api/nodes/{node.id}/active", json={"is_active": False})
    assert resp.status_code == 200

    db_session.refresh(node)
    assert node.is_active is False
    assert node.cooldown_until is not None


def test_set_active_missing_field_422(client, db_session):
    """Отсутствие is_active → 422 (pydantic required)."""
    node = make_node(db_session, name="n34-missing", host="203.0.113.37")
    resp = client.post(f"/api/nodes/{node.id}/active", json={})
    assert resp.status_code == 422


def test_set_status_bad_value_400(client, db_session):
    """Строка вне whitelist → 400 (валидация значения)."""
    node = make_node(db_session, name="n34-badstatus", host="203.0.113.38")
    resp = client.patch(f"/api/nodes/{node.id}/status", json={"status": "draining"})
    assert resp.status_code == 400


def test_set_status_non_string_422(client, db_session):
    """status не строка (bool) → 422 str-схемы, а не тихий проброс."""
    node = make_node(db_session, name="n34-boolstatus", host="203.0.113.39")
    resp = client.patch(f"/api/nodes/{node.id}/status", json={"status": True})
    assert resp.status_code == 422


def test_set_status_active_clears_gates(client, db_session):
    """Happy path: status=active чистит гейты и re-enter пул."""
    node = make_node(db_session, name="n34-statusok", host="203.0.113.40")
    node.cooldown_until = datetime.utcnow() + timedelta(hours=1)
    node.blocked_regions = ["ru"]
    db_session.commit()

    resp = client.patch(f"/api/nodes/{node.id}/status", json={"status": "active"})
    assert resp.status_code == 200

    db_session.refresh(node)
    assert node.status == models.VPNNodeStatus.active
    assert node.cooldown_until is None
    assert node.blocked_regions is None
