"""Ядро самопочинки: один исход независимо от канала входа.

До выноса (2026-07-29) логика жила в бот-хендлере, а WebApp-путь звал
``failover_device`` напрямую — мимо лестницы ротации, и его жалобы не
считались счётчиком эскалации. То есть человек из кабинета всегда получал
самый дорогой шаг и никогда не доходил до дубля. Здесь фиксируем, что
бот и WebApp на одинаковом состоянии дают одинаковый шаг лестницы, а
публичные каналы получают троттл и суточный потолок.
"""
from __future__ import annotations

from datetime import timedelta

from app import models
from app.services import leg_scheme, self_repair
from app.time_utils import utcnow

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)

ALL_PROTOS = ("vless-reality", "hysteria2", "vless-xhttp", "vless-ws-cdn")


def _device_on_nodes(db, tag, node_count=4):
    # Имя плана уникально по тегу: тесты, зовущие фикстуру дважды, иначе
    # ловят UniqueViolation по plans.name.
    plan = make_plan(db, name=f"sr-plan-{tag}")
    user = make_user(db, telegram_id=f"sr-{tag}")
    nodes = [
        make_node(db, name=f"sr-{tag}-{i}", host=f"203.0.113.{160 + i}")
        for i in range(node_count)
    ]
    for node in nodes:
        make_config(db, node)
    sub = make_subscription_with_device(db, user, plan, nodes[0])
    device = sub.devices[0]
    for node in nodes:
        for proto in ALL_PROTOS:
            db.add(
                models.Credential(
                    subscription_id=sub.id,
                    device_id=device.id,
                    node_id=node.id,
                    proto=proto,
                    config_text="enc-stub",
                    access_username=device.access_username,
                    is_active=True,
                )
            )
    db.commit()
    # Роли легов (⚡ основной / 🚀 быстрый / …) — без них перетасовывать
    # нечего и лестница сразу падает в перенос ноды.
    leg_scheme.apply_leg_scheme(db, device, commit=True)
    db.refresh(device)
    return user, sub, device


def _complaints(db, user_id: int) -> int:
    return (
        db.query(models.AuditLog)
        .filter_by(action="complaint_received", target_type="user", target_id=user_id)
        .count()
    )


def test_first_complaint_reshuffles_for_any_source(db_session, monkeypatch):
    """Шаг лестницы зависит от состояния, а не от канала: и «бот», и
    «webapp» на первой жалобе получают перетасовку протоколов."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")

    for source in ("bot_vpn_broken", "webapp_report_broken"):
        user, _sub, device = _device_on_nodes(db_session, source)
        outcome = self_repair.handle_broken_device(
            db_session, device.id, user=user, dedup_sec=60, source=source
        )
        assert outcome.action == "reshuffled", (source, outcome)
        assert outcome.repaired is True
        assert outcome.report_id is not None


def test_source_reaches_the_audit_log(db_session, monkeypatch):
    """``source`` доезжает в audit — иначе не отличить, какой канал чинил."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, sub, device = _device_on_nodes(db_session, "src")

    self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=60, source="sub_page"
    )
    row = (
        db_session.query(models.AuditLog)
        .filter_by(action="client_reported_failure", target_id=sub.id)
        .order_by(models.AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.extra["source"] == "sub_page"


def test_complaint_is_recorded_and_debounced(db_session, monkeypatch):
    """Жалоба — топливо лестницы: пишется всегда, но двойной тап в окне
    дедупа остаётся одной жалобой (иначе порог эскалации срабатывал бы от
    одного нетерпеливого нажатия)."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, _sub, device = _device_on_nodes(db_session, "dedup")

    first = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=600
    )
    assert first.deduped is False
    assert _complaints(db_session, user.id) == 1

    second = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=600
    )
    assert second.deduped is True, "дребезг обязан схлопнуться"
    assert _complaints(db_session, user.id) == 1


def test_throttle_blocks_repeat_repair_but_keeps_the_complaint(db_session, monkeypatch):
    """Публичные каналы задают окно троттла: повторный запрос в нём не жжёт
    шаг лестницы, но жалоба всё равно фиксируется — счётчик эскалации не
    должен слепнуть."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, _sub, device = _device_on_nodes(db_session, "throttle")

    first = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0, throttle_sec=120
    )
    assert first.action == "reshuffled"

    second = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0, throttle_sec=120
    )
    assert second.action == "throttled"
    assert _complaints(db_session, user.id) == 2


def test_daily_cap_stops_pool_drain(db_session, monkeypatch):
    """Суточный потолок на устройство: утёкший sub_token иначе вычерпывает
    пул нод бесконечными починками."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, sub, device = _device_on_nodes(db_session, "cap")

    # Две «вчерашние» починки не считаются, три сегодняшних — считаются.
    for ago_h, count in ((30, 2), (1, 3)):
        for _ in range(count):
            db_session.add(
                models.OperatorNodeReport(
                    user_id=user.id,
                    subscription_id=sub.id,
                    device_id=device.id,
                    reported_at=utcnow() - timedelta(hours=ago_h),
                    outcome="pending",
                )
            )
    db_session.commit()

    outcome = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0, daily_max=3
    )
    assert outcome.action == "daily_limit"
    # Жалоба всё равно записана.
    assert _complaints(db_session, user.id) == 1


def test_foreign_device_is_refused(db_session, monkeypatch):
    """Anti-forge живёт в ядре: чужое устройство не чинится ни из какого
    канала (у страницы по токену это единственная защита)."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    _user_a, _sub_a, device_a = _device_on_nodes(db_session, "owner")
    user_b, _sub_b, _device_b = _device_on_nodes(db_session, "stranger")

    outcome = self_repair.handle_broken_device(
        db_session, device_a.id, user=user_b, dedup_sec=0
    )
    assert outcome.action == "no_subscription"
    assert outcome.repaired is False


def test_revoked_device_is_not_repaired_twice(db_session, monkeypatch):
    """Повторный тап по старой клавиатуре: устройство уже перенесено."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, _sub, device = _device_on_nodes(db_session, "revoked")
    device.status = models.DeviceStatus.revoked
    db_session.commit()

    outcome = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0
    )
    assert outcome.action == "no_subscription"
