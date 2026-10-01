"""Probe ingestion + autoscale config — thin happy-path tests.

These cover the other two API surfaces we expose to external callers
(the probe agents and the admin UI for pool config) so that schema or
route-level breakage trips CI before it ships.
"""
from __future__ import annotations

from app import models
from tests import factories


def test_probe_targets_exposes_only_active_nodes(client, db_session) -> None:
    active = factories.make_node(
        db_session, name="active-1", host="198.51.100.10", status=models.VPNNodeStatus.active
    )
    factories.make_config(db_session, active, sni="www.microsoft.com")

    factories.make_node(
        db_session,
        name="registering-1",
        host="198.51.100.11",
        status=models.VPNNodeStatus.registering,
    )

    resp = client.get("/api/probes/targets")
    assert resp.status_code == 200
    targets = resp.json()["targets"]

    assert len(targets) == 1
    assert targets[0]["name"] == "active-1"
    assert targets[0]["endpoints"][0]["kind"] == "tls"
    assert targets[0]["endpoints"][0]["sni"] == "www.microsoft.com"


def test_submit_probe_updates_node_health(client, db_session) -> None:
    node = factories.make_node(db_session)

    resp = client.post(
        f"/api/nodes/{node.id}/probes",
        json={
            "source_region": "ru-mts",
            "result": "ok",
            "latency_ms": 42,
            "source_kind": "residential",
            "details": {"port": 443},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["node_id"] == node.id
    assert body["overall_success_rate"] == 1.0
    assert "ru-mts" in body["per_region"]


def test_pool_autoscale_config_round_trip(client, db_session) -> None:
    pool = models.ServerPool(name="eu-main", region="eu")
    db_session.add(pool)
    db_session.commit()
    db_session.refresh(pool)

    resp = client.put(
        f"/api/pools/{pool.id}/autoscale",
        json={
            "autoscale_enabled": True,
            "autoscale_high_watermark": 0.75,
            "autoscale_max_nodes": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["autoscale_enabled"] is True
    assert body["autoscale_high_watermark"] == 0.75
    assert body["autoscale_max_nodes"] == 5

    db_session.expire_all()
    refreshed = db_session.get(models.ServerPool, pool.id)
    assert refreshed.autoscale_enabled is True
    assert float(refreshed.autoscale_high_watermark) == 0.75
