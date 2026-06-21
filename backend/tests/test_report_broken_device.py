"""Bot per-device failover endpoints (/api/admin/client-control).

GET  /devices-by-telegram   — список живых устройств для пикера
POST /report-broken-device  — перенос ТОЛЬКО выбранного устройства

Поведение самого failover_device покрыто test_device_failover.py — здесь
проверяем только обвязку эндпоинтов (anti-forge, статус, OperatorNodeReport,
ответ). failover_device мокается.
"""
from __future__ import annotations

import types as _types

from sqlalchemy.orm import Session

from app import models
from app.services.provisioning import ProvisioningOrchestrator
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _setup_two_devices(db: Session):
    plan = make_plan(db)
    user = make_user(db, telegram_id="tg-broken")
    node = make_node(db, name="rbd-a", region="ru")
    cfg = make_config(db, node)
    sub = make_subscription(db, user, plan, node)
    d1 = make_device(db, sub, cfg, access_username="A")
    d1.name = "Телефон"
    d2 = make_device(db, sub, cfg, access_username="B")
    d2.name = "Ноутбук"
    db.commit()
    return user, sub, d1, d2, node


def test_devices_by_telegram_lists_live(client, db_session) -> None:
    user, sub, d1, d2, node = _setup_two_devices(db_session)

    r = client.get(
        f"/api/admin/client-control/devices-by-telegram?telegram_id={user.telegram_id}"
    )
    assert r.status_code == 200, r.text
    devices = r.json()["devices"]
    assert {d["name"] for d in devices} == {"Телефон", "Ноутбук"}
    assert all(d["status"] == "active" for d in devices)

    # revoked устройство исключается из пикера
    d1.status = models.DeviceStatus.revoked
    db_session.commit()
    r2 = client.get(
        f"/api/admin/client-control/devices-by-telegram?telegram_id={user.telegram_id}"
    )
    assert {d["name"] for d in r2.json()["devices"]} == {"Ноутбук"}


def test_devices_by_telegram_unknown_user(client) -> None:
    r = client.get("/api/admin/client-control/devices-by-telegram?telegram_id=nope")
    assert r.status_code == 200
    assert r.json()["devices"] == []


def test_report_broken_device_migrates_only_one(
    client, db_session, monkeypatch
) -> None:
    user, sub, d1, d2, node = _setup_two_devices(db_session)
    fresh = make_node(db_session, name="rbd-fresh", region="ru", host="10.9.9.9")
    fresh_cfg = make_config(db_session, fresh)
    new_dev = make_device(db_session, sub, fresh_cfg, access_username="A-new")
    db_session.commit()

    fake_target = _types.SimpleNamespace(id=fresh.id, name="rbd-fresh", region="ru")
    fake_new = _types.SimpleNamespace(
        id=new_dev.id, name="Телефон", access_username="A-new"
    )

    def fake_failover(self, device):
        assert device.id == d1.id  # переносим ИМЕННО выбранное устройство
        return fake_target, fake_new, None, node.id

    monkeypatch.setattr(ProvisioningOrchestrator, "failover_device", fake_failover)

    r = client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": user.telegram_id, "device_id": d1.id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "migrated"
    assert body["new_node_name"] == "rbd-fresh"
    assert body["device_name"] == "Телефон"

    rep = db_session.get(models.OperatorNodeReport, body["report_id"])
    assert rep is not None
    assert rep.device_id == new_dev.id  # репорт на НОВОЕ устройство
    assert rep.failed_node_id == node.id
    assert rep.target_node_id == fresh.id

    db_session.refresh(d2)
    assert d2.status == models.DeviceStatus.active  # соседнее не тронуто


def test_report_broken_device_anti_forge(client, db_session, monkeypatch) -> None:
    user, sub, d1, d2, node = _setup_two_devices(db_session)
    other = make_user(db_session, telegram_id="tg-other")
    called = {"n": 0}
    monkeypatch.setattr(
        ProvisioningOrchestrator,
        "failover_device",
        lambda self, d: called.__setitem__("n", called["n"] + 1),
    )

    r = client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": other.telegram_id, "device_id": d1.id},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "no_subscription"
    assert called["n"] == 0  # чужое устройство не мигрируется


def test_report_broken_device_revoked_skips(client, db_session, monkeypatch) -> None:
    user, sub, d1, d2, node = _setup_two_devices(db_session)
    d1.status = models.DeviceStatus.revoked
    db_session.commit()

    def boom(self, device):
        raise AssertionError("revoked device must not migrate")

    monkeypatch.setattr(ProvisioningOrchestrator, "failover_device", boom)

    r = client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": user.telegram_id, "device_id": d1.id},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "no_subscription"


def test_report_broken_device_no_target(client, db_session, monkeypatch) -> None:
    user, sub, d1, d2, node = _setup_two_devices(db_session)

    def raise_rt(self, device):
        raise RuntimeError("no fresh node")

    monkeypatch.setattr(ProvisioningOrchestrator, "failover_device", raise_rt)

    r = client.post(
        "/api/admin/client-control/report-broken-device",
        json={"telegram_id": user.telegram_id, "device_id": d1.id},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "no_target"
