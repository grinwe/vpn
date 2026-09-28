"""Бот показывает ссылку УСТРОЙСТВА, а не всей подписки.

Разбор user 1000076 (27.09.2026): бот прислал legacy-токен подписки, юзер
добавил device-2/device-3 в кабинете — и та же ссылка из бота стала отдавать
креды всех трёх устройств (12 конфигов в одном Hiddify), пока ссылки device-2/3
из кабинета ушли родным → одни логины на двух клиентах.

Схема: ``Subscription.link_token`` ставится при создании подписки (токен
первого устройства), бот показывает его. Храним токен, а не строку: failover и
миграции переносят токен на НОВУЮ строку Device (ревью 28.09: правило «самое
раннее устройство» после failover выбирало device-2 родственника). NULL —
подписки до 0070, им остаётся legacy-ссылка.

Закрепляем:

* ``link_token_for``: NULL → legacy; живой держатель → токен; после failover
  (токен на новой строке, старая без токена) → тот же токен, не device-2;
  держатель удалён юзером → самое раннее активное устройство; активных нет →
  legacy.
* provision_subscription (cold и warm) ставит link_token = токен первого
  устройства — cold ещё pending, но /config и пуш уже совпадают.
* regenerate_subscription_sublink переносит link_token на замену.
* Пуш config_ready и ``/api/users/by_telegram`` отдают link_token,
  ``sub_token`` в API сохранил смысл.
* add_device (бот/ЛК) возвращает токен НОВОГО устройства, бот шлёт его.
"""
from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import provisioning_throttle, sub_links, warm_pool
from app.services.config_ready import notify_config_ready
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

BASE = "https://grn-ssync.pro"
_ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_API_TOKEN", "")}
T0 = utcnow()


@pytest.fixture(autouse=True)
def _no_sub_link_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SUB_LINK_BASE_URL", "SUB_LINK_BASE_URL_ALT", "SUB_LINK_ALT_SHARE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _reset_cold_throttle():
    provisioning_throttle.reset_for_tests()
    yield
    provisioning_throttle.reset_for_tests()


# ── link_token_for (чистая логика) ──


def _dev(id_, token, status="active", minutes=0, name=None, swap_from=None):
    return SimpleNamespace(
        id=id_, sub_token=token, status=SimpleNamespace(value=status),
        created_at=T0 + timedelta(minutes=minutes), name=name or f"device-{id_}",
        pending_swap_from=swap_from,
    )


def _sub(link_token, devices):
    return SimpleNamespace(sub_token="SUBTOK", link_token=link_token, devices=devices)


def test_legacy_sub_without_link_token_keeps_subscription_token() -> None:
    assert sub_links.link_token_for(_sub(None, [_dev(1, "d1")])) == "SUBTOK"


def test_live_holder_gives_its_token() -> None:
    devices = [_dev(1, "d1"), _dev(2, "d2", minutes=5)]
    assert sub_links.link_token_for(_sub("d1", devices)) == "d1"


def test_failover_keeps_link_on_moved_token_not_sibling() -> None:
    """failover_device(primary): старая строка теряет токен (_release_sub_token),
    новая — позже device-2 по created_at — несёт тот же токен. Бот обязан
    показывать ту же ссылку, а не device-2 родственника."""
    devices = [
        _dev(1, None, status="disabled"),        # старый primary, токен освобождён
        _dev(2, "d2", minutes=5),                # device-2 у родственника
        _dev(3, "d1", minutes=30),               # замена primary с тем же токеном
    ]
    assert sub_links.link_token_for(_sub("d1", devices)) == "d1"


def test_holder_row_not_loaded_trusts_stored_token() -> None:
    assert sub_links.link_token_for(_sub("d1", [])) == "d1"


def test_holder_removed_by_user_falls_to_earliest_active() -> None:
    """Юзер удалил primary (revoke оставляет токен на disabled-строке): иначе
    саб-эндпоинт алиасил бы ссылку на «последнее обновлённое» соседнее."""
    devices = [
        _dev(1, "d1", status="disabled"),
        _dev(3, "d3", minutes=9),
        _dev(2, "d2", minutes=5),
        _dev(4, "d4", status="pending", minutes=1),
    ]
    assert sub_links.link_token_for(_sub("d1", devices)) == "d2"


def test_retired_holder_prefers_same_named_replacement_even_pending() -> None:
    """Разморозка до переноса (или ручная починка): primary выведен, его
    одноимённая замена ещё pending — показываем её, а не ссылку всей подписки
    и не device-2 родственника."""
    devices = [
        _dev(1, "d1", status="disabled", name="primary"),
        _dev(2, "d2", minutes=5, name="device-2"),
        _dev(3, "d1new", status="pending", minutes=40, name="primary"),
    ]
    assert sub_links.link_token_for(_sub("d1", devices)) == "d1new"


def test_holder_removed_only_pending_left_gives_pending_not_legacy() -> None:
    devices = [_dev(1, "d1", status="revoked"), _dev(2, "d2", status="pending")]
    assert sub_links.link_token_for(_sub("d1", devices)) == "d2"


def test_fallback_skips_failover_replacement_with_temporary_token() -> None:
    """Замена в незавершённом свапе несёт ВРЕМЕННЫЙ токен — при свапе он
    исчезнет, и ссылка стала бы 404."""
    devices = [
        _dev(1, "d1", status="disabled", name="primary"),
        _dev(2, "tmp", status="pending", minutes=3, name="primary", swap_from=7),
        _dev(3, "d3", minutes=5, name="device-2"),
    ]
    assert sub_links.link_token_for(_sub("d1", devices)) == "d3"


def test_holder_removed_and_nothing_alive_falls_back_to_legacy() -> None:
    devices = [_dev(1, "d1", status="revoked"), _dev(2, "d2", status="failed")]
    assert sub_links.link_token_for(_sub("d1", devices)) == "SUBTOK"


# ── интеграция ──


def _fresh_node(db: Session, *, name: str, host: str) -> models.VPNNode:
    node = make_node(db, name=name, host=host)
    make_config(db, node, name=f"{name}-vless")
    db.refresh(node)
    return node


def test_cold_provision_sets_link_token_to_pending_first_device(db_session: Session) -> None:
    node = _fresh_node(db_session, name="dl-cold", host="10.9.1.1")
    plan = make_plan(db_session, name="dl-cold-plan")
    user = make_user(db_session, telegram_id="555001")

    sub, _task = ProvisioningOrchestrator(db_session).provision_subscription(
        user, plan, node_id=node.id
    )
    db_session.commit()
    db_session.refresh(sub)

    first = sub.devices[0]
    assert first.status == models.DeviceStatus.pending
    assert sub.link_token and sub.link_token == first.sub_token
    assert sub.link_token != sub.sub_token
    # Пока pending, /config уже отдаёт ту же ссылку, что придёт пушем.
    assert sub_links.link_token_for(sub) == first.sub_token


def test_warm_provision_sets_link_token(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        warm_pool, "run_playbook",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(warm_pool, "build_inventory_for_node", lambda node: None)
    monkeypatch.setenv("SUB_LINK_BASE_URL", BASE)
    node = _fresh_node(db_session, name="dl-warm", host="10.9.1.2")
    assert warm_pool.warm_one_bundle(db_session, node) is not None
    plan = make_plan(db_session, name="dl-warm-plan")
    user = make_user(db_session, telegram_id="555002")

    sub, task = ProvisioningOrchestrator(db_session).provision_subscription(
        user, plan, node_id=node.id
    )
    db_session.commit()
    db_session.refresh(sub)

    assert task.action == "assign_warm"
    first = sub.devices[0]
    assert sub.link_token == first.sub_token
    # Пуш warm-пути несёт ссылку устройства, а не подписки.
    row = (
        db_session.query(models.AuditLog)
        .filter_by(action="config_ready", target_type="subscription", target_id=sub.id)
        .one()
    )
    assert row.extra["sub_uri"] == f"{BASE}/{first.sub_token}"


def test_regenerate_moves_link_token_to_replacement(db_session: Session) -> None:
    node = _fresh_node(db_session, name="dl-regen", host="10.9.1.3")
    plan = make_plan(db_session, name="dl-regen-plan")
    user = make_user(db_session, telegram_id="555003")
    orch = ProvisioningOrchestrator(db_session)
    sub, _task = orch.provision_subscription(user, plan, node_id=node.id)
    db_session.commit()
    db_session.refresh(sub)
    old_token = sub.link_token

    results = orch.regenerate_subscription_sublink(sub)
    db_session.refresh(sub)

    new_device = results[0][0]
    assert sub.link_token == new_device.sub_token
    assert sub.link_token != old_token


def test_reprovision_after_retire_adopts_link_token(db_session: Session) -> None:
    """Разморозка/enable/продление/reality-dest refresh: устройство выведено
    (токен остаётся на disabled-строке), замена тем же именем со СВЕЖИМ
    токеном перенимает link_token — бот не откатывается на ссылку всей
    подписки и не уходит на соседнее устройство."""
    node = _fresh_node(db_session, name="dl-unfreeze", host="10.9.1.5")
    plan = make_plan(db_session, name="dl-unfreeze-plan")
    user = make_user(db_session, telegram_id="555005")
    orch = ProvisioningOrchestrator(db_session)
    sub, _task = orch.provision_subscription(user, plan, node_id=node.id)
    db_session.commit()
    db_session.refresh(sub)
    first = sub.devices[0]
    old_token = sub.link_token
    second, _t = orch.reprovision_subscription(sub, device_name="device-2")
    db_session.commit()

    orch.revoke_device(first, reason="freeze", background=True)
    db_session.commit()
    replacement, _t = orch.reprovision_subscription(sub, device_name=first.name)
    db_session.commit()
    db_session.refresh(sub)

    assert replacement.sub_token != old_token
    assert sub.link_token == replacement.sub_token
    assert sub_links.link_token_for(sub) == replacement.sub_token
    assert sub_links.link_token_for(sub) != second.sub_token


def test_restore_adopts_even_if_holder_was_renamed(db_session: Session) -> None:
    """Разморозка/enable/продление зовут reprovision без имени → «primary».
    Юзер мог переименовать своё устройство — перенос всё равно нужен."""
    node = _fresh_node(db_session, name="dl-renamed", host="10.9.1.7")
    plan = make_plan(db_session, name="dl-renamed-plan")
    user = make_user(db_session, telegram_id="555007")
    orch = ProvisioningOrchestrator(db_session)
    sub, _task = orch.provision_subscription(user, plan, node_id=node.id)
    db_session.commit()
    db_session.refresh(sub)
    first = sub.devices[0]
    first.name = "iPhone"
    db_session.commit()

    orch.revoke_device(first, reason="freeze", background=True)
    db_session.commit()
    replacement, _t = orch.reprovision_subscription(sub)  # как unfreeze_subscription
    db_session.commit()
    db_session.refresh(sub)

    assert replacement.name == "primary"
    assert sub.link_token == replacement.sub_token


def test_failover_style_reprovision_never_adopts(db_session: Session) -> None:
    """failover_device зовёт reprovision с reuse_uuid и ВРЕМЕННЫМ токеном
    (своп в конце) — переносить link_token на него нельзя: после свапа токен
    исчезнет, и ссылка бота стала бы 404."""
    import uuid

    node = _fresh_node(db_session, name="dl-fo", host="10.9.1.8")
    plan = make_plan(db_session, name="dl-fo-plan")
    user = make_user(db_session, telegram_id="555008")
    orch = ProvisioningOrchestrator(db_session)
    sub, _task = orch.provision_subscription(user, plan, node_id=node.id)
    db_session.commit()
    db_session.refresh(sub)
    first = sub.devices[0]
    old_token = sub.link_token
    orch.revoke_device(first, reason="test", background=True)
    db_session.commit()

    orch.reprovision_subscription(
        sub, device_name=first.name, reuse_uuid=str(uuid.uuid4()),
    )
    db_session.commit()
    db_session.refresh(sub)
    assert sub.link_token == old_token


def test_reprovision_other_name_does_not_steal_link_token(db_session: Session) -> None:
    node = _fresh_node(db_session, name="dl-steal", host="10.9.1.6")
    plan = make_plan(db_session, name="dl-steal-plan")
    user = make_user(db_session, telegram_id="555006")
    orch = ProvisioningOrchestrator(db_session)
    sub, _task = orch.provision_subscription(user, plan, node_id=node.id)
    db_session.commit()
    db_session.refresh(sub)
    old_token = sub.link_token

    # Держатель жив — добавление устройства link_token не трогает.
    orch.reprovision_subscription(sub, device_name="device-2")
    db_session.commit()
    db_session.refresh(sub)
    assert sub.link_token == old_token

    # Держатель удалён юзером, новое устройство с ДРУГИМ именем — не перенимает.
    orch.revoke_device(sub.devices[0] if sub.devices[0].sub_token == old_token
                       else sub.devices[1], reason="user remove", background=True)
    db_session.commit()
    orch.reprovision_subscription(sub, device_name="device-3")
    db_session.commit()
    db_session.refresh(sub)
    assert sub.link_token == old_token


def test_user_removing_holder_pins_replacement_link(client, db_session: Session) -> None:
    """Юзер удалил устройство со ссылкой бота: выбор замены фиксируется в
    link_token сразу, а не пересчитывается на каждый /config (иначе после
    failover выбранного ссылка переползала бы на другое устройство)."""
    node = _fresh_node(db_session, name="dl-rm", host="10.9.1.9")
    plan = make_plan(db_session, name="dl-rm-plan")
    user = make_user(db_session, telegram_id="555009")
    orch = ProvisioningOrchestrator(db_session)
    sub, _task = orch.provision_subscription(user, plan, node_id=node.id)
    db_session.commit()
    db_session.refresh(sub)
    holder = sub.devices[0]
    second, _t = orch.reprovision_subscription(sub, device_name="device-2")
    second.status = models.DeviceStatus.active
    db_session.commit()

    resp = client.post(
        f"/api/bot/devices/{holder.id}/remove",
        json={"telegram_id": user.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    sub = db_session.get(models.Subscription, sub.id)
    assert sub.link_token == second.sub_token


def _sub_fixture(db: Session, *, link_token: str | None):
    plan = make_plan(db, name="dl-plan")
    user = make_user(db, telegram_id="555004")
    node = make_node(db, name="dl-node", host="10.9.1.4")
    cfg = make_config(db, node)
    sub = make_subscription(db, user, plan, node)
    sub.sub_token = "sub-tok-dl"
    sub.link_token = link_token
    device = make_device(db, sub, cfg, access_username=f"user-{user.id}-{sub.id}")
    device.sub_token = "dev-tok-dl"
    db.add(
        models.Credential(
            node_id=node.id, device_id=device.id, subscription_id=sub.id,
            access_username=device.access_username, is_active=True,
            proto="vless-reality", config_text="enc",
            pool_state=models.CredentialPoolState.assigned,
        )
    )
    db.commit()
    return user, sub, device


def _push_uri(db: Session, sub_id: int) -> str | None:
    db.expire_all()
    row = (
        db.query(models.AuditLog)
        .filter_by(action="config_ready", target_id=sub_id)
        .one()
    )
    return row.extra.get("sub_uri")


@pytest.mark.parametrize("stored,expected", [("dev-tok-dl", "dev-tok-dl"), (None, "sub-tok-dl")])
def test_config_ready_push_follows_link_token(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, stored, expected
) -> None:
    monkeypatch.setenv("SUB_LINK_BASE_URL", BASE)
    _user, sub, device = _sub_fixture(db_session, link_token=stored)
    assert notify_config_ready(db_session, device, source="warm", commit=True)
    assert _push_uri(db_session, sub.id) == f"{BASE}/{expected}"


@pytest.mark.parametrize("stored,expected", [("dev-tok-dl", "dev-tok-dl"), (None, "sub-tok-dl")])
def test_users_api_exposes_link_token(client, db_session: Session, stored, expected) -> None:
    user, sub, _device = _sub_fixture(db_session, link_token=stored)
    resp = client.get(f"/api/users/by_telegram/{user.telegram_id}")
    assert resp.status_code == 200, resp.text
    item = next(s for s in resp.json() if s["id"] == sub.id)
    assert item["sub_token"] == "sub-tok-dl"  # старое поле не меняет смысла
    assert item["link_token"] == expected


def test_bot_add_device_returns_new_device_token(client, db_session: Session) -> None:
    user, sub, _device = _sub_fixture(db_session, link_token="dev-tok-dl")
    resp = client.post(
        f"/api/bot/subscriptions/{sub.id}/add_device",
        json={"telegram_id": user.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    db_session.expire_all()
    new_device = db_session.get(models.Device, data["device_id"])
    assert data["sub_token"] == new_device.sub_token
    assert data["sub_token"] not in ("dev-tok-dl", "sub-tok-dl")


# ── бот: после добавления устройства шлёт ЕГО ссылку ──


@pytest.fixture()
def bot_handlers(monkeypatch):
    from tests._bot_stubs import bot_sources_available, install_bot_stubs

    if not bot_sources_available():
        pytest.skip("bot/handlers.py недоступен в этом образе")
    mod = install_bot_stubs(monkeypatch, sub_link_base_url="https://sub.test/s")

    async def _no_devices(*_a, **_k):
        return None

    monkeypatch.setattr(mod, "_send_devices", _no_devices)
    return mod


def _script(mod, monkeypatch, resp):
    async def _fake(method, url, **kw):
        assert url.endswith("/add_device"), url
        return resp

    monkeypatch.setattr(mod, "_fetch_json", _fake)


async def _tap_add(mod):
    from tests._bot_stubs import FakeBot, FakeCallback

    bot = FakeBot()
    cb = FakeCallback(bot, None)
    cb.data = "devadd:7"
    await mod.device_add_cb(cb)
    return [t for _c, t, _k in bot.sent]


@pytest.mark.asyncio
async def test_bot_add_device_sends_new_device_link(bot_handlers, monkeypatch) -> None:
    _script(
        bot_handlers, monkeypatch,
        (200, {"device_count": 2, "charged_kopecks": 0, "sub_token": "newtok"}),
    )
    texts = await _tap_add(bot_handlers)
    assert any("https://sub.test/s/newtok" in t for t in texts), texts
    assert not any("/config" in t for t in texts), texts


@pytest.mark.asyncio
async def test_bot_add_device_without_token_points_to_cabinet(bot_handlers, monkeypatch) -> None:
    _script(bot_handlers, monkeypatch, (200, {"device_count": 2, "charged_kopecks": 0}))
    texts = await _tap_add(bot_handlers)
    assert any("личном кабинете" in t for t in texts), texts
    assert not any("/config" in t for t in texts), texts
