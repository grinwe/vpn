"""Per-user учёт трафика: коллектор → Subscription.traffic_used_bytes → сброс.

Раньше per-user разбивку, которую отдают и xray (`statsquery --reset`), и
hysteria (`/traffic?clear=1`), тик выбрасывал, суммируя в счётчик ноды, — и
шкала трафика у юзера была вечным «0B». Теперь дельты копятся в
``Subscription.traffic_used_bytes`` (информационный счётчик, БЕЗ блокировки —
блокирующий ингест api/traffic.py удалён), а каждое продление обнуляет их:
шкала показывает «за текущий оплаченный период».
"""
from __future__ import annotations

from app import models
from app.services.traffic_stats import (
    NodeStatsResult,
    ProtocolStats,
    _apply_user_traffic,
    _parse_hysteria_traffic,
    _parse_xray_stats_payload,
)
from tests.factories import make_node, make_plan, make_subscription_with_device, make_user


def _sub_with_creds(db, username: str = "user-1-1"):
    node = make_node(db)
    user = make_user(db)
    plan = make_plan(db, traffic_limit_mb=None)
    sub = make_subscription_with_device(db, user, plan, node, access_username=username)
    # Два креда с одним именем (разные протоколы одного warm-бандла) — маппинг
    # обязан схлопнуть их в одну подписку, а не удвоить дельту.
    for proto in ("vless-reality", "hysteria2"):
        db.add(models.Credential(
            subscription_id=sub.id,
            node_id=node.id,
            proto=proto,
            config_text="stub",
            access_username=username,
        ))
    db.commit()
    return node, sub


def test_parsers_capture_user_bytes():
    xray = _parse_xray_stats_payload(
        '{"stat": ['
        '{"name": "user>>>alice>>>traffic>>>uplink", "value": "100"},'
        '{"name": "user>>>alice>>>traffic>>>downlink", "value": "900"},'
        '{"name": "user>>>bob>>>traffic>>>downlink", "value": "5"}]}'
    )
    assert xray.user_bytes == {"alice": 1000, "bob": 5}

    hy2 = _parse_hysteria_traffic('{"alice": {"tx": 70, "rx": 30}, "idle": {"tx": 0, "rx": 0}}')
    assert hy2.user_bytes == {"alice": 100}
    # Молчуны (0 байт) не считаются активными и не попадают в байты.
    assert "idle" not in hy2.user_bytes


def test_user_bytes_accumulate_into_subscription(db_session):
    node, sub = _sub_with_creds(db_session)

    applied = _apply_user_traffic(
        db_session, node,
        {"user-1-1": 1_000_000, "warm-9-abcd": 500},  # warm без подписки — мимо
    )
    db_session.commit()
    db_session.refresh(sub)
    assert applied == 1
    assert sub.traffic_used_bytes == 1_000_000

    # Второй тик докидывает, а не перезаписывает.
    _apply_user_traffic(db_session, node, {"user-1-1": 250})
    db_session.commit()
    db_session.refresh(sub)
    assert sub.traffic_used_bytes == 1_000_250


def test_accounting_kill_switch(db_session, monkeypatch):
    node, sub = _sub_with_creds(db_session)
    monkeypatch.setenv("TRAFFIC_USER_ACCOUNTING", "0")
    assert _apply_user_traffic(db_session, node, {"user-1-1": 999}) == 0
    db_session.commit()
    db_session.refresh(sub)
    assert sub.traffic_used_bytes == 0


def test_persist_node_result_applies_user_bytes(db_session):
    from app.services.traffic_stats import _persist_node_result

    node, sub = _sub_with_creds(db_session)
    result = NodeStatsResult(
        uplink_bytes=10, downlink_bytes=90, active_users=1,
        per_protocol={
            "vless-reality": ProtocolStats(
                uplink=10, downlink=90,
                users={"user-1-1"}, user_bytes={"user-1-1": 100},
            ),
        },
    )
    _persist_node_result(db_session, node, result, interval_seconds=300)
    db_session.commit()
    db_session.refresh(sub)
    assert sub.traffic_used_bytes == 100

    # details несёт разбивку — верификация учёта без SSH на ноду.
    sample = (
        db_session.query(models.NodeTrafficSample)
        .filter_by(node_id=node.id).one()
    )
    assert sample.details["vless-reality"]["user_bytes"] == {"user-1-1": 100}


def test_renewal_resets_the_meter(db_session):
    """Продление = новый оплаченный период = шкала с нуля."""
    from app.services import balance

    node, sub = _sub_with_creds(db_session)
    sub.traffic_used_bytes = 777
    user = db_session.get(models.User, sub.user_id)
    user.balance_kopecks = 10_000_000
    sub.auto_renew = True
    db_session.commit()

    assert balance.renew_subscription(db_session, sub) is True
    db_session.commit()
    db_session.refresh(sub)
    assert sub.traffic_used_bytes == 0


def test_blocking_traffic_ingest_is_gone(client):
    """Ручка с авто-блокировкой (used > limit → block + ревок) удалена:
    счётчик теперь информационный, и возвращение ручки должно быть
    осознанным решением, а не случайностью."""
    resp = client.post("/api/nodes/1/traffic", json={"samples": []})
    assert resp.status_code in (404, 405)
    resp = client.post("/api/subscriptions/1/traffic", json={"used_mb": 1})
    assert resp.status_code in (404, 405)
