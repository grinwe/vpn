"""Аудит операций с API-токенами должен фиксировать оператора (audit #47).

Раньше create_api_token/revoke_api_token писали в аудит литерал
``actor="admin"``, игнорируя заголовок X-Admin-Actor. Теперь оба хендлера
резолвят оператора через _resolve_admin_actor, как остальные роутеры зоны.
"""
from __future__ import annotations


def _last_audit(db_session, action: str):
    from app import models

    return (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == action)
        .order_by(models.AuditLog.id.desc())
        .first()
    )


def test_create_token_records_operator_from_header(client, db_session) -> None:
    resp = client.post(
        "/api/api-tokens",
        json={"name": "with-actor", "scopes": ["probe:read"]},
        headers={"X-Admin-Actor": "operator-alice"},
    )
    assert resp.status_code == 201, resp.text

    row = _last_audit(db_session, "api_token.create")
    assert row is not None
    assert row.actor == "operator-alice"


def test_revoke_token_records_operator_from_header(client, db_session) -> None:
    created = client.post(
        "/api/api-tokens",
        json={"name": "to-revoke", "scopes": ["probe:read"]},
    )
    assert created.status_code == 201, created.text
    token_id = created.json()["id"]

    resp = client.delete(
        f"/api/api-tokens/{token_id}",
        headers={"X-Admin-Actor": "operator-bob"},
    )
    assert resp.status_code == 204, resp.text

    row = _last_audit(db_session, "api_token.revoke")
    assert row is not None
    assert row.actor == "operator-bob"


def test_create_token_without_header_falls_back_to_admin(client, db_session) -> None:
    resp = client.post(
        "/api/api-tokens",
        json={"name": "no-actor", "scopes": ["probe:read"]},
    )
    assert resp.status_code == 201, resp.text

    row = _last_audit(db_session, "api_token.create")
    assert row is not None
    assert row.actor == "admin"
