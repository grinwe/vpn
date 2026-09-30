"""Унификация флоу «VPN не работает» (2026-09-12): один контракт на все каналы.

До унификации у бота, кабинета и страницы по токену было 15 расхождений
(docs/operations/vpn_broken_channels_parity_2026_09_12.md): троттл был только
у страницы, whole-sub путь не писал жалобу, pending-устройство «чинилось»
вторым холодным провижном, чужое устройство из кабинета отвечало 404, а
забаненный владелец мог жечь слоты со страницы. Здесь фиксируем, что ядро
``services/self_repair.py`` и три его адаптера (admin-эндпоинты бота,
``/api/webapp/*``, ``POST /api/sub/{token}?fix=1``) отвечают одинаково.

Ansible/Redis не трогаем: перенос ноды мокается на уровне
``ProvisioningOrchestrator``; перетасовка протоколов (первый шаг лестницы)
идёт по-настоящему — это только флаги в БД.
"""
from __future__ import annotations

import itertools
import types as _types
from datetime import timedelta

import pytest

from app import models
from app.api import sub_fix
from app.api_webapp import issue_token
from app.config import get_settings
from app.services import leg_scheme, self_repair
from app.services.provisioning import ProvisioningOrchestrator
from app.time_utils import utcnow

from .factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_subscription_with_device,
    make_user,
)

ALL_PROTOS = ("vless-reality", "hysteria2", "vless-xhttp", "vless-ws-cdn")
# Уникальные хосты нод в пределах процесса (в одном тесте нод несколько).
_ip_seq = itertools.count(1)


def _host() -> str:
    n = next(_ip_seq)
    return f"10.77.{n // 250}.{n % 250 + 1}"


def _cfg(db, node) -> models.VPNConfig:
    return db.query(models.VPNConfig).filter_by(node_id=node.id).first()


HTML = {"accept": "text/html,application/xhtml+xml"}
POLICY_ENV = (
    "SELF_REPAIR_THROTTLE_SEC",
    "SELF_REPAIR_DAILY_MAX",
    "SUB_FIX_THROTTLE_SEC",
    "SUB_FIX_DAILY_MAX",
)


# ── Фикстуры и хелперы ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_policy_env(monkeypatch):
    """Единая политика читается из окружения при каждом вызове — чистим,
    чтобы дефолты (120 с / 5 в сутки) не зависели от env разработчика."""
    for name in POLICY_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Лимит POST'а страницы ключуется по токену и живёт в памяти процесса."""
    from app.rate_limit import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def ladder(db_session, monkeypatch):
    """Фабрика «устройство на N нодах со всеми протоколами» — состояние, в
    котором лестница ротации на первой жалобе делает перетасовку (без
    ansible). Возвращает ``(user, sub, device, nodes)``."""
    monkeypatch.setenv("SUB_LEG_SCHEME", "4x1")

    def make(tag: str, *, node_count: int = 4, token: str | None = None):
        plan = make_plan(db_session, name=f"ru-plan-{tag}")
        user = make_user(db_session, telegram_id=f"ru-{tag}")
        nodes = [
            make_node(db_session, name=f"ru-{tag}-{i}", host=_host())
            for i in range(node_count)
        ]
        for node in nodes:
            make_config(db_session, node)
        sub = make_subscription_with_device(db_session, user, plan, nodes[0])
        device = sub.devices[0]
        if token:
            device.sub_token = token
        for node in nodes:
            for proto in ALL_PROTOS:
                db_session.add(
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
        db_session.commit()
        leg_scheme.apply_leg_scheme(db_session, device, commit=True)
        db_session.refresh(device)
        return user, sub, device, nodes

    return make


def _plain_sub(db, tag: str):
    """Подписка с одной нодой и одним устройством — для гейтов и пре-чеков,
    где лестница не нужна."""
    node = make_node(db, name=f"pl-{tag}", host=_host())
    cfg = make_config(db, node)
    plan = make_plan(db, name=f"pl-plan-{tag}")
    user = make_user(db, telegram_id=f"pl-{tag}")
    sub = make_subscription(db, user, plan, node)
    device = make_device(db, sub, cfg, access_username=f"pl-{tag}-u")
    return user, sub, device, node


def _report(db, user, sub, *, ago_sec: int = 0, node=None, outcome="pending"):
    r = models.OperatorNodeReport(
        user_id=user.id,
        subscription_id=sub.id,
        failed_node_id=node.id if node else None,
        target_node_id=node.id if node else None,
        reported_at=utcnow() - timedelta(seconds=ago_sec),
        outcome=outcome,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _complaints(db, user_id: int) -> int:
    return (
        db.query(models.AuditLog)
        .filter_by(action="complaint_received", target_type="user", target_id=user_id)
        .count()
    )


def _reports(db, sub_id: int) -> int:
    return (
        db.query(models.OperatorNodeReport)
        .filter_by(subscription_id=sub_id)
        .count()
    )


def _auth(user_id: int) -> dict:
    token = issue_token(user_id, get_settings().webapp_jwt_secret, 600)
    return {"Authorization": f"Bearer {token}"}


def _fake_whole_sub_migrate(monkeypatch, db, sub, *, tag: str):
    """Подменяет whole-sub миграцию: новая нода + устройство на ней, без
    ansible. Возвращает новую ноду."""
    fresh = make_node(db, name=f"fresh-{tag}", region="nl", host=_host())
    cfg = make_config(db, fresh)
    new_dev = make_device(db, sub, cfg, access_username=f"fresh-{tag}-u")
    monkeypatch.setattr(
        ProvisioningOrchestrator,
        "migrate_subscription_to_free_node",
        lambda self, s, **k: (fresh, new_dev, None, True),
    )
    return fresh


def _fake_device_failover(monkeypatch, db, sub, device, *, tag: str):
    """Подменяет per-device перенос (третий вызов лестницы = смена ноды)."""
    fresh = make_node(db, name=f"dfo-{tag}", region="nl", host=_host())
    cfg = make_config(db, fresh)
    new_dev = make_device(db, sub, cfg, access_username=f"dfo-{tag}-u")
    new_dev.name = device.name
    db.commit()
    old_node_id = sub.node_id

    def fake(self, dev):
        assert dev.id == device.id
        return fresh, new_dev, None, old_node_id

    monkeypatch.setattr(ProvisioningOrchestrator, "failover_device", fake)
    return fresh


# ── 1. Ядро ──────────────────────────────────────────────────────────────


def test_policy_defaults_read_env_with_fallback(monkeypatch):
    """SELF_REPAIR_* — канонические имена; SUB_FIX_* — fallback (прод
    настроен через них); мусор в значении пропускается, а не роняет."""
    assert self_repair.default_throttle_sec() == 120
    assert self_repair.default_daily_max() == 5

    monkeypatch.setenv("SUB_FIX_THROTTLE_SEC", "45")
    monkeypatch.setenv("SUB_FIX_DAILY_MAX", "7")
    assert self_repair.default_throttle_sec() == 45
    assert self_repair.default_daily_max() == 7

    monkeypatch.setenv("SELF_REPAIR_THROTTLE_SEC", "90")
    assert self_repair.default_throttle_sec() == 90, "каноническое имя важнее"

    monkeypatch.setenv("SELF_REPAIR_DAILY_MAX", "garbage")
    assert self_repair.default_daily_max() == 7, "мусор → следующее имя"


def test_default_throttle_applies_when_not_passed(db_session, ladder, monkeypatch):
    """Вызов без throttle_sec берёт окно из env: повтор внутри окна →
    throttled с retry_after_sec, а не второй шаг лестницы."""
    monkeypatch.setenv("SELF_REPAIR_THROTTLE_SEC", "300")
    user, _sub, device, _nodes = ladder("envthr")

    first = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0
    )
    assert first.action == "reshuffled"

    second = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0
    )
    assert second.action == "throttled"
    assert second.retry_after_sec is not None
    assert 0 < second.retry_after_sec <= 300
    assert second.scope == "device"
    assert second.device_id == device.id
    # Жалоба внутри троттла всё равно записана (dedup_sec=0).
    assert _complaints(db_session, user.id) == 2


def test_explicit_zero_throttle_disables_window(db_session, ladder, monkeypatch):
    """Явный ``throttle_sec=0`` — окно выключено даже при env-дефолте:
    вторая жалоба идёт на следующий шаг лестницы (перенос)."""
    monkeypatch.setenv("SELF_REPAIR_THROTTLE_SEC", "300")
    user, sub, device, _nodes = ladder("zero")
    _fake_device_failover(monkeypatch, db_session, sub, device, tag="zero")

    first = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0, throttle_sec=0
    )
    assert first.action == "reshuffled"
    second = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0, throttle_sec=0
    )
    assert second.action == "migrated", second
    assert second.repaired is True


def test_pending_device_is_not_ready(db_session):
    """pending = ещё собирается: чинить нельзя (второй холодный провижн
    поверх первого), но это и не «нет подписки»."""
    user, sub, device, _node = _plain_sub(db_session, "pending")
    device.status = models.DeviceStatus.pending
    db_session.commit()

    outcome = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0
    )
    assert outcome.action == "not_ready"
    assert outcome.device_id == device.id
    assert outcome.repaired is False
    assert _reports(db_session, sub.id) == 0


def test_banned_user_is_refused_without_complaint(db_session, monkeypatch):
    """Глобальный бан — ни per-device, ни whole-sub, и жалоба не пишется:
    забаненный не должен двигать счётчик эскалации."""
    user, sub, device, _node = _plain_sub(db_session, "banned")
    user.banned_at = utcnow()
    db_session.commit()
    monkeypatch.setattr(
        ProvisioningOrchestrator, "failover_device",
        lambda self, d: pytest.fail("забаненного чинить нельзя"),
    )
    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node",
        lambda self, s, **k: pytest.fail("забаненного чинить нельзя"),
    )

    per_device = self_repair.handle_broken_device(
        db_session, device.id, user=user, dedup_sec=0
    )
    assert per_device.action == "no_subscription"
    whole = self_repair.handle_broken_subscription(
        db_session, sub, user=user, dedup_sec=0
    )
    assert whole.action == "no_subscription"
    assert whole.scope == "subscription"
    assert _complaints(db_session, user.id) == 0
    assert self_repair.user_may_repair(user) is False


def test_repair_wait_is_a_pure_precheck(db_session):
    """(None, None) без починок; throttled с остатком окна; daily_limit с
    остатком до освобождения суток; ничего не пишет."""
    user, sub, _device, node = _plain_sub(db_session, "wait")
    assert self_repair.repair_wait(db_session, sub, throttle_sec=120, daily_max=5) == (None, None)

    _report(db_session, user, sub, ago_sec=30, node=node)
    wait, reason = self_repair.repair_wait(db_session, sub, throttle_sec=120, daily_max=5)
    assert reason == "throttled"
    assert wait is not None and 60 <= wait <= 91, wait  # 120 - 30 (+1 вверх)

    # Вне окна троттла — снова можно.
    assert self_repair.repair_wait(db_session, sub, throttle_sec=20, daily_max=5) == (None, None)

    # Потолок: две починки за сутки при daily_max=2 → ждать до выхода
    # старшей из «лишних» за 24 ч.
    _report(db_session, user, sub, ago_sec=3600, node=node)
    wait, reason = self_repair.repair_wait(db_session, sub, throttle_sec=0, daily_max=2)
    assert reason == "daily_limit"
    assert wait is not None and 22 * 3600 <= wait <= 23 * 3600 + 2, wait

    # daily_max=0 = потолка нет; троттл при этом свой.
    assert self_repair.repair_wait(db_session, sub, throttle_sec=0, daily_max=0) == (None, None)

    # Пре-чек ничего не пишет.
    assert _complaints(db_session, user.id) == 0
    assert _reports(db_session, sub.id) == 2


def test_repair_wait_uses_env_defaults(db_session, monkeypatch):
    """Без аргументов пре-чек считает той же арифметикой, что гейт ядра."""
    user, sub, _device, node = _plain_sub(db_session, "waitenv")
    monkeypatch.setenv("SELF_REPAIR_THROTTLE_SEC", "600")
    _report(db_session, user, sub, ago_sec=200, node=node)
    wait, reason = self_repair.repair_wait(db_session, sub)
    assert reason == "throttled"
    assert wait is not None and 380 <= wait <= 401


def test_live_devices_is_active_and_failed_only(db_session):
    """Пикеры бота и кабинета берут ОДИН набор: active + failed, без
    pending/disabled/revoked, по id."""
    user, sub, first, node = _plain_sub(db_session, "live")
    cfg = _cfg(db_session, node)
    statuses = {
        "failed": models.DeviceStatus.failed,
        "pending": models.DeviceStatus.pending,
        "disabled": models.DeviceStatus.disabled,
        "revoked": models.DeviceStatus.revoked,
        "active2": models.DeviceStatus.active,
    }
    made = {}
    for tag, status in statuses.items():
        d = make_device(db_session, sub, cfg, access_username=f"live-{tag}")
        d.status = status
        d.name = tag
        made[tag] = d
    db_session.commit()
    db_session.refresh(sub)

    live = self_repair.live_devices(sub)
    assert [d.id for d in live] == sorted(
        [first.id, made["failed"].id, made["active2"].id]
    )
    assert self_repair.live_devices(None) == []


def test_handle_broken_subscription_records_complaint_and_throttles(
    db_session, monkeypatch
):
    """Whole-sub путь теперь пишет complaint_received и живёт на единой
    политике: недавний OperatorNodeReport → throttled без миграции."""
    user, sub, _device, node = _plain_sub(db_session, "wsthr")
    _report(db_session, user, sub, ago_sec=10, node=node)
    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node",
        lambda self, s, **k: pytest.fail("внутри троттла миграции быть не должно"),
    )

    outcome = self_repair.handle_broken_subscription(
        db_session, sub, user=user, dedup_sec=0, throttle_sec=120
    )
    assert outcome.action == "throttled"
    assert outcome.scope == "subscription"
    assert outcome.retry_after_sec is not None and outcome.retry_after_sec > 0
    assert _complaints(db_session, user.id) == 1
    assert _reports(db_session, sub.id) == 1, "нового репорта нет"


def test_handle_broken_subscription_migrates_whole_sub(db_session, monkeypatch):
    user, sub, _device, node = _plain_sub(db_session, "wsmig")
    fresh = _fake_whole_sub_migrate(monkeypatch, db_session, sub, tag="wsmig")

    outcome = self_repair.handle_broken_subscription(
        db_session, sub, user=user, dedup_sec=0, source="webapp_report_broken",
        operator="mts",
    )
    assert outcome.action == "migrated"
    assert outcome.scope == "subscription"
    assert outcome.repaired is True
    assert outcome.report_id is not None
    assert outcome.new_node_name == fresh.name
    assert outcome.new_node_region == "nl"
    assert outcome.device_name is None, "whole-sub — без имени устройства"

    report = db_session.get(models.OperatorNodeReport, outcome.report_id)
    assert report.subscription_id == sub.id
    assert report.failed_node_id == node.id
    assert report.target_node_id == fresh.id
    assert report.operator == "mts"
    assert _complaints(db_session, user.id) == 1

    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="client_reported_failure", target_id=sub.id)
        .order_by(models.AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    assert audit.extra["source"] == "webapp_report_broken"
    assert audit.extra["scope"] == "subscription"


def test_handle_broken_subscription_without_live_sub(db_session, monkeypatch):
    """None / истёкшая / чужая подписка → no_subscription, без жалобы."""
    user, sub, _device, _node = _plain_sub(db_session, "wsnone")
    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node",
        lambda self, s, **k: pytest.fail("чинить нечего"),
    )

    none = self_repair.handle_broken_subscription(
        db_session, None, user=user, dedup_sec=0
    )
    assert none.action == "no_subscription"
    assert none.scope == "subscription"

    sub.expires_at = utcnow() - timedelta(hours=1)
    db_session.commit()
    expired = self_repair.handle_broken_subscription(
        db_session, sub, user=user, dedup_sec=0
    )
    assert expired.action == "no_subscription"

    stranger = make_user(db_session, telegram_id="wsnone-stranger")
    sub.expires_at = utcnow() + timedelta(days=1)
    db_session.commit()
    foreign = self_repair.handle_broken_subscription(
        db_session, sub, user=stranger, dedup_sec=0
    )
    assert foreign.action == "no_subscription"
    assert _complaints(db_session, user.id) == 0
    assert _complaints(db_session, stranger.id) == 0


# ── 2. Бот-эндпоинты (admin-token) ───────────────────────────────────────


def test_bot_report_broken_is_throttled_by_unified_policy(client, db_session, monkeypatch):
    """Старого per-user 5-мин троттла нет: whole-sub кнопка бота отвечает
    throttled по OperatorNodeReport подписки, с retry_after_sec, и жалоба
    записывается."""
    user, sub, _device, node = _plain_sub(db_session, "botthr")
    _report(db_session, user, sub, ago_sec=5, node=node)
    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node",
        lambda self, s, **k: pytest.fail("внутри троттла миграции быть не должно"),
    )

    r = client.post(
        "/api/admin/client-control/report-broken",
        json={"telegram_id": user.telegram_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "throttled"
    assert body["scope"] == "subscription"
    assert body["retry_after_sec"] and 0 < body["retry_after_sec"] <= 120
    assert _complaints(db_session, user.id) == 1


def test_bot_report_broken_migrates_whole_sub(client, db_session, monkeypatch):
    user, sub, _device, _node = _plain_sub(db_session, "botmig")
    fresh = _fake_whole_sub_migrate(monkeypatch, db_session, sub, tag="botmig")

    r = client.post(
        "/api/admin/client-control/report-broken",
        json={"telegram_id": user.telegram_id, "operator": "beeline"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "migrated"
    assert body["scope"] == "subscription"
    assert body["new_node_name"] == fresh.name
    assert body["report_id"] is not None
    assert body["device_name"] is None
    assert db_session.get(models.OperatorNodeReport, body["report_id"]).operator == "beeline"

    unknown = client.post(
        "/api/admin/client-control/report-broken", json={"telegram_id": "nobody"}
    )
    assert unknown.json()["action"] == "user_not_found"


def test_devices_by_telegram_hides_pending_and_reports_wait(client, db_session):
    user, sub, active, node = _plain_sub(db_session, "dbt")
    active.name = "Телефон"
    pending = make_device(db_session, sub, _cfg(db_session, node), access_username="dbt-pend")
    pending.name = "Ещё собирается"
    pending.status = models.DeviceStatus.pending
    db_session.commit()

    url = f"/api/admin/client-control/devices-by-telegram?telegram_id={user.telegram_id}"
    r = client.get(url)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["name"] for d in body["devices"]] == ["Телефон"]
    assert body["devices"][0] == {"device_id": active.id, "name": "Телефон", "status": "active"}
    assert body["retry_after_sec"] is None
    assert body["wait_reason"] is None
    assert body["subscription_id"] == sub.id

    _report(db_session, user, sub, ago_sec=5, node=node)
    body = client.get(url).json()
    assert body["wait_reason"] == "throttled"
    assert 0 < body["retry_after_sec"] <= 120
    assert body["subscription_id"] == sub.id

    # Суточный потолок (дефолт 5) важнее троттла в причине ожидания.
    for ago in (3600, 7200, 10800, 14400):
        _report(db_session, user, sub, ago_sec=ago, node=node)
    body = client.get(url).json()
    assert body["wait_reason"] == "daily_limit"
    assert body["retry_after_sec"] > 3600


def test_bot_report_broken_device_scope_is_device(client, db_session, ladder):
    user, sub, device, _nodes = ladder("botdev")
    device.name = "Планшет"
    db_session.commit()

    r = client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": user.telegram_id, "device_id": device.id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "reshuffled"
    assert body["scope"] == "device"
    assert body["device_name"] == "Планшет"
    assert body["report_id"] is not None
    assert body["retry_after_sec"] is None


def test_bot_report_broken_device_pending_is_not_ready(client, db_session):
    user, _sub, device, _node = _plain_sub(db_session, "botpend")
    device.status = models.DeviceStatus.pending
    db_session.commit()
    r = client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": user.telegram_id, "device_id": device.id},
    )
    assert r.json()["action"] == "not_ready"


# ── 3. Кабинет (Bearer JWT) ──────────────────────────────────────────────


def test_webapp_repair_state_mirrors_bot_precheck(client, db_session):
    user, sub, active, node = _plain_sub(db_session, "wstate")
    active.name = "Ноут"
    pending = make_device(db_session, sub, _cfg(db_session, node), access_username="wstate-p")
    pending.status = models.DeviceStatus.pending
    db_session.commit()

    r = client.get("/api/webapp/repair-state", headers=_auth(user.id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["devices"] == [{"id": active.id, "name": "Ноут", "status": "active"}]
    assert body["retry_after_sec"] is None
    assert body["wait_reason"] is None
    assert body["subscription_id"] == sub.id

    _report(db_session, user, sub, ago_sec=5, node=node)
    body = client.get("/api/webapp/repair-state", headers=_auth(user.id)).json()
    assert body["wait_reason"] == "throttled"
    assert 0 < body["retry_after_sec"] <= 120
    # Внутри окна жалоба фиксируется пре-чеком (как у бота).
    assert _complaints(db_session, user.id) == 1

    nobody = make_user(db_session, telegram_id="wstate-nosub")
    body = client.get("/api/webapp/repair-state", headers=_auth(nobody.id)).json()
    assert body == {
        "devices": [], "retry_after_sec": None, "wait_reason": None,
        "subscription_id": None,
    }
    assert client.get("/api/webapp/repair-state").status_code == 401


def test_webapp_report_broken_device_first_complaint_reshuffles(client, db_session, ladder):
    user, sub, device, _nodes = ladder("wdev")
    device.name = "Телефон"
    db_session.commit()

    r = client.post(
        "/api/webapp/report-broken-device",
        json={"device_id": device.id},
        headers=_auth(user.id),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "reshuffled"
    assert body["migrated"] is True, "совместимость: старый фронт читает migrated"
    assert body["scope"] == "device"
    assert body["device_name"] == "Телефон"
    assert body["report_id"] is not None
    assert body["subscription_id"] == sub.id
    assert db_session.get(models.OperatorNodeReport, body["report_id"]).user_id == user.id


def test_webapp_report_broken_device_foreign_is_no_subscription(
    client, db_session, ladder, monkeypatch
):
    """Чужое/несуществующее устройство — единый action, а не 404: фронт
    показывает тот же честный текст, что бот."""
    _owner, _sub, device, _nodes = ladder("wforeign")
    stranger = make_user(db_session, telegram_id="wforeign-stranger")
    monkeypatch.setattr(
        ProvisioningOrchestrator, "failover_device",
        lambda self, d: pytest.fail("чужое устройство не чинится"),
    )

    for device_id in (device.id, 999_999):
        r = client.post(
            "/api/webapp/report-broken-device",
            json={"device_id": device_id},
            headers=_auth(stranger.id),
        )
        assert r.status_code == 200, r.text
        assert r.json()["action"] == "no_subscription"
        assert r.json()["migrated"] is False
    db_session.refresh(device)
    assert device.status == models.DeviceStatus.active


def test_webapp_report_broken_is_whole_sub(client, db_session, monkeypatch):
    user, sub, _device, _node = _plain_sub(db_session, "wall")
    fresh = _fake_whole_sub_migrate(monkeypatch, db_session, sub, tag="wall")

    r = client.post("/api/webapp/report-broken", headers=_auth(user.id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "migrated"
    assert body["scope"] == "subscription"
    assert body["migrated"] is True
    assert body["new_node_name"] == fresh.name
    assert body["target_node_name"] == fresh.name
    assert body["subscription_id"] == sub.id
    assert body["report_id"] is not None
    assert _complaints(db_session, user.id) == 1

    # Повтор сразу — throttled по единой политике, и это не ошибка.
    again = client.post("/api/webapp/report-broken", headers=_auth(user.id)).json()
    assert again["action"] == "throttled"
    assert again["migrated"] is False
    assert again["retry_after_sec"] > 0


def test_webapp_feedback_endpoints_check_owner_and_keep_fail(client, db_session):
    user, sub, _device, node = _plain_sub(db_session, "wfb")
    stranger = make_user(db_session, telegram_id="wfb-stranger")
    report = _report(db_session, user, sub, node=node)

    # Чужой репорт — 404 на всех трёх.
    for path in ("report-still-broken", "report-ok"):
        r = client.post(
            f"/api/webapp/{path}", json={"report_id": report.id},
            headers=_auth(stranger.id),
        )
        assert r.status_code == 404, (path, r.text)
    r = client.post(
        "/api/webapp/report-operator",
        json={"report_id": report.id, "operator": "mts"},
        headers=_auth(stranger.id),
    )
    assert r.status_code == 404

    r = client.post(
        "/api/webapp/report-still-broken", json={"report_id": report.id},
        headers=_auth(user.id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "fail"
    db_session.refresh(report)
    assert report.outcome == "fail"
    assert report.resolved_at is not None

    # «Всё работает» после «не работает» явный негатив не перетирает.
    r = client.post(
        "/api/webapp/report-ok", json={"report_id": report.id}, headers=_auth(user.id)
    )
    assert r.json()["outcome"] == "fail"
    db_session.refresh(report)
    assert report.outcome == "fail"

    fresh = _report(db_session, user, sub, node=node)
    r = client.post(
        "/api/webapp/report-ok", json={"report_id": fresh.id}, headers=_auth(user.id)
    )
    assert r.json()["outcome"] == "ok"


def test_webapp_health_ping_report_single_device_goes_per_device(client, db_session, ladder):
    """Совместимость со старым бандлом: одно живое устройство → per-device
    лестница (reshuffle), а не whole-sub перенос мимо неё."""
    user, _sub, device, _nodes = ladder("whp")
    r = client.post("/api/webapp/health-ping-report", headers=_auth(user.id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "reshuffled"
    assert body["scope"] == "device"
    assert body["migrated"] is True
    assert body["device_name"]
    rep = db_session.get(models.OperatorNodeReport, body["report_id"])
    assert rep.device_id == device.id


# ── 4. Плановый пинг ─────────────────────────────────────────────────────


def test_health_ping_bad_repairs_and_returns_action(client, db_session, ladder):
    user, sub, device, _nodes = ladder("hping")
    r = client.post(
        "/api/users/health-ping-response",
        json={"telegram_id": user.telegram_id, "subscription_id": sub.id, "answer": "bad"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "reshuffled"
    assert body["scope"] == "device"
    assert body["report_id"] is not None
    assert db_session.get(models.OperatorNodeReport, body["report_id"]).device_id == device.id

    rows = (
        db_session.query(models.AuditLog)
        .filter_by(action="health_ping_response", target_id=sub.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].extra["answer"] == "bad"
    assert rows[0].extra["source"] == "prompted"
    assert _complaints(db_session, user.id) == 1


def test_health_ping_bad_without_subscription_only_acks(client, db_session, monkeypatch):
    user = make_user(db_session, telegram_id="hping-nosub")
    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node",
        lambda self, s, **k: pytest.fail("без подписки чинить нечего"),
    )
    r = client.post(
        "/api/users/health-ping-response",
        json={"telegram_id": user.telegram_id, "answer": "bad"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    rows = (
        db_session.query(models.AuditLog)
        .filter_by(action="health_ping_response", actor=str(user.id))
        .all()
    )
    assert len(rows) == 1

    # Чужая подписка в теле — как без подписки (anti-forge).
    _owner, sub, _device, _node = _plain_sub(db_session, "hping-owner")
    r = client.post(
        "/api/users/health-ping-response",
        json={"telegram_id": user.telegram_id, "subscription_id": sub.id, "answer": "bad"},
    )
    assert "action" not in r.json()


def test_health_ping_bad_whole_sub_when_several_devices(client, db_session, monkeypatch):
    user, sub, _device, node = _plain_sub(db_session, "hping-multi")
    make_device(db_session, sub, _cfg(db_session, node), access_username="hping-multi-2")
    fresh = _fake_whole_sub_migrate(monkeypatch, db_session, sub, tag="hping-multi")
    r = client.post(
        "/api/users/health-ping-response",
        json={"telegram_id": user.telegram_id, "subscription_id": sub.id, "answer": "bad"},
    )
    body = r.json()
    assert body["action"] == "migrated"
    assert body["scope"] == "subscription"
    assert body["new_node_name"] == fresh.name


# ── 5. Страница по токену (?fix=1) ───────────────────────────────────────


TOKEN = "unifytoken1234567890"


@pytest.fixture
def page(db_session, ladder, monkeypatch):
    monkeypatch.setenv("SUB_FIX_PAGE", "1")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-for-nonce")
    monkeypatch.setenv("BOT_USERNAME", "GV8_vpn_bot")
    return ladder("page", token=TOKEN)


def _post(client, extra: str = "", token: str = TOKEN):
    """POST на страницу с валидным nonce. Лимит 2/мин по токену сбрасываем:
    в одном тесте шагов больше."""
    from app.rate_limit import limiter

    limiter.reset()
    nonce = sub_fix.make_nonce(token)
    return client.post(f"/api/sub/{token}?fix=1&n={nonce}{extra}", headers=HTML)


def _latest_report(db, sub_id: int) -> models.OperatorNodeReport:
    db.expire_all()
    return (
        db.query(models.OperatorNodeReport)
        .filter_by(subscription_id=sub_id)
        .order_by(models.OperatorNodeReport.id.desc())
        .first()
    )


def test_page_success_offers_operator_forms(client, db_session, page):
    _user, sub, _device, _nodes = page
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]

    report = _latest_report(db_session, sub.id)
    assert report is not None
    for op in ("mts", "beeline", "megafon", "tele2", "home_wifi", "other"):
        assert f"report={report.id}&op={op}" in resp.text, op
    assert "Пропустить" in resp.text
    assert f"report={report.id}&op=skip" in resp.text
    assert resp.text.count("&op=") == 7
    assert "какой у вас интернет" in resp.text
    # На успехе обратная связь идёт после оператора — «Попробовать ещё
    # раз» здесь не нужна, а поддержка уже активна.
    assert "?start=support" in resp.text


def test_page_operator_then_feedback_forms(client, db_session, page):
    _user, sub, _device, _nodes = page
    _post(client)
    report = _latest_report(db_session, sub.id)
    assert report.operator is None

    resp = _post(client, f"&report={report.id}&op=mts")
    assert resp.status_code == 200
    db_session.refresh(report)
    assert report.operator == "mts"
    assert report.outcome == "pending"
    assert f"report={report.id}&ok=1" in resp.text
    assert f"report={report.id}&still=1" in resp.text
    assert "Всё работает" in resp.text
    assert "Всё равно не работает" in resp.text
    assert "Попробовать ещё раз" in resp.text
    assert "?start=support" in resp.text

    # «Пропустить» оператора не трогает и ведёт на тот же экран.
    resp = _post(client, f"&report={report.id}&op=skip")
    db_session.refresh(report)
    assert report.operator == "mts"
    assert f"report={report.id}&ok=1" in resp.text

    # Мусор вне таксономии → unknown.
    _post(client, f"&report={report.id}&op=<img>")
    db_session.refresh(report)
    assert report.operator == "unknown"


def test_page_still_broken_sets_fail_and_ok_does_not_override(client, db_session, page):
    user, sub, _device, _nodes = page
    _post(client)
    report = _latest_report(db_session, sub.id)

    resp = _post(client, f"&report={report.id}&still=1")
    assert resp.status_code == 200
    db_session.refresh(report)
    assert report.outcome == "fail"
    assert report.resolved_at is not None
    assert "Жаль, что не помогло" in resp.text
    assert "Попробовать ещё раз" in resp.text
    assert "?start=support" in resp.text

    resp = _post(client, f"&report={report.id}&ok=1")
    db_session.refresh(report)
    assert report.outcome == "fail", "явный негатив не перетираем"
    assert "рады, что заработало" in resp.text

    # Свежий репорт «ok» закрывает как ok.
    fresh = _report(db_session, user, sub)
    _post(client, f"&report={fresh.id}&ok=1")
    db_session.refresh(fresh)
    assert fresh.outcome == "ok"
    assert fresh.resolved_at is not None


def test_page_foreign_or_garbage_report_changes_nothing(client, db_session, page):
    _user, sub, _device, _nodes = page
    other_user, other_sub, _d, other_node = _plain_sub(db_session, "page-other")
    other = _report(db_session, other_user, other_sub, node=other_node)
    before = (
        db_session.query(models.AuditLog).count(),
        db_session.query(models.OperatorNodeReport).count(),
    )

    resp = _post(client, f"&report={other.id}&op=mts&still=1")
    assert resp.status_code == 200
    db_session.refresh(other)
    assert other.operator is None
    assert other.outcome == "pending"
    assert "Починить подключение" in resp.text, "стартовый экран, без «не найдено»"
    assert "не найден" not in resp.text.lower()

    resp = _post(client, "&report=abc&ok=1")
    assert "Починить подключение" in resp.text
    db_session.expire_all()
    assert (
        db_session.query(models.AuditLog).count(),
        db_session.query(models.OperatorNodeReport).count(),
    ) == before, "чужой/битый репорт ничего не пишет и не чинит"


def test_page_banned_owner_is_shown_suspended(client, db_session, page, monkeypatch):
    user, sub, _device, _nodes = page
    user.banned_at = utcnow()
    db_session.commit()
    monkeypatch.setattr(
        ProvisioningOrchestrator, "failover_device",
        lambda self, d: pytest.fail("забаненного чинить нельзя"),
    )

    resp = client.get(f"/api/sub/{TOKEN}?fix=1", headers=HTML)
    assert resp.status_code == 200
    assert "Доступ приостановлен" in resp.text
    assert "Починить подключение" not in resp.text
    assert "?start=support" in resp.text, "поддержка активна"

    resp = _post(client)
    assert resp.status_code == 200
    assert "Доступ приостановлен" in resp.text
    db_session.expire_all()
    assert _reports(db_session, sub.id) == 0
    assert _complaints(db_session, user.id) == 0


def test_page_throttled_screen_offers_retry_with_minutes(client, db_session, page, monkeypatch):
    user, sub, _device, nodes = page
    monkeypatch.setenv("SELF_REPAIR_THROTTLE_SEC", "300")
    _report(db_session, user, sub, ago_sec=10, node=nodes[0])

    resp = _post(client)
    assert resp.status_code == 200
    assert "Уже чиним" in resp.text
    assert "через 5 мин." in resp.text, "M = ceil(retry_after_sec/60)"
    assert "Попробовать ещё раз" in resp.text
    assert "?start=support" in resp.text
    assert _reports(db_session, sub.id) == 1, "нового шага не было"


def test_page_daily_limit_and_no_target_screens_offer_retry(
    client, db_session, page, monkeypatch
):
    user, sub, _device, nodes = page

    # Потолок: 5 починок за сутки (дефолт) — «нужен человек», но повтор
    # и поддержка на экране есть.
    for ago in (600, 1200, 1800, 2400, 3000):
        _report(db_session, user, sub, ago_sec=ago, node=nodes[0])
    resp = _post(client)
    assert "Слишком часто" in resp.text
    assert "Попробовать ещё раз" in resp.text
    assert "?start=support" in resp.text

    # Нет свободной ноды: лестницу ведём сразу на перенос и роняем его.
    db_session.query(models.OperatorNodeReport).filter_by(subscription_id=sub.id).delete()
    db_session.commit()
    monkeypatch.setattr(
        self_repair.rotation, "decide_step", lambda db, uid: self_repair.rotation.STEP_RELOCATE
    )

    def no_node(self, device):
        raise RuntimeError("no fresh node")

    monkeypatch.setattr(ProvisioningOrchestrator, "failover_device", no_node)
    resp = _post(client)
    assert "Сейчас не получилось" in resp.text
    assert "Попробовать ещё раз" in resp.text
    assert "?start=support" in resp.text
    assert _reports(db_session, sub.id) == 0


def test_page_not_ready_screen(client, db_session, page):
    _user, sub, device, _nodes = page
    device.status = models.DeviceStatus.pending
    db_session.commit()
    resp = _post(client)
    assert resp.status_code == 200
    assert "настраивается" in resp.text
    assert "Попробовать ещё раз" in resp.text
    assert _reports(db_session, sub.id) == 0


def test_page_uses_core_policy_not_its_own_env(monkeypatch):
    """Обёртки страницы читают ту же политику, что ядро."""
    monkeypatch.setenv("SUB_FIX_THROTTLE_SEC", "180")
    monkeypatch.setenv("SUB_FIX_DAILY_MAX", "9")
    assert sub_fix._throttle_sec() == 180
    assert sub_fix._daily_max() == 9
    monkeypatch.setenv("SELF_REPAIR_THROTTLE_SEC", "60")
    assert sub_fix._throttle_sec() == 60
    outcome = _types.SimpleNamespace(retry_after_sec=None)
    assert sub_fix._retry_minutes(outcome) == 1
    assert sub_fix._retry_minutes(_types.SimpleNamespace(retry_after_sec=61)) == 2


# ── 6. Фиксы по адверсарному ревью (2026-09-13) ──────────────────────────


def test_ladder_ignores_throttled_complaints(db_session):
    """Жалобы, по которым ничего не делали (throttled=true), ступень не
    двигают: два нетерпеливых тапа внутри окна иначе перепрыгивали смену
    ноды и сразу давали дубль. Старые строки без ключа — считаются."""
    from app.services import rotation

    user = make_user(db_session, telegram_id="ladder-thr")

    def row(extra):
        db_session.add(
            models.AuditLog(
                actor=str(user.id),
                actor_type=models.AuditActor.user,
                action="complaint_received",
                target_type="user",
                target_id=user.id,
                extra=extra,
            )
        )

    row({"throttled": False, "proto": None})
    row({"throttled": True, "proto": None})
    row({"throttled": True, "proto": None})
    row(None)  # legacy-строка без extra
    db_session.commit()

    assert rotation.complaints_in_window(db_session, user.id) == 2


def test_whole_sub_all_pending_is_not_ready(db_session, monkeypatch):
    """Все устройства ещё собираются → not_ready, а не whole-sub перенос
    поверх незавершённого провижна (тот же гейт, что у per-device пути)."""
    user, sub, device, _node = _plain_sub(db_session, "wsp")
    device.status = models.DeviceStatus.pending
    db_session.commit()

    def boom(self, *a, **k):
        raise AssertionError("миграция не должна вызываться")

    monkeypatch.setattr(ProvisioningOrchestrator, "migrate_subscription_to_free_node", boom)
    out = self_repair.handle_broken_subscription(
        db_session, sub, user=user, dedup_sec=60, source="test"
    )
    assert out.action == "not_ready"
    assert out.scope == "subscription"
    assert _complaints(db_session, user.id) == 0


def test_whole_sub_parallel_tap_is_blocked_by_advisory_lock(db_session, monkeypatch):
    """Второй параллельный whole-sub тап упирается в advisory-лок 4002 и
    получает throttled, а не вторую миграцию + второй бан ноды."""
    from sqlalchemy import text

    user, sub, _device, _node = _plain_sub(db_session, "wsl")

    def boom(self, *a, **k):
        raise AssertionError("миграция под чужим локом не должна вызываться")

    monkeypatch.setattr(ProvisioningOrchestrator, "migrate_subscription_to_free_node", boom)

    bind = db_session.get_bind()
    engine = getattr(bind, "engine", bind)
    other = engine.connect()
    try:
        got = other.execute(
            text("SELECT pg_try_advisory_lock(4002, :s)"), {"s": sub.id}
        ).scalar()
        assert got is True
        out = self_repair.handle_broken_subscription(
            db_session, sub, user=user, dedup_sec=60, source="test"
        )
    finally:
        other.execute(text("SELECT pg_advisory_unlock(4002, :s)"), {"s": sub.id})
        other.close()
    assert out.action == "throttled"
    assert out.scope == "subscription"
    assert out.retry_after_sec == 30


def test_daily_cap_scales_with_live_devices(db_session, monkeypatch):
    """Потолок «на устройство» считается по подписке → умножаем на число
    живых устройств: двум устройствам с daily_max=2 положено 4 шага."""
    user, sub, device, node = _plain_sub(db_session, "dcap")
    cfg = _cfg(db_session, node)
    make_device(db_session, sub, cfg, access_username="dcap-second")
    monkeypatch.setenv("SELF_REPAIR_DAILY_MAX", "2")
    for i in range(3):
        _report(db_session, user, sub, ago_sec=600 + i, node=node)
    monkeypatch.setenv("SELF_REPAIR_THROTTLE_SEC", "0")
    assert self_repair.repair_wait(db_session, sub) == (None, None)
    _report(db_session, user, sub, ago_sec=590, node=node)
    wait, reason = self_repair.repair_wait(db_session, sub)
    assert reason == "daily_limit"
    assert wait and wait > 0


def test_page_ban_gate_covers_pay_and_feedback_and_precedes_expired(
    client, db_session, page, monkeypatch
):
    """Бан закрывает ВСЕ действия страницы (оплату и обратную связь тоже) и
    стоит раньше экрана expired — иначе забаненный с истёкшей подпиской
    получал кнопку продления."""
    user, sub, _device, _nodes = page
    monkeypatch.setenv("SUB_FIX_PAY", "1")
    user.banned_at = utcnow()
    db_session.commit()

    before = db_session.query(models.Invoice).count()
    resp = _post(client, "&pay=1")
    assert resp.status_code == 200
    assert "Доступ приостановлен" in resp.text
    assert db_session.query(models.Invoice).count() == before

    resp = _post(client, "&report=1&ok=1")
    assert "Доступ приостановлен" in resp.text

    sub.expires_at = utcnow() - timedelta(days=1)
    db_session.commit()
    resp = client.get(f"/api/sub/{TOKEN}?fix=1", headers=HTML)
    assert "Доступ приостановлен" in resp.text
    assert "Продлить" not in resp.text


def test_page_feedback_accepts_older_nonce_and_rerenders_on_stale(
    client, db_session, page
):
    """Человек ушёл в клиент и вернулся через полчаса-час: nonce формы
    обратной связи принимаем шире; совсем протухший — перерисовываем тот же
    экран со свежим nonce, ничего не записывая."""
    from app.rate_limit import limiter

    _user, sub, _device, _nodes = page
    _post(client)
    report = _latest_report(db_session, sub.id)
    assert report is not None

    limiter.reset()
    old = sub_fix.make_nonce(TOKEN, offset=3)  # 45–60 мин назад
    resp = client.post(
        f"/api/sub/{TOKEN}?fix=1&n={old}&report={report.id}&ok=1", headers=HTML
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.OperatorNodeReport, report.id).outcome == "ok"

    limiter.reset()
    stale = sub_fix.make_nonce(TOKEN, offset=8)
    resp = client.post(
        f"/api/sub/{TOKEN}?fix=1&n={stale}&report={report.id}&still=1", headers=HTML
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.OperatorNodeReport, report.id).outcome == "ok"
    assert f"report={report.id}&ok=1" in resp.text
    assert f"report={report.id}&still=1" in resp.text
    assert "Что-то не работает?" not in resp.text


def test_page_rate_limited_screen_has_exits(monkeypatch):
    """Экран 429 — не тупик: ссылка обратно (GET вне лимита) и поддержка."""
    monkeypatch.setenv("BOT_USERNAME", "GV8_vpn_bot")
    resp = sub_fix.render_rate_limited()
    assert resp.status_code == 429
    body = resp.body.decode()
    assert 'href="?fix=1"' in body
    assert "?start=support" in body


def test_page_repair_after_expiry_shows_renewal(client, db_session, page):
    """Подписка истекла между GET и POST → экран продления, а не «проверьте
    клиент»."""
    _user, sub, _device, _nodes = page
    sub.expires_at = utcnow() - timedelta(hours=1)
    db_session.commit()
    resp = _post(client)
    assert resp.status_code == 200
    assert "Подписка закончилась" in resp.text


def test_page_migrated_text_hides_node_name():
    out = self_repair.RepairOutcome(
        action="migrated", report_id=None, new_node_name="ufo-ru-01"
    )
    sub = _types.SimpleNamespace(expires_at=None, plan=None, extra_device_slots=0)
    body = sub_fix.render_outcome(out, sub, TOKEN).body.decode()
    assert "ufo-ru-01" not in body
    assert "Перевели вас на другой сервер" in body


def test_webapp_health_ping_row_has_node_id_and_survives_no_target(
    client, db_session, ladder, monkeypatch
):
    """Строка опроса коммитится ДО починки (откат no_target её не уносит) и
    несёт node_id устройства — на нём стоит админ-дашборд /health-pings."""
    from app.services import rotation

    user, sub, device, nodes = ladder("hpnode")
    monkeypatch.setattr(rotation, "reshuffle_legs", lambda db, dev: None)

    def boom(self, dev):
        raise RuntimeError("no free node")

    monkeypatch.setattr(ProvisioningOrchestrator, "failover_device", boom)
    resp = client.post(
        "/api/webapp/report-broken-device",
        json={"device_id": device.id},
        headers=_auth(user.id),
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "no_target"
    assert resp.json()["node_id"] == nodes[0].id

    db_session.expire_all()
    rows = (
        db_session.query(models.AuditLog)
        .filter_by(action="health_ping_response", target_id=sub.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].extra["node_id"] == nodes[0].id
    assert rows[0].extra["scope"] == "device"


# ── Алерты админу (2026-09-30): пуша на саму жалобу нет, только «не помогло» ──


def _alerts(db, action: str) -> list[models.AuditLog]:
    db.expire_all()
    return db.query(models.AuditLog).filter_by(action=action).all()


def test_health_ping_bad_sends_no_admin_push(client, db_session, ladder, monkeypatch):
    """Жалоба на пинг — только в БД: лестница чинит сама (user 1000078, 30.09:
    пуш «жалуется» пришёл, хотя перетасовка через 14 мин всё починила)."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    user, sub, _device, _nodes = ladder("hp-nopush")
    r = client.post(
        "/api/users/health-ping-response",
        json={"telegram_id": user.telegram_id, "subscription_id": sub.id, "answer": "bad"},
    )
    assert r.status_code == 200 and r.json()["action"] == "reshuffled"
    assert _alerts(db_session, "admin_alert_user_report") == []
    assert _alerts(db_session, "admin_alert_repair_failed") == []
    assert _complaints(db_session, user.id) == 1


def _bad_ping(client, user, sub):
    r = client.post(
        "/api/users/health-ping-response",
        json={"telegram_id": user.telegram_id, "subscription_id": sub.id, "answer": "bad"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_inconclusive_alerts_only_after_delayed_recheck(
    client, db_session, ladder, monkeypatch
):
    """Watcher решает на 10-15-й минуте — пушить по первому inconclusive рано
    (у 1000078 переподключение увидели на 14-й). Пуш только после
    перепроверки через REPAIR_ALERT_DELAY_MIN, и один на репорт."""
    from app.services import operator_reports, repair_alerts

    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    user, sub, _device, _nodes = ladder("hp-inconcl")
    report = db_session.get(models.OperatorNodeReport, _bad_ping(client, user, sub)["report_id"])
    report.reported_at = utcnow() - timedelta(minutes=20)
    db_session.commit()

    assert operator_reports.resolve_pending_reports(db_session)["resolved_inconclusive"] == 1
    assert _alerts(db_session, "admin_alert_repair_failed") == []  # на 20-й минуте — рано
    assert repair_alerts.alert_stale_inconclusive(db_session)["alerted"] == 0

    report = db_session.get(models.OperatorNodeReport, report.id)
    report.reported_at = utcnow() - timedelta(minutes=90)
    db_session.commit()
    assert repair_alerts.alert_stale_inconclusive(db_session)["alerted"] == 1
    rows = _alerts(db_session, "admin_alert_repair_failed")
    assert len(rows) == 1
    assert rows[0].extra["report_id"] == report.id
    assert rows[0].extra["reason"] == "inconclusive"
    text = rows[0].extra["text"]
    assert "Починка не помогла" in text and "перетасовали протоколы" in text
    assert f"tg={user.telegram_id}" in text

    # Повторный проход и «всё ещё не работает» по тому же репорту — без второго пуша.
    assert repair_alerts.alert_stale_inconclusive(db_session)["alerted"] == 0
    r2 = client.post(
        "/api/admin/client-control/report-still-broken", json={"report_id": report.id}
    )
    assert r2.status_code == 200
    assert len(_alerts(db_session, "admin_alert_repair_failed")) == 1


def test_late_reconnect_suppresses_inconclusive_alert(
    client, db_session, ladder, monkeypatch
):
    from app.services import operator_reports, repair_alerts

    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    user, sub, _device, _nodes = ladder("hp-late")
    report = db_session.get(models.OperatorNodeReport, _bad_ping(client, user, sub)["report_id"])
    report.outcome = "inconclusive"
    report.reported_at = utcnow() - timedelta(minutes=90)
    db_session.commit()
    monkeypatch.setattr(operator_reports, "report_reconnected", lambda db, r: True)
    res = repair_alerts.alert_stale_inconclusive(db_session)
    assert res["alerted"] == 0 and res["reconnected_late"] == 1
    assert _alerts(db_session, "admin_alert_repair_failed") == []


def test_whole_sub_reconnect_counts_any_device(db_session, ladder):
    """Перенос всей подписки: репорт хранит первое устройство, а подключиться
    мог второй — это успех, не «не помогло»."""
    from app.services import repair_alerts

    user, sub, device, nodes = ladder("hp-wholesub")
    node = nodes[0]
    other = make_device(db_session, sub, _cfg(db_session, node), access_username="ws-other")
    db_session.add(models.Credential(
        node_id=node.id, device_id=other.id, subscription_id=sub.id,
        access_username="ws-other", is_active=True, proto="vless-reality",
        config_text="enc", pool_state=models.CredentialPoolState.assigned,
    ))
    report = models.OperatorNodeReport(
        user_id=user.id, subscription_id=sub.id, device_id=device.id,
        target_node_id=node.id, target_access_username="nobody-here",
        outcome="inconclusive", reported_at=utcnow() - timedelta(minutes=90),
    )
    db_session.add(report)
    db_session.add(models.NodeTrafficSample(
        node_id=node.id, observed_at=utcnow(),
        details={"vless-reality": {"users": ["ws-other"]}},
    ))
    db_session.commit()
    assert repair_alerts._subscription_reconnected(db_session, report) is True


def test_reports_without_self_repair_source_do_not_page(db_session, ladder, monkeypatch):
    """Кнопка «симулировать сигнал» в админке и автоотчёты клиента source не пишут."""
    from app.services import repair_alerts

    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    user, sub, device, _nodes = ladder("hp-admin-sim")
    report = models.OperatorNodeReport(
        user_id=user.id, subscription_id=sub.id, device_id=device.id,
        outcome="inconclusive", reported_at=utcnow() - timedelta(minutes=90),
    )
    db_session.add(report)
    db_session.commit()
    db_session.add(models.AuditLog(
        actor="admin_panel", action="client_reported_failure", target_type="subscription",
        target_id=sub.id, extra={"report_id": report.id, "kind": "user_reported", "scope": "subscription"},
    ))
    db_session.commit()
    assert repair_alerts.alert_stale_inconclusive(db_session)["alerted"] == 0
    assert _alerts(db_session, "admin_alert_repair_failed") == []


def test_watcher_ok_sends_nothing(client, db_session, ladder, monkeypatch):
    from app.services import operator_reports

    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    monkeypatch.setattr(operator_reports, "_username_in_details", lambda details, u: True)
    user, sub, _device, _nodes = ladder("hp-ok")
    r = client.post(
        "/api/users/health-ping-response",
        json={"telegram_id": user.telegram_id, "subscription_id": sub.id, "answer": "bad"},
    )
    report = db_session.get(models.OperatorNodeReport, r.json()["report_id"])
    report.reported_at = utcnow() - timedelta(minutes=30)
    db_session.commit()
    targets = operator_reports._reconnect_targets(db_session, report)
    for node_id in targets:
        db_session.add(models.NodeTrafficSample(node_id=node_id, observed_at=utcnow(), details={}))
    db_session.commit()

    res = operator_reports.resolve_pending_reports(db_session)
    assert res["resolved_ok"] == 1
    assert _alerts(db_session, "admin_alert_repair_failed") == []


def test_still_broken_sends_repair_failed_alert(client, db_session, ladder, monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    user, sub, _device, _nodes = ladder("hp-still")
    r = client.post(
        "/api/users/health-ping-response",
        json={"telegram_id": user.telegram_id, "subscription_id": sub.id, "answer": "bad"},
    )
    rid = r.json()["report_id"]
    r2 = client.post("/api/admin/client-control/report-still-broken", json={"report_id": rid})
    assert r2.status_code == 200
    rows = _alerts(db_session, "admin_alert_repair_failed")
    assert len(rows) == 1 and rows[0].extra["reason"] == "fail"


def test_no_target_sends_repair_failed_alert(client, db_session, ladder, monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    user, sub, _device, _nodes = ladder("hp-notarget")
    monkeypatch.setattr(
        self_repair.rotation, "decide_step", lambda db, uid: self_repair.rotation.STEP_RELOCATE
    )

    def no_node(self, device):
        raise RuntimeError("no fresh node")

    monkeypatch.setattr(ProvisioningOrchestrator, "failover_device", no_node)
    r = client.post(
        "/api/users/health-ping-response",
        json={"telegram_id": user.telegram_id, "subscription_id": sub.id, "answer": "bad"},
    )
    assert r.json()["action"] == "no_target"
    rows = _alerts(db_session, "admin_alert_repair_failed")
    assert len(rows) == 1
    assert rows[0].extra["reason"] == "no_target"
