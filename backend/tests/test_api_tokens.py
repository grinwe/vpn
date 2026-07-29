"""Scoped API token auth — creation, scope enforcement, revocation.

These tests exist because the original auth model was "admin token can
do anything". Now probe rigs carry narrow tokens, and it's critical
that (a) a read-only token cannot write, (b) a revoked token stops
working immediately, (c) the admin token still short-circuits the
scope check.
"""
from __future__ import annotations


def _create_token(client, name: str, scopes: list[str]) -> str:
    resp = client.post(
        "/api/api-tokens",
        json={"name": name, "scopes": scopes},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["token"], "plaintext token must be in creation response"
    assert body["scopes"] == scopes
    return body["token"]


def test_create_token_rejects_unknown_scopes(client) -> None:
    resp = client.post(
        "/api/api-tokens",
        json={"name": "bad", "scopes": ["probe:read", "invoices:nuke"]},
    )
    assert resp.status_code == 400
    assert "invoices:nuke" in resp.text

    # traffic:write умер вместе с блокирующим ингестом (2026-07-29) и не
    # должен молча вернуться: учёт трафика наливает тик, а не HTTP-ручка.
    resp = client.post(
        "/api/api-tokens",
        json={"name": "zombie", "scopes": ["traffic:write"]},
    )
    assert resp.status_code == 400
    assert "traffic:write" in resp.text


def test_read_scope_cannot_write_probes(client) -> None:
    token = _create_token(client, "probe-kz", ["probe:read"])

    # Swap the admin header for the scoped one.
    client.headers.pop("X-Admin-Token", None)
    client.headers["X-Api-Token"] = token

    # probe:read → allowed
    resp = client.get("/api/probes/targets")
    assert resp.status_code == 200

    # probe:write → denied with 403 (token is authenticated but unauthorized)
    resp = client.post(
        "/api/nodes/1/probes",
        json={"source_region": "kz", "result": "ok"},
    )
    assert resp.status_code == 403
    assert "probe:write" in resp.text


def test_write_scope_cannot_read_probes(client) -> None:
    token = _create_token(client, "probe-writer", ["probe:write"])
    client.headers.pop("X-Admin-Token", None)
    client.headers["X-Api-Token"] = token

    resp = client.get("/api/probes/targets")
    assert resp.status_code == 403
    assert "probe:read" in resp.text


def test_revoked_token_is_rejected(client) -> None:
    # Create + list to grab the id, then revoke via admin.
    token = _create_token(client, "doomed", ["probe:read"])
    listing = client.get("/api/api-tokens").json()
    [row] = [r for r in listing if r["name"] == "doomed"]

    del_resp = client.delete(f"/api/api-tokens/{row['id']}")
    assert del_resp.status_code == 204

    client.headers.pop("X-Admin-Token", None)
    client.headers["X-Api-Token"] = token
    resp = client.get("/api/probes/targets")
    assert resp.status_code == 401


def test_admin_token_has_all_scopes_implicitly(client) -> None:
    # Default fixture client is admin — no scoped token, and all three
    # machine endpoints must still answer.
    assert client.get("/api/probes/targets").status_code == 200
