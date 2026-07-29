"""Dynamic subscription link (`GET /api/sub/{token}`).

Covers the multi-protocol provisioning refactor: one provision call should
yield credentials for every enabled config on the chosen node, and the
sub-link endpoint should return all of them in a base64 blob.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta

from app import models
from app.services.provisioning import ProvisioningOrchestrator

from .factories import make_config, make_node, make_plan, make_user


def _node_with_two_protocols(db):
    node = make_node(db)
    make_config(
        db,
        node,
        name="shadowtls",
        protocol=models.VPNConfigProtocol.shadowtls_ss,
        port=8443,
        sni=None,
    )
    make_config(
        db,
        node,
        name="reality",
        protocol=models.VPNConfigProtocol.vless_reality,
        port=443,
    )
    db.refresh(node)
    return node


def test_provision_creates_credential_per_enabled_protocol(db_session):
    node = _node_with_two_protocols(db_session)
    user = make_user(db_session)
    plan = make_plan(db_session)

    orch = ProvisioningOrchestrator(db_session)
    sub, _task = orch.provision_subscription(user, plan, node_id=node.id)

    db_session.refresh(sub)
    protos = sorted(c.proto for c in sub.credentials)
    assert protos == ["shadowtls+shadowsocks", "vless-reality"]
    # One device, two credentials hanging off it.
    assert len(sub.devices) == 1
    assert len(sub.devices[0].credentials) == 2
    assert sub.sub_token


def test_sub_link_returns_all_protocols_base64(client, db_session):
    node = _node_with_two_protocols(db_session)
    user = make_user(db_session)
    plan = make_plan(db_session)
    orch = ProvisioningOrchestrator(db_session)
    sub, task = orch.provision_subscription(user, plan, node_id=node.id)
    # Провижн создаёт Device=pending и Credential.is_active=False; активация
    # (device→active, creds.is_active=True) выполняется в _handle_task_outcome
    # на успехе ansible. conftest глушит run_task_async, поэтому прогоняем
    # исход apply-таски вручную — иначе эндпоинт отбросит неактивные креды и
    # вернёт 503 вместо base64-выдачи всех протоколов.
    orch._handle_task_outcome(task, success=True)
    db_session.refresh(sub)

    resp = client.get(f"/api/sub/{sub.sub_token}", headers={})
    assert resp.status_code == 200
    body = base64.b64decode(resp.text).decode()
    # Строки с `#` — директивы блока статуса (Happ читает их из тела, потому
    # что в заголовке русский текст не уезжает). Сами ссылки — всё остальное.
    lines = [line for line in body.splitlines() if line and not line.startswith("#")]
    assert len(lines) == 2
    assert any(line.startswith("ss://") for line in lines)
    assert any(line.startswith("vless://") for line in lines)

    # Audit log entry was written.
    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="subscription_fetch", target_id=sub.id)
        .first()
    )
    assert audit is not None


def test_sub_link_expired_returns_403(client, db_session):
    node = _node_with_two_protocols(db_session)
    user = make_user(db_session)
    plan = make_plan(db_session)
    orch = ProvisioningOrchestrator(db_session)
    sub, _ = orch.provision_subscription(user, plan, node_id=node.id)

    sub.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    resp = client.get(f"/api/sub/{sub.sub_token}")
    assert resp.status_code == 403
    assert "expired" in resp.json()["detail"].lower()


def test_sub_link_blocked_returns_403(client, db_session):
    node = _node_with_two_protocols(db_session)
    user = make_user(db_session)
    plan = make_plan(db_session)
    orch = ProvisioningOrchestrator(db_session)
    sub, _ = orch.provision_subscription(user, plan, node_id=node.id)

    sub.status = models.SubscriptionStatus.blocked
    db_session.commit()

    resp = client.get(f"/api/sub/{sub.sub_token}")
    assert resp.status_code == 403


def test_sub_link_unknown_token_returns_404(client):
    resp = client.get("/api/sub/does-not-exist-token")
    assert resp.status_code == 404
