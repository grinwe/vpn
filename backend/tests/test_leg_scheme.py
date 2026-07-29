"""Набор эндпоинтов 4×1: раскладка ролей и фильтр публикации.

Смысл схемы: двенадцать строк (3 ноды × 4 протокола) покрывают 3 ноды, а четыре
строки на четырёх разных нодах — 4 ноды И 4 протокола. Отсюда два свойства,
которые тут и защищаются: в подписку идёт РОВНО один протокол с ноды, а роли
раздаются паросочетанием — жадный проход терял бы роль на ноде без нужного
протокола.
"""
from __future__ import annotations

from app import models
from app.services import leg_scheme

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)


class _Cred:
    """Кред без БД: раскладка — чистая функция, ей хватает пяти полей."""

    _seq = 0

    def __init__(self, proto, node_id, *, active=True, published=False, role=None):
        _Cred._seq += 1
        self.id = _Cred._seq
        self.proto = proto
        self.node_id = node_id
        self.is_active = active
        self.leg_published = published
        self.leg_role = role


ALL_PROTOS = ("vless-reality", "hysteria2", "vless-xhttp", "vless-ws-cdn")


def _full_bundle(node_id):
    return [_Cred(proto, node_id) for proto in ALL_PROTOS]


# ── раскладка ролей ─────────────────────────────────────────────────────────


def test_four_nodes_give_four_roles_one_each():
    """Целевая схема: четыре полных бандла → по одной роли с ноды."""
    creds = [c for node in (1, 2, 3, 4) for c in _full_bundle(node)]
    plan = leg_scheme.plan_legs(creds)

    assert not plan.missing
    assert len(plan.published) == 4
    nodes = {cred.node_id for cred in plan.published}
    assert len(nodes) == 4, "две роли с одной ноды — это не 4×1"
    protos = {cred.proto for cred in plan.published}
    assert protos == set(ALL_PROTOS)
    assert plan.hidden == 12, "остальные двенадцать кредов живут, но не публикуются"


def test_matching_beats_greedy_when_protocol_is_rare():
    """Ключевой случай: hy2 поднят ровно на одной ноде, и она же несёт reality.

    Жадный проход отдал бы эту ноду роли primary (она идёт первой) и оставил
    роль fast без ноды — при том что полный набор собирался. Паросочетание
    обязано увести primary на соседнюю ноду.
    """
    creds = [
        _Cred("vless-reality", 1),
        _Cred("hysteria2", 1),  # hy2 больше нигде нет
        _Cred("vless-reality", 2),
        _Cred("vless-xhttp", 3),
        _Cred("vless-ws-cdn", 4),
    ]
    plan = leg_scheme.plan_legs(creds)

    assert not plan.missing, plan.missing
    assert plan.assigned["fast"].node_id == 1
    assert plan.assigned["primary"].node_id == 2


def test_missing_role_is_reported_not_silently_dropped():
    """Ролей больше, чем нод с нужными протоколами: дефицит обязан быть виден,
    иначе у человека молча на строку меньше, а жаловаться ему не на что."""
    creds = [_Cred("vless-reality", 1), _Cred("vless-xhttp", 2)]
    plan = leg_scheme.plan_legs(creds)

    assert sorted(plan.missing) == ["fast", "reserve"]
    assert set(plan.assigned) == {"primary", "backup"}
    assert not plan.complete


def test_inactive_creds_never_published():
    """Неактивный кред — учётки на ноде уже нет; опубликовать его значит
    показать заведомо мёртвый эндпоинт."""
    creds = [
        _Cred("vless-reality", 1, active=False),
        _Cred("vless-reality", 2),
    ]
    plan = leg_scheme.plan_legs(creds)
    assert plan.assigned["primary"].node_id == 2


def test_assignment_is_stable_across_reruns():
    """Раскладка не должна прыгать: «Основной 2» у человека в клиенте обязан
    остаться тем же сервером после повторного провижининга."""
    creds = [c for node in (1, 2, 3, 4) for c in _full_bundle(node)]
    first = leg_scheme.plan_legs(creds)
    for role, cred in first.assigned.items():
        cred.leg_published = True
        cred.leg_role = role

    second = leg_scheme.plan_legs(creds)
    assert {r: c.node_id for r, c in second.assigned.items()} == {
        r: c.node_id for r, c in first.assigned.items()
    }


def test_duplicate_leg_survives_reshuffle():
    """Дубль (Э5) живёт СВЕРХ набора: он выдан по эскалации человеку, у
    которого работает один протокол, и раскладка не имеет права его снять."""
    creds = _full_bundle(1) + _full_bundle(2) + _full_bundle(3) + _full_bundle(4)
    dup = _Cred("hysteria2", 5, published=True, role=leg_scheme.DUP_ROLE)
    creds.append(dup)

    plan = leg_scheme.plan_legs(creds)
    assert dup in plan.published
    assert len(plan.published) == 5
    assert not plan.missing


def test_target_nodes_matches_role_count(monkeypatch):
    monkeypatch.delenv("SUB_LEG_NODES", raising=False)
    assert leg_scheme.target_leg_nodes() == 4
    monkeypatch.setenv("SUB_LEG_NODES", "3")
    assert leg_scheme.target_leg_nodes() == 3
    monkeypatch.setenv("SUB_LEG_NODES", "99")
    assert leg_scheme.target_leg_nodes() == 4, "больше ролей нод не нужно"
    monkeypatch.setenv("SUB_LEG_NODES", "мусор")
    assert leg_scheme.target_leg_nodes() == 4


# ── применение к БД ─────────────────────────────────────────────────────────


def _device_with_creds(db, protos_by_node):
    plan = make_plan(db)
    user = make_user(db, telegram_id=f"leg-{len(protos_by_node)}-{id(protos_by_node)}")
    nodes = {}
    for idx, node_key in enumerate(protos_by_node):
        nodes[node_key] = make_node(
            db, name=f"leg-node-{node_key}-{id(protos_by_node)}",
            host=f"203.0.113.{100 + idx}",
        )
        make_config(db, nodes[node_key])
    first = nodes[next(iter(nodes))]
    sub = make_subscription_with_device(db, user, plan, first)
    device = sub.devices[0]
    for node_key, protos in protos_by_node.items():
        for proto in protos:
            db.add(
                models.Credential(
                    subscription_id=sub.id,
                    device_id=device.id,
                    node_id=nodes[node_key].id,
                    proto=proto,
                    config_text="enc-stub",
                    access_username=device.access_username,
                    is_active=True,
                )
            )
    db.commit()
    db.refresh(device)
    return device


def test_apply_is_noop_without_flag(db_session, monkeypatch):
    """Откат схемы должен стоить перезапуск контейнера, а не откат данных."""
    monkeypatch.delenv("SUB_LEG_SCHEME", raising=False)
    device = _device_with_creds(db_session, {"a": ALL_PROTOS, "b": ALL_PROTOS})

    assert leg_scheme.apply_leg_scheme(db_session, device) is None
    assert all(c.leg_published for c in device.credentials)


def test_apply_publishes_one_leg_per_node(db_session, monkeypatch):
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    device = _device_with_creds(
        db_session, {"a": ALL_PROTOS, "b": ALL_PROTOS, "c": ALL_PROTOS, "d": ALL_PROTOS}
    )

    result = leg_scheme.apply_leg_scheme(db_session, device, commit=True)
    assert result is not None and not result.missing

    published = [c for c in device.credentials if c.leg_published]
    assert len(published) == 4
    assert len({c.node_id for c in published}) == 4
    # Инвариант: роль есть ⟺ лег опубликован.
    for cred in device.credentials:
        assert bool(cred.leg_role) == bool(cred.leg_published), cred.proto


def test_apply_hides_extra_protocols_but_keeps_them_active(db_session, monkeypatch):
    """Скрытый лег обязан остаться живым на ноде: на этом стоит бесплатный
    первый шаг ротации — «переставить флаг» вместо ansible-прогона."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    device = _device_with_creds(db_session, {"a": ALL_PROTOS, "b": ALL_PROTOS})

    leg_scheme.apply_leg_scheme(db_session, device, commit=True)
    hidden = [c for c in device.credentials if not c.leg_published]
    assert hidden, "часть протоколов обязана уйти из публикации"
    assert all(c.is_active for c in hidden)


# ── фильтр публикации в саб-линке ───────────────────────────────────────────


def test_sub_link_shows_only_published_legs(monkeypatch):
    from app.api_extensions import _decrypt_configs
    from app.security import encrypt

    class _Row:
        def __init__(self, proto, node_id, published):
            self.proto = proto
            self.node_id = node_id
            self.is_active = True
            self.leg_published = published
            self.id = node_id * 10
            self.config_text = encrypt(f"vless://u@h{node_id}:443#raw")

    creds = [
        _Row("vless-reality", 1, True),
        _Row("hysteria2", 1, False),
        _Row("hysteria2", 2, True),
        _Row("vless-xhttp", 2, False),
    ]

    monkeypatch.delenv("SUB_LEG_SCHEME", raising=False)
    assert len(_decrypt_configs(creds, sub=None, device_id=None)) == 4

    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    out = _decrypt_configs(creds, sub=None, device_id=None)
    assert len(out) == 2
    labels = [c.uri.split("#", 1)[1] for c in out]
    assert any("Основной" in lb for lb in labels)
    assert any("Быстрый" in lb for lb in labels)


# ── Э6: перевод существующих устройств ──────────────────────────────────────


def test_backfill_relays_legacy_device_with_enough_nodes(db_session, monkeypatch):
    """Ключевой случай перехода: у девайса ноды уже набраны, но публикуются ВСЕ
    протоколы (легаси-мир, 12 строк). Добирать нечего — а переразложить надо,
    иначе переход на 4×1 обошёл бы стороной ровно тех, у кого набор широкий.
    """
    from app.services.provisioning import ProvisioningOrchestrator

    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    monkeypatch.setenv("DIVERSE_SUB_NODES", "1")
    device = _device_with_creds(
        db_session, {"a": ALL_PROTOS, "b": ALL_PROTOS, "c": ALL_PROTOS, "d": ALL_PROTOS}
    )
    device.status = models.DeviceStatus.active
    sub = device.subscription
    sub.status = models.SubscriptionStatus.active
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    result = orch.backfill_diverse_subscriptions(
        dry_run=False, user_id=sub.user_id, limit=10
    )

    assert result["leg_scheme"] == "4x1"
    assert result["legs_relaid"] >= 1
    db_session.refresh(device)
    published = [c for c in device.credentials if c.leg_published]
    assert len(published) == 4
    assert len({c.node_id for c in published}) == 4


def test_backfill_dry_run_changes_nothing(db_session, monkeypatch):
    from app.services.provisioning import ProvisioningOrchestrator

    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    monkeypatch.setenv("DIVERSE_SUB_NODES", "1")
    device = _device_with_creds(
        db_session, {"a": ALL_PROTOS, "b": ALL_PROTOS, "c": ALL_PROTOS, "d": ALL_PROTOS}
    )
    device.status = models.DeviceStatus.active
    sub = device.subscription
    sub.status = models.SubscriptionStatus.active
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    result = orch.backfill_diverse_subscriptions(
        dry_run=True, user_id=sub.user_id, limit=10
    )

    assert result["legs_relaid"] >= 1, "dry-run обязан показать охват"
    db_session.refresh(device)
    assert all(c.leg_published for c in device.credentials), "dry-run не мутирует"


def test_single_role_needs_no_number(monkeypatch):
    """При 4×1 каждая роль живёт на своём сервере, и «Основной 4» среди
    четырёх строк читается как «а где ещё три Основных?»."""
    from app.api_extensions import _decrypt_configs
    from app.security import encrypt

    class _Row:
        def __init__(self, proto, node_id):
            self.proto = proto
            self.node_id = node_id
            self.is_active = True
            self.leg_published = True
            self.id = node_id * 10
            self.config_text = encrypt(f"vless://u@h{node_id}:443#raw")

    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    creds = [
        _Row("vless-reality", 4),
        _Row("hysteria2", 2),
        _Row("vless-xhttp", 3),
        _Row("vless-ws-cdn", 1),
    ]
    labels = [c.uri.split("#", 1)[1] for c in _decrypt_configs(creds, sub=None, device_id=None)]
    assert labels == ["⚡ Основной", "🚀 Быстрый", "🛡️ Запасной", "☁️ Резервный"]


def test_duplicate_role_keeps_numbers(monkeypatch):
    """А вот после эскалации у человека два «Быстрых» на разных нодах — здесь
    цифра снова несёт смысл, и убирать её нельзя."""
    from app.api_extensions import _decrypt_configs
    from app.security import encrypt

    class _Row:
        def __init__(self, proto, node_id):
            self.proto = proto
            self.node_id = node_id
            self.is_active = True
            self.leg_published = True
            self.id = node_id * 10
            self.config_text = encrypt(f"vless://u@h{node_id}:443#raw")

    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")
    creds = [
        _Row("vless-reality", 1),
        _Row("hysteria2", 2),
        _Row("hysteria2", 3),  # дубль по эскалации
    ]
    labels = [c.uri.split("#", 1)[1] for c in _decrypt_configs(creds, sub=None, device_id=None)]
    assert labels[0] == "⚡ Основной"
    assert sorted(labels[1:]) == ["🚀 Быстрый 2", "🚀 Быстрый 3"]
