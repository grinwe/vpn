"""Активность юзера «за 24ч»: Device.last_seen_at и его потребители.

Тик traffic_stats штампует ``Device.last_seen_at`` по положительным
per-user дельтам (``_touch_devices_last_seen``); дальше поле читают
``GET /api/users`` (``last_active_at`` = max по девайсам юзера) и воронка
онбординга (шаг ``active_24h`` + loss ``trial_inactive``).
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app import models
from app.services.traffic_stats import (
    NodeStatsResult,
    ProtocolStats,
    _persist_node_result,
)
from app.time_utils import utcnow
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _cred(db: Session, node, device, username: str) -> None:
    db.add(
        models.Credential(
            node_id=node.id,
            device_id=device.id,
            subscription_id=device.subscription_id,
            access_username=username,
            is_active=True,
            proto="vless-reality",
            config_text="enc",
            pool_state=models.CredentialPoolState.assigned,
        )
    )
    db.commit()


def _result(user_bytes: dict[str, int]) -> NodeStatsResult:
    stats = ProtocolStats(uplink=sum(user_bytes.values()))
    for name, value in user_bytes.items():
        stats.users.add(name)
        stats.add_user_bytes(name, value)
    return NodeStatsResult(
        uplink_bytes=stats.uplink,
        active_users=len(user_bytes),
        per_protocol={"vless-reality": stats},
    )


def test_persist_stamps_last_seen(db_session: Session) -> None:
    node = make_node(db_session, name="ls-node")
    other = make_node(db_session, name="ls-other", host="198.51.100.11")
    cfg = make_config(db_session, node)
    plan = make_plan(db_session)

    user = make_user(db_session, telegram_id="ls-active")
    sub = make_subscription(db_session, user, plan, node)
    dev = make_device(db_session, sub, cfg, access_username="u-ls-1")
    _cred(db_session, node, dev, "u-ls-1")

    # тот же username, но кред на ДРУГОЙ ноде — штамп чужого тика не для него
    foreign = make_user(db_session, telegram_id="ls-foreign")
    fsub = make_subscription(db_session, foreign, plan, other)
    fdev = make_device(db_session, fsub, cfg, access_username="u-ls-1")
    _cred(db_session, other, fdev, "u-ls-1")

    _persist_node_result(db_session, node, _result({"u-ls-1": 4096}), 300)
    db_session.commit()

    db_session.refresh(dev)
    db_session.refresh(fdev)
    assert dev.last_seen_at is not None
    assert (utcnow() - dev.last_seen_at) < timedelta(minutes=1)
    assert fdev.last_seen_at is None


def test_stamp_survives_accounting_gate(db_session: Session, monkeypatch) -> None:
    """TRAFFIC_USER_ACCOUNTING=0 глушит начисление байтов, но НЕ активность."""
    monkeypatch.setenv("TRAFFIC_USER_ACCOUNTING", "0")
    node = make_node(db_session, name="ls-gate")
    cfg = make_config(db_session, node)
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="ls-gated")
    sub = make_subscription(db_session, user, plan, node)
    dev = make_device(db_session, sub, cfg, access_username="u-ls-gate")
    _cred(db_session, node, dev, "u-ls-gate")

    _persist_node_result(db_session, node, _result({"u-ls-gate": 2048}), 300)
    db_session.commit()

    db_session.refresh(dev)
    db_session.refresh(sub)
    assert dev.last_seen_at is not None
    assert sub.traffic_used_bytes == 0


def test_users_list_returns_last_active(client, db_session: Session) -> None:
    node = make_node(db_session, name="ls-api")
    cfg = make_config(db_session, node)
    plan = make_plan(db_session)

    user = make_user(db_session, telegram_id="ls-list")
    sub = make_subscription(db_session, user, plan, node)
    old = make_device(db_session, sub, cfg, access_username="u-ls-old")
    fresh = make_device(db_session, sub, cfg, access_username="u-ls-new")
    old.last_seen_at = utcnow() - timedelta(days=3)
    fresh.last_seen_at = utcnow() - timedelta(hours=1)
    idle = make_user(db_session, telegram_id="ls-idle")
    db_session.commit()

    rows = {r["telegram_id"]: r for r in client.get("/api/users").json()}
    active_row = rows["ls-list"]
    # max по девайсам — свежий штамп, не трёхдневный
    assert active_row["last_active_at"] is not None
    assert "ls-idle" in rows and rows["ls-idle"]["last_active_at"] is None
    got = active_row["last_active_at"]
    assert got.startswith(
        fresh.last_seen_at.strftime("%Y-%m-%dT%H:%M:%S")
    ), (got, fresh.last_seen_at, idle.id)


def test_funnel_counts_active_24h(client, db_session: Session) -> None:
    node = make_node(db_session, name="ls-funnel")
    cfg = make_config(db_session, node)
    plan = make_plan(db_session)

    # триальщик, живой: трафик видели час назад
    alive = make_user(db_session, telegram_id="ls-alive")
    alive.trial_activated_at = utcnow() - timedelta(days=2)
    asub = make_subscription(db_session, alive, plan, node)
    adev = make_device(db_session, asub, cfg, access_username="u-ls-alive")
    adev.last_seen_at = utcnow() - timedelta(hours=1)

    # триальщик, отвалившийся: последний трафик — двое суток назад
    gone = make_user(db_session, telegram_id="ls-gone")
    gone.trial_activated_at = utcnow() - timedelta(days=2)
    gsub = make_subscription(db_session, gone, plan, node)
    gdev = make_device(db_session, gsub, cfg, access_username="u-ls-gone")
    gdev.last_seen_at = utcnow() - timedelta(hours=48)
    db_session.commit()

    body = client.get("/api/admin/onboarding-funnel?days=7").json()
    steps = {s["key"]: s for s in body["steps"]}
    losses = {s["key"]: s for s in body["losses"]}

    assert steps["active_24h"]["count"] == 1
    assert steps["active_24h"]["denominator"] == body["total"]
    # из 2 забравших триал не активен один; знаменатель — триальщики
    assert losses["trial_inactive"]["count"] == 1
    assert losses["trial_inactive"]["denominator"] == 2
    assert losses["trial_inactive"]["measurable"] is True
