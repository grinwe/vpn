"""End-to-end for the node traffic ingestion endpoint.

Exercises the path the node-side collector hits in production:
``POST /api/nodes/{node_id}/traffic`` → per-user delta attribution →
over-limit revocation. Also makes sure unknown usernames don't poison
the batch.
"""
from __future__ import annotations

from datetime import datetime

from app import models
from tests import factories


def _build_payload(samples: list[tuple[str, int, int]]) -> dict:
    return {
        "collected_at": datetime.utcnow().isoformat() + "Z",
        "samples": [
            {
                "access_username": u,
                "uplink_bytes": up,
                "downlink_bytes": down,
            }
            for (u, up, down) in samples
        ],
    }


def test_traffic_ingest_updates_subscription_counter(client, db_session) -> None:
    node = factories.make_node(db_session)
    config = factories.make_config(db_session, node)
    plan = factories.make_plan(db_session, traffic_limit_mb=10_000)
    user = factories.make_user(db_session)
    sub = factories.make_subscription(db_session, user, plan, node)
    factories.make_device(db_session, sub, config, access_username="alice")

    # 5 MiB uplink + 3 MiB downlink = 8 MiB → rounds up to 8 MB in the
    # helper's bytes→MB conversion (ceil division on 1 MiB units).
    payload = _build_payload([("alice", 5 * 1024 * 1024, 3 * 1024 * 1024)])

    resp = client.post(f"/api/nodes/{node.id}/traffic", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted_samples"] == 1
    assert body["unknown_usernames"] == []
    assert len(body["subscriptions"]) == 1
    assert body["subscriptions"][0]["subscription_id"] == sub.id
    assert body["subscriptions"][0]["used_mb_delta"] == 8
    assert body["subscriptions"][0]["over_limit"] is False

    db_session.expire_all()
    refreshed = db_session.get(models.Subscription, sub.id)
    assert refreshed.traffic_used_mb == 8


def test_traffic_ingest_unknown_username_is_not_an_error(client, db_session) -> None:
    node = factories.make_node(db_session)
    factories.make_config(db_session, node)

    payload = _build_payload([("ghost-device", 123456, 789012)])

    resp = client.post(f"/api/nodes/{node.id}/traffic", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted_samples"] == 0
    assert body["unknown_usernames"] == ["ghost-device"]
    assert body["subscriptions"] == []


def test_traffic_ingest_over_limit_revokes_subscription(client, db_session) -> None:
    node = factories.make_node(db_session)
    config = factories.make_config(db_session, node)
    plan = factories.make_plan(db_session, traffic_limit_mb=1)  # 1 MB budget
    user = factories.make_user(db_session)
    sub = factories.make_subscription(db_session, user, plan, node)
    factories.make_device(db_session, sub, config, access_username="alice")

    # 10 MiB of uplink — well over the 1 MB limit.
    payload = _build_payload([("alice", 10 * 1024 * 1024, 0)])

    resp = client.post(f"/api/nodes/{node.id}/traffic", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["subscriptions"][0]["over_limit"] is True

    db_session.expire_all()
    refreshed = db_session.get(models.Subscription, sub.id)
    assert refreshed.status == models.SubscriptionStatus.blocked
    assert refreshed.notes == "traffic limit exceeded"
