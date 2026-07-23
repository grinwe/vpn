"""Регресс на баг диверсного ребилда config_text.

rebuild_subscription_config_text печёт URI КАЖДОГО кредо. Диверсная (N×M)
подписка держит креды на РАЗНЫХ нодах. Баг: билдер получал subscription.node
(primary) для ВСЕХ кредов → reality/hy2 кредов на не-primary нодах запекались
с IP primary-ноды (xhttp уцелевал — доменный). Клиент бил не туда = «reality
н/д» у диверсных юзеров. Фикс: строить по cfg.node (нода самого кредо).
"""
from __future__ import annotations

from app import models
from app.security import decrypt, encrypt
from app.services.provisioning import (
    ProvisioningOrchestrator,
    _build_vless_reality_credential,
)

from .factories import make_config, make_device, make_node, make_plan, make_subscription, make_user


def _add_reality_cred(db_session, device, node, cfg, uuid):
    cred = models.Credential(
        subscription_id=device.subscription_id,
        device_id=device.id,
        config_id=cfg.id,
        node_id=node.id,
        proto=models.VPNConfigProtocol.vless_reality.value,
        config_text=encrypt(_build_vless_reality_credential(node, cfg, uuid)),
        is_active=True,
        pool_state=models.CredentialPoolState.assigned,
    )
    db_session.add(cred)
    db_session.commit()
    db_session.refresh(cred)
    return cred


def test_rebuild_uses_each_creds_own_node_not_subscription_node(db_session):
    node_a = make_node(db_session, name="node-a", region="ru", host="10.0.0.1")
    node_b = make_node(db_session, name="node-b", region="nl", host="10.0.0.2")
    cfg_a = make_config(db_session, node_a, name="reality-a")
    cfg_b = make_config(db_session, node_b, name="reality-b")
    user = make_user(db_session)
    plan = make_plan(db_session)
    sub = make_subscription(db_session, user, plan, node_a)  # primary = node_a
    device = make_device(db_session, sub, cfg_a, access_username="u1")

    uuid = "11111111-1111-1111-1111-111111111111"
    cred_a = _add_reality_cred(db_session, device, node_a, cfg_a, uuid)
    cred_b = _add_reality_cred(db_session, device, node_b, cfg_b, uuid)

    n = ProvisioningOrchestrator(db_session).rebuild_subscription_config_text(sub)
    assert n >= 2

    db_session.refresh(cred_a)
    db_session.refresh(cred_b)
    txt_a = decrypt(cred_a.config_text)
    txt_b = decrypt(cred_b.config_text)

    # Каждый cred — по СВОЕЙ ноде. С багом cred_b запёкся бы с host node_a (10.0.0.1).
    assert "@10.0.0.1:" in txt_a, txt_a
    assert "@10.0.0.2:" in txt_b, txt_b
