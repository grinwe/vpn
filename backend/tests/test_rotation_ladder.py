"""Ротационная лестница: первая жалоба — протоколы, вторая — ноды, третья — дубль.

Раньше на любую жалобу был один ответ — перенести устройство на другую ноду. Но
чаще всего режут ТРАНСПОРТ (регион душит TCP-Reality), и смена ноды в этом
случае стреляет мимо: на новой ноде тот же протокол душат так же. Первый шаг
лестницы отвечает именно на это — и стоит переставленного флага в БД.
"""
from __future__ import annotations

from datetime import timedelta

from app import models
from app.services import leg_scheme, rotation
from app.time_utils import utcnow

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)

ALL_PROTOS = ("vless-reality", "hysteria2", "vless-xhttp", "vless-ws-cdn")


def _device_on_nodes(db, tag, node_count=4, protos=ALL_PROTOS):
    plan = make_plan(db)
    user = make_user(db, telegram_id=f"rot-{tag}")
    nodes = [
        make_node(db, name=f"rot-{tag}-{i}", host=f"203.0.113.{140 + i}")
        for i in range(node_count)
    ]
    for node in nodes:
        make_config(db, node)
    sub = make_subscription_with_device(db, user, plan, nodes[0])
    device = sub.devices[0]
    for node in nodes:
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
                )
            )
    db.commit()
    db.refresh(device)
    return user, sub, device, nodes


def _complain(db, user, *, count=1, ago_sec=0):
    for _ in range(count):
        db.add(
            models.AuditLog(
                actor=str(user.telegram_id),
                actor_type=models.AuditActor.user,
                action="complaint_received",
                target_type="user",
                target_id=user.id,
                created_at=utcnow() - timedelta(seconds=ago_sec),
                extra={},
            )
        )
    db.commit()


# ── выбор шага ──────────────────────────────────────────────────────────────


def test_ladder_is_off_without_scheme(db_session, monkeypatch):
    """Без 4×1 перетасовывать нечего — публикуются все протоколы сразу."""
    monkeypatch.delenv("SUB_LEG_SCHEME", raising=False)
    user, *_ = _device_on_nodes(db_session, "off")
    assert rotation.decide_step(db_session, user.id) == rotation.STEP_RELOCATE


def test_ladder_steps_in_order(db_session, monkeypatch):
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, *_ = _device_on_nodes(db_session, "order")

    _complain(db_session, user)
    assert rotation.decide_step(db_session, user.id) == rotation.STEP_RESHUFFLE
    _complain(db_session, user)
    assert rotation.decide_step(db_session, user.id) == rotation.STEP_RELOCATE
    _complain(db_session, user)
    assert rotation.decide_step(db_session, user.id) == rotation.STEP_DUPLICATE


def test_old_complaints_do_not_escalate(db_session, monkeypatch):
    """Жалоба неделю спустя — новая история: регион мог разблокироваться, ноды
    сменились. Начинать её надо снова с дешёвого шага."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, *_ = _device_on_nodes(db_session, "stale")
    _complain(db_session, user, count=5, ago_sec=rotation.ladder_window_sec() + 60)
    _complain(db_session, user)
    assert rotation.decide_step(db_session, user.id) == rotation.STEP_RESHUFFLE


# ── шаг 1: перетасовка ──────────────────────────────────────────────────────


def test_reshuffle_keeps_nodes_and_changes_protocols(db_session, monkeypatch):
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    _, _, device, _ = _device_on_nodes(db_session, "shuffle")
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)
    before = leg_scheme.current_pairs(device)
    before_nodes = {node for _, node in before}

    plan = rotation.reshuffle_legs(db_session, device)
    assert plan is not None

    after = leg_scheme.current_pairs(device)
    assert after != before, "набор обязан измениться, иначе кнопка врёт"
    assert {node for _, node in after} == before_nodes, "ноды те же — это шаг 1"
    # На каждой ноде теперь ДРУГОЙ протокол.
    assert not (after & before)


def test_reshuffle_moves_hy2_to_another_node(db_session, monkeypatch):
    """hy2 обязан уехать НОДОЙ: его устойчивость в транспорте, и если не
    работает он — TCP-варианты на этой ноде уже есть и тоже не работают."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    _, _, device, _ = _device_on_nodes(db_session, "hy2move")
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)
    fast_before = next(
        c.node_id for c in device.credentials if c.leg_published and c.leg_role == "fast"
    )

    rotation.reshuffle_legs(db_session, device)
    fast_after = next(
        c.node_id for c in device.credentials if c.leg_published and c.leg_role == "fast"
    )
    assert fast_after != fast_before


def test_reshuffle_gives_up_when_nothing_to_swap(db_session, monkeypatch):
    """Одна нода с одним протоколом: перетасовать нечего. Возвращать «готово»
    при неизменном наборе нельзя — человек увидит ровно тот же список."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    _, _, device, _ = _device_on_nodes(
        db_session, "stuck", node_count=1, protos=("vless-reality",)
    )
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)

    assert rotation.reshuffle_legs(db_session, device) is None


def test_reshuffle_does_not_touch_credentials_on_nodes(db_session, monkeypatch):
    """Главное свойство шага 1 — он бесплатный: ни ansible, ни новых кредов."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    _, _, device, _ = _device_on_nodes(db_session, "cheap")
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)
    ids_before = {c.id for c in device.credentials}
    active_before = {c.id for c in device.credentials if c.is_active}

    rotation.reshuffle_legs(db_session, device)
    db_session.refresh(device)

    assert {c.id for c in device.credentials} == ids_before
    assert {c.id for c in device.credentials if c.is_active} == active_before


# ── шаг 3: дубль ────────────────────────────────────────────────────────────


def test_duplicate_prefers_hy2_from_another_node(db_session, monkeypatch):
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    _, _, device, _ = _device_on_nodes(db_session, "dup")
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)
    fast_node = next(
        c.node_id for c in device.credentials if c.leg_published and c.leg_role == "fast"
    )

    dup = rotation.grant_duplicate_leg(db_session, device)
    assert dup is not None
    assert dup.proto == "hysteria2"
    assert dup.node_id != fast_node, "дубль на той же ноде бессмыслен — ляжет нода, умрут оба"
    assert dup.leg_role == leg_scheme.DUP_ROLE and dup.leg_published


def test_duplicate_respects_cap(db_session, monkeypatch):
    """Дубли платные — каждый лишний кред и лишняя строка. Потолок жёсткий."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    monkeypatch.setenv("SUB_LEG_DUP_MAX", "1")
    _, _, device, _ = _device_on_nodes(db_session, "dupcap")
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)

    assert rotation.grant_duplicate_leg(db_session, device) is not None
    assert rotation.grant_duplicate_leg(db_session, device) is None


def test_duplicate_survives_next_reshuffle(db_session, monkeypatch):
    """Дубль выдан по эскалации — раскладка не имеет права снять его при
    следующем шаге лестницы."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    _, _, device, _ = _device_on_nodes(db_session, "dupkeep")
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)
    dup = rotation.grant_duplicate_leg(db_session, device)

    rotation.reshuffle_legs(db_session, device)
    db_session.refresh(dup)
    assert dup.leg_published and dup.leg_role == leg_scheme.DUP_ROLE


# ── эндпоинт целиком ────────────────────────────────────────────────────────


def test_endpoint_first_complaint_reshuffles_not_migrates(client, db_session, monkeypatch):
    """Сквозь ручку: первая жалоба не должна тратить свежую ноду."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, _, device, _ = _device_on_nodes(db_session, "api1")
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)
    before = leg_scheme.current_pairs(device)

    resp = client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": user.telegram_id, "device_id": device.id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "reshuffled"

    db_session.refresh(device)
    assert leg_scheme.current_pairs(device) != before


def test_reshuffle_does_not_blame_the_node(client, db_session, monkeypatch):
    """Перетасовка отвечает на «режут транспорт», а не «нода мертва». Записать
    ноду в failed_node_id значило бы влить фальшивый fail в крауд-матрицу
    node×operator и своими руками выжигать здоровые ноды."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, _, device, _ = _device_on_nodes(db_session, "api2")
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)

    resp = client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": user.telegram_id, "device_id": device.id},
    )
    report = db_session.get(models.OperatorNodeReport, resp.json()["report_id"])
    assert report.failed_node_id is None
    assert report.outcome == "pending"


def test_endpoint_records_complaint_for_the_ladder(client, db_session, monkeypatch):
    """Счётчик лестницы наполняется самим фактом жалобы, а не только троттлом."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    user, _, device, _ = _device_on_nodes(db_session, "api3")
    leg_scheme.apply_leg_scheme(db_session, device, commit=True)

    client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": user.telegram_id, "device_id": device.id},
    )
    assert rotation.complaints_in_window(db_session, user.id) == 1
