"""Smoke tests for admin authentication.

Cheap canary — if these fail, the admin-token middleware itself is
broken and nothing else in the suite can be trusted.
"""
from __future__ import annotations


def test_admin_required_endpoint_rejects_missing_token(client) -> None:
    # Strip the default admin header the fixture installed.
    client.headers.pop("X-Admin-Token", None)
    resp = client.get("/api/probes/targets")
    assert resp.status_code == 401


def test_admin_required_endpoint_rejects_wrong_token(client) -> None:
    client.headers["X-Admin-Token"] = "definitely-not-the-token"
    resp = client.get("/api/probes/targets")
    assert resp.status_code == 401


def test_admin_required_endpoint_accepts_correct_token(client) -> None:
    resp = client.get("/api/probes/targets")
    assert resp.status_code == 200
    body = resp.json()
    assert "targets" in body
    # Empty DB → no targets, but the response shape must still be valid.
    assert body["targets"] == []
