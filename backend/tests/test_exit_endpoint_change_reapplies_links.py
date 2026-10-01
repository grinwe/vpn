"""Смена endpoint-полей exit'а перепрошивает WG на всех его relay'ях.

Аудит RU split-routing 2026-07-28, находка А2. ``host``/``wg_port``/
``wg_public_key`` зашиты в ``wgN.conf`` на КАЖДОМ relay'е этого exit'а. WG
сам не переприцеливается: PersistentKeepalive продолжает слать кипэлайвы на
старый адрес, ``wg-quick@`` без ``Restart=`` не перезапустится, а exit не
может спасти роумингом — ``wg0.conf.j2`` рендерит peer'ов без ``Endpoint``.

Раньше правка в админке молча оставляла все туннели этого exit'а висеть:
зарубежный трафик умирал при живом РУ-сегменте, а лечилось это ручным
Reconnect по каждому линку (если оператор вообще догадывался, что дело в этом
— наблюдаемость показывала только протухший handshake и спам auto-diagnose).
"""
from __future__ import annotations

from app import models
from app.security import encrypt

from .factories import make_node


def _mk_exit(db, *, name: str, host: str = "198.51.100.60", pubkey: str = "pub-old"):
    node = models.WGExitNode(
        name=name,
        region="Turkey",
        host=host,
        wg_port=51820,
        wg_public_key=pubkey,
        wg_private_key_enc=encrypt("priv-old"),
        status=models.WGExitNodeStatus.active,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _mk_link(db, relay: models.VPNNode, exit_node: models.WGExitNode, *, iface="wg0"):
    link = models.RelayExitLink(
        relay_node_id=relay.id,
        exit_id=exit_node.id,
        wg_interface_name=iface,
        wg_client_private_key_enc=encrypt("client-priv"),
        wg_client_public_key="client-pub",
        wg_client_address_v4="10.77.0.5/32",
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def _apply_tasks(db, relay_id: int):
    return (
        db.query(models.ProvisioningTask)
        .filter(
            models.ProvisioningTask.target_type == "relay_tunnel",
            models.ProvisioningTask.target_id == relay_id,
            models.ProvisioningTask.action == "apply",
        )
        .all()
    )


def test_host_change_reapplies_every_link(client, db_session):
    exit_node = _mk_exit(db_session, name="ex-host-change")
    relay_a = make_node(db_session, name="relay-a", host="203.0.113.1")
    relay_b = make_node(db_session, name="relay-b", host="203.0.113.2")
    _mk_link(db_session, relay_a, exit_node)
    _mk_link(db_session, relay_b, exit_node)

    resp = client.patch(
        f"/api/exits/{exit_node.id}", json={"host": "198.51.100.99"}
    )

    assert resp.status_code == 200, resp.text
    assert _apply_tasks(db_session, relay_a.id), "relay A не переприцелен"
    assert _apply_tasks(db_session, relay_b.id), "relay B не переприцелен"


def test_wg_port_change_reapplies(client, db_session):
    exit_node = _mk_exit(db_session, name="ex-port-change")
    relay = make_node(db_session, name="relay-port", host="203.0.113.3")
    _mk_link(db_session, relay, exit_node)

    resp = client.patch(f"/api/exits/{exit_node.id}", json={"wg_port": 51821})

    assert resp.status_code == 200, resp.text
    assert _apply_tasks(db_session, relay.id)


def test_cosmetic_change_does_not_touch_links(client, db_session):
    """Переименование/заметка не трогают туннели — иначе каждая правка в
    админке рвала бы прод пачкой ансибл-прогонов."""
    exit_node = _mk_exit(db_session, name="ex-cosmetic")
    relay = make_node(db_session, name="relay-cosmetic", host="203.0.113.4")
    _mk_link(db_session, relay, exit_node)

    resp = client.patch(
        f"/api/exits/{exit_node.id}",
        json={"name": "ex-cosmetic-renamed", "notes": "переехал в другой ДЦ"},
    )

    assert resp.status_code == 200, resp.text
    assert not _apply_tasks(db_session, relay.id)


def test_keygen_reapplies_links(client, db_session):
    """Ротация ключа рвёт handshake на всех relay'ях — их надо перепрошить."""
    exit_node = _mk_exit(db_session, name="ex-keygen")
    relay = make_node(db_session, name="relay-keygen", host="203.0.113.5")
    _mk_link(db_session, relay, exit_node)

    resp = client.post(f"/api/exits/{exit_node.id}/keygen")

    assert resp.status_code == 200, resp.text
    assert resp.json()["wg_public_key"] != "pub-old"
    assert _apply_tasks(db_session, relay.id)


def test_exit_without_links_is_noop(client, db_session):
    """Exit без привязанных relay'ев — правка проходит, задач нет."""
    exit_node = _mk_exit(db_session, name="ex-orphan")

    resp = client.patch(f"/api/exits/{exit_node.id}", json={"host": "198.51.100.98"})

    assert resp.status_code == 200, resp.text
    assert (
        db_session.query(models.ProvisioningTask)
        .filter(models.ProvisioningTask.target_type == "relay_tunnel")
        .count()
        == 0
    )


def test_audit_row_written(client, db_session):
    exit_node = _mk_exit(db_session, name="ex-audit")
    relay = make_node(db_session, name="relay-audit", host="203.0.113.6")
    link = _mk_link(db_session, relay, exit_node)

    client.patch(f"/api/exits/{exit_node.id}", json={"host": "198.51.100.97"})

    row = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "wg_exit_endpoint_reapplied")
        .first()
    )
    assert row is not None
    assert row.extra["link_ids"] == [link.id]
    assert row.extra["task_ids"]
