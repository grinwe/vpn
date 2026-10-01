"""Бот-обёртки устройств (/api/bot/...): паритет с ЛК, аудит 2026-08-21.

Тонкие обёртки над webapp-хэндлерами: ownership и биллинг слотов живут
в одном месте (api_webapp), бот получает юзера по telegram_id.
"""
from __future__ import annotations

import os

import pytest

from app import models
from app.services import provisioning_throttle

from .factories import make_config, make_device, make_node, make_plan, make_subscription, make_user

_ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_API_TOKEN", "")}


@pytest.fixture(autouse=True)
def _reset_cold_throttle():
    provisioning_throttle.reset_for_tests()
    yield
    provisioning_throttle.reset_for_tests()


def _setup(db):
    plan = make_plan(db)  # max_devices=3
    node = make_node(db)
    cfg = make_config(db, node)
    owner = make_user(db, telegram_id="dev-owner")
    stranger = make_user(db, telegram_id="dev-stranger")
    sub = make_subscription(db, owner, plan, node)
    dev = make_device(db, sub, cfg, access_username="dev-1")
    return plan, node, cfg, owner, stranger, sub, dev


def test_bot_add_device_free_within_capacity(client, db_session):
    _plan, _node, _cfg, owner, _s, sub, _dev = _setup(db_session)
    resp = client.post(
        f"/api/bot/subscriptions/{sub.id}/add_device",
        json={"telegram_id": owner.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["device_count"] == 2
    assert data["charged_kopecks"] == 0


def test_bot_rename_device_ownership(client, db_session):
    _p, _n, _c, owner, stranger, _sub, dev = _setup(db_session)
    url = f"/api/bot/devices/{dev.id}/rename"
    resp = client.post(
        url,
        json={"telegram_id": stranger.telegram_id, "name": "hack"},
        headers=_ADMIN,
    )
    assert resp.status_code == 404, resp.text

    resp = client.post(
        url,
        json={"telegram_id": owner.telegram_id, "name": "мой телефон"},
        headers=_ADMIN,
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(models.Device, dev.id).name == "мой телефон"


def test_bot_remove_device_guards_last(client, db_session):
    _p, _n, cfg, owner, _s, sub, dev = _setup(db_session)
    # Последнее устройство удалить нельзя.
    resp = client.post(
        f"/api/bot/devices/{dev.id}/remove",
        json={"telegram_id": owner.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 400, resp.text

    second = make_device(db_session, sub, cfg, access_username="dev-2")
    resp = client.post(
        f"/api/bot/devices/{second.id}/remove",
        json={"telegram_id": owner.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert (
        db_session.get(models.Device, second.id).status
        == models.DeviceStatus.disabled
    )
