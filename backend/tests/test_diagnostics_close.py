"""POST /api/diagnostics/{kind}/{id}/close — ручное закрытие инцидента.

В отличие от ack (глушит лишь ре-диагностику, оставляя красный бейдж),
close снимает сам инцидент: diagnose_incident_open_at → None + сброс
серии падений unreachable_since.
"""
from __future__ import annotations

from app import models
from app.time_utils import utcnow
from tests.factories import make_node


def test_diagnostics_close_clears_incident(client, db_session) -> None:
    node = make_node(
        db_session, name="close-1", host="203.0.113.60",
        status=models.VPNNodeStatus.active,
    )
    node.diagnose_incident_open_at = utcnow()
    node.unreachable_since = utcnow()
    node.diagnose_acked_at = utcnow()
    db_session.commit()
    nid = node.id

    r = client.post(f"/api/diagnostics/node/{nid}/close")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["was_open"] is True
    assert body["diagnose_incident_open_at"] is None

    db_session.expire_all()
    n = db_session.get(models.VPNNode, nid)
    assert n.diagnose_incident_open_at is None
    assert n.unreachable_since is None
    assert n.diagnose_acked_at is None


def test_diagnostics_close_noop_when_no_incident(client, db_session) -> None:
    node = make_node(
        db_session, name="close-2", host="203.0.113.61",
        status=models.VPNNodeStatus.active,
    )
    nid = node.id

    r = client.post(f"/api/diagnostics/node/{nid}/close")
    assert r.status_code == 200, r.text
    assert r.json()["was_open"] is False


def test_diagnostics_close_unknown_target(client) -> None:
    r = client.post("/api/diagnostics/node/999999/close")
    assert r.status_code == 404
