"""Tests for the 2026-07 fleet-uniformity code changes:

1. ``node_spawner.ensure_hysteria2_config`` — auto-provisioning helper that
   makes ``create_config`` able to add a working hysteria2 config (obfs+password,
   port-hopping, cert reuse) без ручной сборки settings.
2. Diverse-aware backfill/resync — a protocol added to a node now reaches
   **diverse** holders (Credential.node_id) too, not only home users
   (Subscription.node_id).
"""
import app.models as models
from app.security import decrypt, encrypt
from app.services.node_spawner import ensure_hysteria2_config
from app.services.provisioning import (
    ProvisioningOrchestrator,
    _build_hysteria2_credential,
    _build_vless_reality_credential,
    _build_vless_ws_cdn_credential,
    _extract_hy2_auth,
    _extract_vless_uuid,
    _hy2_auth,
)
from tests import factories


# ── ensure_hysteria2_config ──────────────────────────────────────────────

def test_ensure_hysteria2_config_reuses_xhttp_cert(db_session):
    node = factories.make_node(db_session, name="hy2-reuse", host="203.0.113.9")
    factories.make_config(
        db_session, node, name="xhttp",
        protocol=models.VPNConfigProtocol.vless_xhttp, sni="front.example.info",
    )
    db_session.refresh(node)

    cfg = ensure_hysteria2_config(db_session, node)

    assert cfg.protocol == models.VPNConfigProtocol.hysteria2
    assert cfg.port == 443
    # sni + cert reuse the node's existing xhttp LE cert domain
    assert cfg.sni == "front.example.info"
    s = cfg.settings
    assert s["cert_path"] == "/etc/letsencrypt/live/front.example.info/fullchain.pem"
    assert s["key_path"] == "/etc/letsencrypt/live/front.example.info/privkey.pem"
    # obfs is a pair (salamander + non-empty password)
    assert s["obfs"] == "salamander"
    assert isinstance(s["obfs_password"], str) and s["obfs_password"]
    assert s["port_hopping_range"]
    assert s["up_mbps"] and s["down_mbps"]


def test_ensure_hysteria2_config_idempotent(db_session):
    node = factories.make_node(db_session, name="hy2-idem", host="203.0.113.10")
    factories.make_config(
        db_session, node, name="xhttp",
        protocol=models.VPNConfigProtocol.vless_xhttp, sni="f.example.info",
    )
    db_session.refresh(node)

    first = ensure_hysteria2_config(db_session, node)
    db_session.refresh(node)
    second = ensure_hysteria2_config(db_session, node)

    assert first.id == second.id


def test_ensure_hysteria2_config_no_front_leaves_cert_empty(db_session):
    # A node without any xhttp/ws-cdn front — cert_path stays empty so the
    # role falls back to hysteria's own ACME (warned in the helper).
    node = factories.make_node(db_session, name="hy2-nofront", host="203.0.113.11")

    cfg = ensure_hysteria2_config(db_session, node)

    assert "cert_path" not in cfg.settings
    assert cfg.sni == ""
    assert cfg.settings["obfs"] == "salamander"


# ── diverse-aware backfill ───────────────────────────────────────────────

def test_backfill_ws_cdn_covers_diverse_device(db_session):
    """A device homed on node B but carrying a diverse reality cred on node A
    must get the newly-added ws-cdn leg on A too, reusing its node-A UUID."""
    node_a = factories.make_node(db_session, name="bf-a", host="10.0.0.1")
    node_b = factories.make_node(db_session, name="bf-b", host="10.0.0.2")
    reality_a = factories.make_config(
        db_session, node_a, name="reality-a",
        protocol=models.VPNConfigProtocol.vless_reality, sni="a.example.com",
    )
    ws_a = factories.make_config(
        db_session, node_a, name="ws-a",
        protocol=models.VPNConfigProtocol.vless_ws_cdn, sni="wscdn-a.example.info",
    )
    user = factories.make_user(db_session, telegram_id="tg-div")
    plan = factories.make_plan(db_session)
    sub = factories.make_subscription(db_session, user, plan, node_b)  # homed on B
    device = factories.make_device(db_session, sub, reality_a, access_username="udiv")

    # Existing diverse reality cred for the device ON node A, known UUID.
    known_uuid = "11111111-2222-3333-4444-555555555555"
    reality_text = _build_vless_reality_credential(node_a, reality_a, known_uuid)
    db_session.add(models.Credential(
        subscription_id=sub.id, device_id=device.id, config_id=reality_a.id,
        node_id=node_a.id, proto=models.VPNConfigProtocol.vless_reality.value,
        config_text=encrypt(reality_text), access_username="udiv", is_active=True,
    ))
    db_session.commit()
    db_session.refresh(device)

    orch = ProvisioningOrchestrator(db_session)
    created = orch.backfill_credentials_for_new_config(node_a, ws_a)
    db_session.commit()

    assert created >= 1
    ws_creds = (
        db_session.query(models.Credential)
        .filter(
            models.Credential.device_id == device.id,
            models.Credential.config_id == ws_a.id,
        )
        .all()
    )
    assert len(ws_creds) == 1
    ws_cred = ws_creds[0]
    assert ws_cred.node_id == node_a.id
    # reuses the device's node-A reality UUID (not a fresh one / other node's)
    assert _extract_vless_uuid(decrypt(ws_cred.config_text), cred_id=ws_cred.id) == known_uuid


# ── diverse-aware resync ─────────────────────────────────────────────────

def test_resync_includes_diverse_assigned_cred(db_session):
    """resync_node_clients must push a diverse assigned cred (Credential.node_id
    == this node, Subscription.node_id != this node) onto the node — previously
    it fell in the gap between the home-scoped and warm-scoped queries."""
    node_a = factories.make_node(db_session, name="rs-a", host="10.0.0.5")
    node_b = factories.make_node(db_session, name="rs-b", host="10.0.0.6")
    ws_a = factories.make_config(
        db_session, node_a, name="ws-a",
        protocol=models.VPNConfigProtocol.vless_ws_cdn, sni="wscdn.example.info",
    )
    user = factories.make_user(db_session, telegram_id="tg-rs")
    plan = factories.make_plan(db_session)
    sub = factories.make_subscription(db_session, user, plan, node_b)  # homed on B
    device = factories.make_device(db_session, sub, ws_a, access_username="ursync")

    known_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    ws_text = _build_vless_ws_cdn_credential(node_a, ws_a, known_uuid)
    db_session.add(models.Credential(
        subscription_id=sub.id, device_id=device.id, config_id=ws_a.id,
        node_id=node_a.id, proto=models.VPNConfigProtocol.vless_ws_cdn.value,
        config_text=encrypt(ws_text), access_username="ursync", is_active=True,
    ))  # pool_state defaults to assigned
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    task = orch.resync_node_clients(node_a)

    assert task is not None
    ws_clients = task.payload["clients_by_proto"]["vless-ws-cdn"]
    assert any(
        c["uuid"] == known_uuid and c["username"] == "ursync" for c in ws_clients
    )


def test_resync_hy2_includes_diverse_assigned(db_session):
    """resync_node_hysteria2_clients must push a diverse assigned hy2 cred
    (Credential.node_id == this node, Subscription.node_id != this node) into the
    node's userpass. The home-only query missed it → diverse holders got a /sub
    hy2 leg the node never authenticates (the regression this fix closes)."""
    node_a = factories.make_node(db_session, name="hy2rs-a", host="10.0.1.5")
    node_b = factories.make_node(db_session, name="hy2rs-b", host="10.0.1.6")
    hy2_a = factories.make_config(
        db_session, node_a, name="hy2-a",
        protocol=models.VPNConfigProtocol.hysteria2, sni="hy2.example.info",
    )
    user = factories.make_user(db_session, telegram_id="tg-hy2rs")
    plan = factories.make_plan(db_session)
    sub = factories.make_subscription(db_session, user, plan, node_b)  # homed on B
    device = factories.make_device(db_session, sub, hy2_a, access_username="uhy2")

    known_pw = "SbXwKnownHy2Pw12"
    hy2_text = _build_hysteria2_credential(node_a, hy2_a, _hy2_auth("uhy2", known_pw))
    # sanity: round-trips как ПАРА — сервер на auth.type: userpass матчит её,
    # а не голый пароль (аудит 2026-07-25).
    assert _extract_hy2_auth(hy2_text) == f"uhy2:{known_pw}"
    db_session.add(models.Credential(
        subscription_id=sub.id, device_id=device.id, config_id=hy2_a.id,
        node_id=node_a.id, proto=models.VPNConfigProtocol.hysteria2.value,
        config_text=encrypt(hy2_text), access_username="uhy2", is_active=True,
    ))  # pool_state defaults to assigned
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    tasks = orch.resync_node_hysteria2_clients(node_a)

    assert len(tasks) == 1
    clients = tasks[0].payload["clients"]
    assert any(
        c["username"] == "uhy2" and c["password"] == known_pw for c in clients
    )


def test_ensure_hysteria2_config_prefers_le_front(db_session):
    """When the node's xhttp front is CF Origin-CA (settings.cert_path set → cert
    at /etc/nginx/ssl, not /etc/letsencrypt), the helper must skip it and reuse
    the ws-cdn LE cert instead of building a nonexistent LE path."""
    node = factories.make_node(db_session, name="hy2-le", host="203.0.113.20")
    # CF-origin xhttp: cert_path present → NOT a Let's Encrypt path.
    xhttp = factories.make_config(
        db_session, node, name="xhttp-cforigin",
        protocol=models.VPNConfigProtocol.vless_xhttp, sni="cforigin.example.info",
    )
    xhttp.settings = {**(xhttp.settings or {}), "cert_path": "/etc/nginx/ssl/xhttp-origin.crt"}
    db_session.commit()
    # ws-cdn: always DNS-only LE.
    factories.make_config(
        db_session, node, name="ws-le",
        protocol=models.VPNConfigProtocol.vless_ws_cdn, sni="wsle.example.info",
    )
    db_session.refresh(node)

    cfg = ensure_hysteria2_config(db_session, node)

    # Picked the ws-cdn LE domain, not the CF-origin xhttp.
    assert cfg.sni == "wsle.example.info"
    assert cfg.settings["cert_path"] == "/etc/letsencrypt/live/wsle.example.info/fullchain.pem"
