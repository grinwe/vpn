"""Auditfix client_control_py — находки 99 и 12.

id 99: бот-репорты «VPN не работает» (report-broken / report-broken-device)
теперь участвуют в крауд-детекте нездоровья ноды: audit-метаданные пишут
``current_node_id`` (ключ окна подсчёта) и после миграции дёргается
``_escalate_node_failure_reports`` — как в webapp-пути (_do_failover).

id 12: авто-баны NodeUserBan получили TTL (on-access prune, env
``NODE_USER_BAN_TTL_HOURS``) и потолок на юзера (``NODE_USER_BAN_MAX_PER_USER``)
— юзер с проблемой на своей стороне больше не выжигает себе пул нод навсегда.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import models
from app.api.client_control import _prune_stale_auto_bans, _should_auto_ban
from app.services.provisioning import ProvisioningOrchestrator
from app.time_utils import utcnow
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _mock_migrate(monkeypatch: pytest.MonkeyPatch, new_node, device) -> list[dict]:
    """Подменить migrate_subscription_to_free_node, записывая kwargs вызовов."""
    calls: list[dict] = []

    def fake(self, sub, **kwargs):  # noqa: ANN001, ARG001
        calls.append(kwargs)
        return new_node, device, None, kwargs.get("auto_ban_old_node", True)

    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node", fake
    )
    return calls


def _add_ban(db: Session, user, node, *, created_by: str, created_at) -> models.NodeUserBan:
    ban = models.NodeUserBan(
        user_id=user.id,
        node_id=node.id,
        reason="test",
        created_by=created_by,
        created_at=created_at,
    )
    db.add(ban)
    db.commit()
    return ban


# ── id 99: бот-репорты → крауд-детект ────────────────────────────────────


def test_bot_reports_reach_crowd_threshold_and_cool_node(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 разных юзера жмут «VPN не работает» в боте по одной ноде за окно →
    нода уходит в cooldown (раньше бот-канал в порог вообще не попадал)."""
    monkeypatch.setenv("NODE_FAILURE_BAN_THRESHOLD", "4")
    monkeypatch.setenv("NODE_FAILURE_REPORT_WINDOW_MIN", "60")

    plan = make_plan(db_session)
    bad = make_node(db_session, name="crowd-bad", region="ru")
    good = make_node(db_session, name="crowd-good", region="ru", host="10.7.7.2")
    good_cfg = make_config(db_session, good)

    users_subs = []
    for i in range(4):
        u = make_user(db_session, telegram_id=f"tg-crowd-{i}")
        s = make_subscription(db_session, u, plan, bad)
        users_subs.append((u, s))

    # Мок migrate: device нужен репорту (access_username), ansible не гоняем.
    ref_dev = make_device(
        db_session, users_subs[0][1], good_cfg, access_username="crowd-dev"
    )
    _mock_migrate(monkeypatch, good, ref_dev)

    for i, (u, _s) in enumerate(users_subs):
        r = client.post(
            "/api/admin/client-control/report-broken",
            json={"telegram_id": u.telegram_id},
        )
        assert r.status_code == 200, r.text
        assert r.json()["action"] == "migrated"

        db_session.expire_all()
        node = db_session.get(models.VPNNode, bad.id)
        if i < 3:
            assert node.cooldown_until is None, (
                f"cooldown сработал уже после {i + 1} репортов"
            )
        else:
            assert node.cooldown_until is not None
            assert node.cooldown_until > utcnow()

    # Метаданные аудита содержат ключ окна подсчёта (current_node_id).
    logs = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "client_reported_failure")
        .all()
    )
    assert len(logs) == 4
    assert all((log.extra or {}).get("current_node_id") == bad.id for log in logs)


def test_report_broken_device_escalates_crowd_health(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-device путь тоже голосует: audit пишет current_node_id и зовёт
    _escalate_node_failure_reports по старой primary-ноде."""
    import types as _types

    from app.api import client_control as cc

    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="tg-dev-crowd")
    bad = make_node(db_session, name="crowd-dev-bad", region="ru", host="10.7.7.3")
    cfg = make_config(db_session, bad)
    sub = make_subscription(db_session, user, plan, bad)
    dev = make_device(db_session, sub, cfg, access_username="D1")

    fresh = make_node(db_session, name="crowd-dev-fresh", region="ru", host="10.7.7.4")
    fresh_cfg = make_config(db_session, fresh)
    new_dev = make_device(db_session, sub, fresh_cfg, access_username="D1-new")

    fake_target = _types.SimpleNamespace(id=fresh.id, name=fresh.name, region="ru")

    def fake_failover(self, device):  # noqa: ANN001, ARG001
        return fake_target, new_dev, None, bad.id

    monkeypatch.setattr(ProvisioningOrchestrator, "failover_device", fake_failover)

    escalated: list[int] = []
    monkeypatch.setattr(
        cc,
        "_escalate_node_failure_reports",
        lambda db, node_id: escalated.append(node_id),
    )

    r = client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": user.telegram_id, "device_id": dev.id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "migrated"
    assert escalated == [bad.id]

    log = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "client_reported_failure")
        .one()
    )
    assert (log.extra or {}).get("current_node_id") == bad.id


# ── id 12: TTL + потолок авто-банов ──────────────────────────────────────


def test_prune_stale_auto_bans_ttl_keeps_manual_and_fresh(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODE_USER_BAN_TTL_HOURS", "48")
    user = make_user(db_session, telegram_id="tg-prune")
    nodes = [
        make_node(db_session, name=f"prune-{i}", region="ru", host=f"10.8.8.{i + 1}")
        for i in range(4)
    ]
    old = utcnow() - timedelta(hours=72)
    fresh = utcnow() - timedelta(hours=1)

    _add_ban(db_session, user, nodes[0], created_by="user:tg-prune", created_at=old)
    _add_ban(db_session, user, nodes[1], created_by="client_control", created_at=old)
    keep_fresh = _add_ban(
        db_session, user, nodes[2], created_by="user:tg-prune", created_at=fresh
    )
    # Ручной админ-бан не трогаем, даже протухший.
    keep_manual = _add_ban(
        db_session, user, nodes[3], created_by="admin-vasya", created_at=old
    )

    removed = _prune_stale_auto_bans(db_session, user.id)
    assert removed == 2

    left = {
        b.node_id
        for b in db_session.query(models.NodeUserBan)
        .filter(models.NodeUserBan.user_id == user.id)
        .all()
    }
    assert left == {keep_fresh.node_id, keep_manual.node_id}


def test_prune_disabled_by_zero_ttl(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODE_USER_BAN_TTL_HOURS", "0")
    user = make_user(db_session, telegram_id="tg-prune-off")
    node = make_node(db_session, name="prune-off", region="ru", host="10.8.9.1")
    _add_ban(
        db_session, user, node,
        created_by="client_control",
        created_at=utcnow() - timedelta(days=30),
    )
    assert _prune_stale_auto_bans(db_session, user.id) == 0
    assert db_session.query(models.NodeUserBan).count() == 1


def test_should_auto_ban_cap(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODE_USER_BAN_MAX_PER_USER", "3")
    user = make_user(db_session, telegram_id="tg-cap")
    fresh = utcnow() - timedelta(minutes=10)
    for i in range(2):
        node = make_node(db_session, name=f"cap-{i}", region="ru", host=f"10.8.10.{i + 1}")
        _add_ban(db_session, user, node, created_by="user:tg-cap", created_at=fresh)
    assert _should_auto_ban(db_session, user.id) is True  # 2 < 3

    node3 = make_node(db_session, name="cap-2", region="ru", host="10.8.10.3")
    _add_ban(db_session, user, node3, created_by="client_control", created_at=fresh)
    assert _should_auto_ban(db_session, user.id) is False  # 3 >= 3


def test_bot_report_broken_stops_banning_at_cap(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При потолке авто-банов миграция идёт с auto_ban_old_node=False —
    пул нод юзера больше не сгорает от тапов."""
    monkeypatch.setenv("NODE_USER_BAN_MAX_PER_USER", "2")

    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="tg-cap-bot")
    bad = make_node(db_session, name="cap-bot-bad", region="ru", host="10.8.11.1")
    good = make_node(db_session, name="cap-bot-good", region="ru", host="10.8.11.2")
    good_cfg = make_config(db_session, good)
    sub = make_subscription(db_session, user, plan, bad)
    dev = make_device(db_session, sub, good_cfg, access_username="cap-dev")

    fresh = utcnow() - timedelta(minutes=10)
    for i, name in enumerate(("cap-bot-x1", "cap-bot-x2")):
        node = make_node(db_session, name=name, region="ru", host=f"10.8.12.{i + 1}")
        _add_ban(db_session, user, node, created_by="user:tg-cap-bot", created_at=fresh)

    calls = _mock_migrate(monkeypatch, good, dev)
    r = client.post(
        "/api/admin/client-control/report-broken",
        json={"telegram_id": user.telegram_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "migrated"
    assert len(calls) == 1
    assert calls[0]["auto_ban_old_node"] is False
