"""Э3: hy2 наконец виден в телеметрии, а carrying умеет per-протокол.

До этого hysteria2 не собиралась вовсе: KNOWN_PROTOCOL_PORTS — это gRPC-порты
xray, а hy2 отдельный демон. Человек, у которого работает ТОЛЬКО hy2 (регионы с
жёстким DPI — там вывозит именно он), выглядел для нас «не подключившимся»: ни
carrying_fraction, ни watcher репортов его трафик не видели.
"""
from __future__ import annotations

from app import models
from app.services import carrying
from app.services.traffic_stats import _parse_hysteria_traffic
from app.time_utils import utcnow

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)


# ── разбор ответа Traffic Stats API ─────────────────────────────────────────


def test_hysteria_traffic_parsed():
    stats = _parse_hysteria_traffic('{"alice": {"tx": 100, "rx": 40}}')
    assert stats.users == {"alice"}
    # tx/rx считаются со стороны СЕРВЕРА: tx → downlink клиента, rx → uplink.
    # Перепутать значит показать в админке зеркальную картину.
    assert stats.downlink == 100
    assert stats.uplink == 40
    assert stats.error is None


def test_hysteria_sentinel_is_not_a_person():
    """__sentinel__ — заглушка против краш-лупа на пустом userpass."""
    stats = _parse_hysteria_traffic('{"__sentinel__": {"tx": 1, "rx": 1}, "bob": {}}')
    assert stats.users == {"bob"}


def test_hysteria_broken_payload_is_an_error_not_a_zero():
    """Мусор в ответе обязан стать ошибкой: молчаливый ноль неотличим от
    «никто не подключался» — ровно та подмена, из-за которой hy2 и был слеп."""
    stats = _parse_hysteria_traffic("<html>502</html>")
    assert stats.error
    assert not stats.users


# ── carrying: знаменатель и разрез по протоколам ────────────────────────────


def _node_with_device(db, tag, protos, published):
    node = make_node(db, name=f"carry-{tag}", host=f"203.0.113.{200 + len(tag)}")
    make_config(db, node)
    plan = make_plan(db)
    user = make_user(db, telegram_id=f"carry-{tag}")
    sub = make_subscription_with_device(db, user, plan, node)
    device = sub.devices[0]
    device.status = models.DeviceStatus.active
    sub.status = models.SubscriptionStatus.active
    for proto in protos:
        db.add(
            models.Credential(
                subscription_id=sub.id,
                device_id=device.id,
                node_id=node.id,
                proto=proto,
                config_text="enc-stub",
                access_username=device.access_username,
                is_active=True,
                leg_published=(proto in published),
                leg_role="primary" if proto in published else None,
            )
        )
    db.commit()
    return node, device


def test_denominator_counts_published_legs_only(db_session, monkeypatch):
    """При 4×1 активных кредов на ноде четыре, а отдан один. Считать все —
    значит раздуть знаменатель вчетверо и утопить метрику: она закричала бы
    «нода мертва» на здоровом флоте."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    node, device = _node_with_device(
        db_session,
        "denom",
        ("vless-reality", "hysteria2", "vless-xhttp"),
        published=("vless-reality",),
    )
    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id,
            observed_at=utcnow(),
            details={
                "vless-reality": {"users": [device.access_username], "uplink": 1,
                                  "downlink": 1},
            },
        )
    )
    db_session.commit()

    row = next(r for r in carrying.compute_carrying_fractions(db_session)
               if r["node_id"] == node.id)
    assert row["eligible_devices"] == 1
    assert row["carrying_fraction"] == 1.0


def test_per_protocol_split_separates_transport_block_from_dead_node(
    db_session, monkeypatch
):
    """Главный вопрос метрики при 4×1 — «режут транспорт или умерла нода».
    Провал по одному протоколу при живом соседе = первое."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    node, device = _node_with_device(
        db_session, "split", ("vless-reality", "hysteria2"),
        published=("vless-reality", "hysteria2"),
    )
    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id,
            observed_at=utcnow(),
            details={
                "hysteria2": {"users": [device.access_username]},
                "vless-reality": {"users": []},
            },
        )
    )
    db_session.commit()

    row = next(r for r in carrying.compute_carrying_fractions(db_session)
               if r["node_id"] == node.id)
    by_proto = {r["proto"]: r for r in row["by_protocol"]}
    assert by_proto["hysteria2"]["carrying_fraction"] == 1.0
    assert by_proto["vless-reality"]["carrying_fraction"] == 0.0


def test_legacy_mode_keeps_old_denominator(db_session, monkeypatch):
    """Без флага метрика обязана считаться ровно как считалась."""
    monkeypatch.delenv("SUB_LEG_SCHEME", raising=False)
    node, device = _node_with_device(
        db_session, "legacy", ("vless-reality", "hysteria2"), published=("vless-reality",)
    )
    db_session.add(
        models.NodeTrafficSample(
            node_id=node.id, observed_at=utcnow(),
            details={"vless-reality": {"users": [device.access_username]}},
        )
    )
    db_session.commit()

    row = next(r for r in carrying.compute_carrying_fractions(db_session)
               if r["node_id"] == node.id)
    assert row["eligible_devices"] == 1  # device-уровень, не кред-уровень
    assert {r["proto"] for r in row["by_protocol"]} == {"vless-reality", "hysteria2"}
