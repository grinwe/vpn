"""Per-device admin migrate + switch-exit.

Covers the two orchestrator primitives added for Stage 3 admin UI:

* ``migrate_device_to_node`` — surgical relocation of a single device
  to another node. sub.node_id must stay untouched (the sub becomes
  "split" across nodes); the old device row must be marked disabled
  but kept for sub_token aliasing; a fresh device must land on the
  target.

* ``switch_device_exit`` — per-device exit re-pin on a multi-link
  relay. Only ``Credential.device_id == device.id`` rows must flip to
  the new exit_id — siblings on the same sub that share the relay
  must stay on their original exits.

Ansible is monkey-patched globally (conftest ``_no_provisioning``), so
we exercise DB state transitions and task-row creation rather than the
playbook subprocess.
"""
from __future__ import annotations

import pytest

from app import models
from app.security import encrypt
from app.services.provisioning import ProvisioningOrchestrator

from .factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


# ── migrate_device_to_node ───────────────────────────────────────────


def _sub_with_device_on(db, node):
    """User+plan+sub+1 device on ``node`` with a vless_reality config."""
    cfg = next(iter(node.configs), None) or make_config(db, node)
    plan = make_plan(db)
    user = make_user(db, telegram_id=f"tg-{node.id}")
    sub = make_subscription(db, user, plan, node)
    device = make_device(db, sub, cfg, access_username=f"user-{user.id}-{sub.id}")
    db.refresh(sub)
    return sub, device


def test_migrate_device_leaves_sub_on_old_node(db_session):
    """Device moves to B; sub.node_id stays on A (sub is now 'split')."""
    old_node = make_node(db_session, name="node-A", host="10.0.0.1")
    new_node = make_node(db_session, name="node-B", host="10.0.0.2")
    make_config(db_session, new_node)

    sub, device = _sub_with_device_on(db_session, old_node)
    old_device_id = device.id

    orch = ProvisioningOrchestrator(db_session)
    target, new_device, task = orch.migrate_device_to_node(
        device, target_node_id=new_node.id,
    )

    db_session.refresh(sub)
    db_session.refresh(device)
    db_session.refresh(new_device)

    assert sub.node_id == old_node.id, "sub must stay on old node — split state"
    assert target.id == new_node.id
    assert new_device.id != old_device_id
    assert new_device.config is not None
    assert new_device.config.node_id == new_node.id
    # Old device survives as a disabled row so /sub/{old_token} aliasing
    # keeps working for clients that haven't picked up the new URI yet.
    assert device.status == models.DeviceStatus.disabled
    assert task is not None
    assert task.target_type == "device"


def test_migrate_device_to_inactive_target_raises(db_session):
    old_node = make_node(db_session, name="node-A", host="10.0.0.1")
    dead_node = make_node(db_session, name="node-dead", host="10.0.0.2")
    dead_node.is_active = False
    db_session.add(dead_node)
    db_session.commit()
    make_config(db_session, dead_node)

    _sub, device = _sub_with_device_on(db_session, old_node)

    orch = ProvisioningOrchestrator(db_session)
    with pytest.raises(RuntimeError, match="not active"):
        orch.migrate_device_to_node(device, target_node_id=dead_node.id)


def test_migrate_device_to_same_node_raises(db_session):
    node = make_node(db_session, name="node-A", host="10.0.0.1")
    _sub, device = _sub_with_device_on(db_session, node)

    orch = ProvisioningOrchestrator(db_session)
    with pytest.raises(RuntimeError, match="matches device's current node"):
        orch.migrate_device_to_node(device, target_node_id=node.id)


def test_migrate_disabled_device_raises(db_session):
    node = make_node(db_session, name="node-A", host="10.0.0.1")
    other = make_node(db_session, name="node-B", host="10.0.0.2")
    make_config(db_session, other)
    _sub, device = _sub_with_device_on(db_session, node)
    device.status = models.DeviceStatus.disabled
    db_session.add(device)
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    with pytest.raises(RuntimeError, match="must be active/pending"):
        orch.migrate_device_to_node(device, target_node_id=other.id)


# ── switch_device_exit ───────────────────────────────────────────────


def _make_exit(db, *, name: str, host: str = "203.0.113.1") -> models.WGExitNode:
    exit_node = models.WGExitNode(
        name=name,
        region="eu",
        host=host,
        status=models.WGExitNodeStatus.active,
        is_active=True,
    )
    db.add(exit_node)
    db.commit()
    db.refresh(exit_node)
    return exit_node


def _attach_exit_to_relay(
    db,
    relay: models.VPNNode,
    exit_node: models.WGExitNode,
    *,
    interface: str,
    client_ipv4: str,
) -> models.RelayExitLink:
    link = models.RelayExitLink(
        relay_node_id=relay.id,
        exit_id=exit_node.id,
        wg_interface_name=interface,
        wg_client_private_key_enc=encrypt("dummy-privkey"),
        wg_client_public_key=f"pubkey-{interface}",
        wg_client_address_v4=client_ipv4,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def _cred_on_exit(
    db,
    *,
    sub: models.Subscription,
    device: models.Device,
    cfg: models.VPNConfig,
    exit_id: int,
    username: str,
) -> models.Credential:
    cred = models.Credential(
        subscription_id=sub.id,
        device_id=device.id,
        config_id=cfg.id,
        node_id=sub.node_id,
        exit_id=exit_id,
        proto=cfg.protocol.value,
        config_text=encrypt(f"dummy-cred-{username}"),
        access_username=username,
        is_active=True,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


def test_switch_device_exit_touches_only_target_device(db_session):
    """D1 flips X→Y, D2 on the same sub stays on X."""
    relay = make_node(db_session, name="relay-1", host="10.0.0.10")
    cfg = make_config(db_session, relay)
    exit_x = _make_exit(db_session, name="exit-X", host="203.0.113.10")
    exit_y = _make_exit(db_session, name="exit-Y", host="203.0.113.20")
    _attach_exit_to_relay(
        db_session, relay, exit_x, interface="wg0", client_ipv4="10.77.0.5/32",
    )
    _attach_exit_to_relay(
        db_session, relay, exit_y, interface="wg1", client_ipv4="10.78.0.5/32",
    )

    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, relay)

    d1 = make_device(db_session, sub, cfg, access_username="user-1-1-a")
    d2 = make_device(db_session, sub, cfg, access_username="user-1-1-b")
    cred_d1 = _cred_on_exit(
        db_session, sub=sub, device=d1, cfg=cfg, exit_id=exit_x.id,
        username=d1.access_username,
    )
    cred_d2 = _cred_on_exit(
        db_session, sub=sub, device=d2, cfg=cfg, exit_id=exit_x.id,
        username=d2.access_username,
    )

    orch = ProvisioningOrchestrator(db_session)
    old_exit_id, new_iface, tasks = orch.switch_device_exit(d1, exit_y.id)

    db_session.refresh(cred_d1)
    db_session.refresh(cred_d2)

    assert old_exit_id == exit_x.id
    assert new_iface == "wg1"
    assert cred_d1.exit_id == exit_y.id
    # Sibling on the same sub must stay put — that's the whole point.
    assert cred_d2.exit_id == exit_x.id
    assert len(tasks) == 1
    task = tasks[0]
    assert task.target_type == "relay_tunnel"
    assert task.action == "apply"
    assert task.payload["switch_device_id"] == d1.id
    assert task.payload["new_exit_id"] == exit_y.id
    assert task.payload["new_interface"] == "wg1"


def test_switch_device_exit_unknown_exit_raises(db_session):
    relay = make_node(db_session, name="relay-1", host="10.0.0.10")
    cfg = make_config(db_session, relay)
    exit_x = _make_exit(db_session, name="exit-X", host="203.0.113.10")
    _attach_exit_to_relay(
        db_session, relay, exit_x, interface="wg0", client_ipv4="10.77.0.5/32",
    )
    # exit_y exists but is NOT linked to this relay.
    exit_y = _make_exit(db_session, name="exit-Y", host="203.0.113.20")

    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, relay)
    device = make_device(db_session, sub, cfg)
    _cred_on_exit(
        db_session, sub=sub, device=device, cfg=cfg, exit_id=exit_x.id,
        username=device.access_username,
    )

    orch = ProvisioningOrchestrator(db_session)
    with pytest.raises(RuntimeError, match="not attached to relay"):
        orch.switch_device_exit(device, exit_y.id)


def test_switch_device_exit_same_exit_raises(db_session):
    """Calling with the exit the device is already on is a no-op that
    would still enqueue an ansible run — we reject it to avoid wasting
    a playbook invocation on a spurious admin double-click."""
    relay = make_node(db_session, name="relay-1", host="10.0.0.10")
    cfg = make_config(db_session, relay)
    exit_x = _make_exit(db_session, name="exit-X", host="203.0.113.10")
    _attach_exit_to_relay(
        db_session, relay, exit_x, interface="wg0", client_ipv4="10.77.0.5/32",
    )

    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, relay)
    device = make_device(db_session, sub, cfg)
    _cred_on_exit(
        db_session, sub=sub, device=device, cfg=cfg, exit_id=exit_x.id,
        username=device.access_username,
    )

    orch = ProvisioningOrchestrator(db_session)
    with pytest.raises(RuntimeError, match="already routed"):
        orch.switch_device_exit(device, exit_x.id)


# ── HTTP smoke ───────────────────────────────────────────────────────


def test_migrate_device_route_200_and_audit(client, db_session):
    old_node = make_node(db_session, name="node-A", host="10.0.0.1")
    new_node = make_node(db_session, name="node-B", host="10.0.0.2")
    make_config(db_session, new_node)
    _sub, device = _sub_with_device_on(db_session, old_node)

    resp = client.post(
        f"/api/devices/{device.id}/migrate",
        json={"target_node_id": new_node.id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["old_device_id"] == device.id
    assert body["old_node_id"] == old_node.id
    assert body["new_node_id"] == new_node.id
    assert body["new_node_name"] == "node-B"
    assert body["device_id"] != device.id

    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="device_migrated", target_id=body["device_id"])
        .first()
    )
    assert audit is not None
    assert audit.extra["old_node_id"] == old_node.id
    assert audit.extra["new_node_id"] == new_node.id


def test_migrate_device_route_404(client):
    resp = client.post(
        "/api/devices/999999/migrate", json={"target_node_id": 1},
    )
    assert resp.status_code == 404


def test_migrate_device_route_same_node_400(client, db_session):
    node = make_node(db_session, name="node-A", host="10.0.0.1")
    _sub, device = _sub_with_device_on(db_session, node)
    resp = client.post(
        f"/api/devices/{device.id}/migrate", json={"target_node_id": node.id},
    )
    assert resp.status_code == 400
    assert "current node" in resp.json()["detail"]


def test_switch_device_exit_route_200_and_audit(client, db_session):
    relay = make_node(db_session, name="relay-1", host="10.0.0.10")
    cfg = make_config(db_session, relay)
    exit_x = _make_exit(db_session, name="exit-X", host="203.0.113.10")
    exit_y = _make_exit(db_session, name="exit-Y", host="203.0.113.20")
    _attach_exit_to_relay(
        db_session, relay, exit_x, interface="wg0", client_ipv4="10.77.0.5/32",
    )
    _attach_exit_to_relay(
        db_session, relay, exit_y, interface="wg1", client_ipv4="10.78.0.5/32",
    )
    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, relay)
    device = make_device(db_session, sub, cfg)
    _cred_on_exit(
        db_session, sub=sub, device=device, cfg=cfg, exit_id=exit_x.id,
        username=device.access_username,
    )

    resp = client.post(
        f"/api/devices/{device.id}/switch-exit",
        json={"exit_id": exit_y.id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device_id"] == device.id
    assert body["old_exit_id"] == exit_x.id
    assert body["new_exit_id"] == exit_y.id
    assert body["new_interface"] == "wg1"
    assert len(body["task_ids"]) == 1

    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="device_exit_switched", target_id=device.id)
        .first()
    )
    assert audit is not None
    assert audit.extra["old_exit_id"] == exit_x.id
    assert audit.extra["new_exit_id"] == exit_y.id


def test_switch_device_exit_route_404(client):
    resp = client.post(
        "/api/devices/999999/switch-exit", json={"exit_id": 1},
    )
    assert resp.status_code == 404


def test_switch_device_exit_route_disabled_device_400(client, db_session):
    relay = make_node(db_session, name="relay-1", host="10.0.0.10")
    cfg = make_config(db_session, relay)
    exit_x = _make_exit(db_session, name="exit-X", host="203.0.113.10")
    _attach_exit_to_relay(
        db_session, relay, exit_x, interface="wg0", client_ipv4="10.77.0.5/32",
    )
    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, relay)
    device = make_device(db_session, sub, cfg)
    device.status = models.DeviceStatus.disabled
    db_session.add(device)
    db_session.commit()

    resp = client.post(
        f"/api/devices/{device.id}/switch-exit", json={"exit_id": exit_x.id},
    )
    assert resp.status_code == 400
    assert "must be active" in resp.json()["detail"]


# ── client_id_hmac reuse-clearing regression (node-drain 500) ─────────
#
# Регресс на 500 при /nodes/{id}/migrate: Device.client_id_hmac UNIQUE и
# ПРОИЗВОДНЫЙ от sub_token. Node-drain пути (migrate_subscription_to_new_node,
# migrate_subscriptions_off) обнуляли только sub_token — старый ревокнутый
# девайс сохранял client_id_hmac, и reprovision нового девайса с тем же
# reuse_sub_token упирался в ix_devices_client_id_hmac (IntegrityError →
# отравленная сессия → PendingRollbackError → HTTP 500). Чиним: обнуляем
# client_id_hmac вместе с sub_token (как уже делал migrate_device_to_node).


def test_migrate_subscription_clears_client_id_hmac_on_reused_token(
    db_session, monkeypatch,
):
    from app.security import compute_client_id_hmac
    from app.services import provisioning as prov_mod

    plan = make_plan(db_session)
    user = make_user(db_session)
    old = make_node(db_session, name="hmac-old", region="ru")
    new = make_node(db_session, name="hmac-new", region="ru", host="198.51.100.211")
    cfg = make_config(db_session, old)
    make_config(db_session, new)
    sub = make_subscription(db_session, user, plan, old)
    dev = make_device(db_session, sub, cfg, access_username="u")
    token = "tok-reuse-abc"
    dev.sub_token = token
    dev.client_id_hmac = compute_client_id_hmac(token)
    db_session.commit()

    monkeypatch.setattr(prov_mod, "choose_node", lambda *a, **k: new)
    orch = ProvisioningOrchestrator(db_session)
    monkeypatch.setattr(orch, "revoke_device", lambda *a, **k: None)
    # reprovision — no-op: обнуление client_id_hmac происходит ДО него.
    monkeypatch.setattr(
        orch, "reprovision_subscription", lambda *a, **k: (dev, object())
    )
    monkeypatch.setattr(orch, "_maybe_attach_diverse", lambda *a, **k: None)

    orch.migrate_subscription_to_new_node(sub)

    db_session.refresh(dev)
    # токен освобождён И его client_id_hmac снят → reuse не упрётся в UNIQUE
    assert dev.sub_token is None
    assert dev.client_id_hmac is None


def test_migrate_subscriptions_off_clears_client_id_hmac(db_session, monkeypatch):
    import types

    from app.security import compute_client_id_hmac
    from app.services import provisioning as prov_mod
    from app.services.health import migrate_subscriptions_off

    plan = make_plan(db_session)
    user = make_user(db_session)
    old = make_node(db_session, name="drain-old", region="ru")
    new = make_node(db_session, name="drain-new", region="ru", host="198.51.100.221")
    cfg = make_config(db_session, old)
    make_config(db_session, new)
    sub = make_subscription(db_session, user, plan, old)
    dev = make_device(db_session, sub, cfg, access_username="u")
    token = "tok-drain-xyz"
    dev.sub_token = token
    dev.client_id_hmac = compute_client_id_hmac(token)
    db_session.commit()
    did, sid = dev.id, sub.id

    # migrate_subscriptions_off строит свой ProvisioningOrchestrator(db) внутри,
    # поэтому патчим методы КЛАССА + choose_node/_node_has_vless_family на модуле.
    monkeypatch.setattr(prov_mod, "choose_node", lambda *a, **k: new)
    monkeypatch.setattr(prov_mod, "_node_has_vless_family", lambda *a, **k: False)
    monkeypatch.setattr(
        ProvisioningOrchestrator, "revoke_device",
        lambda self, *a, **k: types.SimpleNamespace(id=0),
    )
    monkeypatch.setattr(
        ProvisioningOrchestrator, "reprovision_subscription",
        lambda self, *a, **k: (None, None),
    )

    result = migrate_subscriptions_off(db_session, old, reason="test-drain")

    assert sid in result["subscription_ids"]
    db_session.expire_all()
    moved = db_session.get(models.Subscription, sid)
    assert moved.node_id == new.id
    reused = db_session.get(models.Device, did)
    assert reused.sub_token is None
    assert reused.client_id_hmac is None
