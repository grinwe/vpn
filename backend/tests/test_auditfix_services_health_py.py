"""Audit-fix id96: массовая миграция не должна терять sub_token при сбое.

До фикса ``migrate_subscriptions_off`` NULL-ил sub_token/client_id_hmac живых
девайсов и коммитил это вместе с флипом node_id ДО репровижена. Если
``reprovision_subscription`` падал, rollback уже закоммиченное не откатывал —
токен не носила ни одна строка Device, /api/sub/{token} умирал навсегда.
Плюс девайсы отзывались до флипа, так что ретрай был невозможен (пустой
live_devices_snapshot).

Фикс (backend/app/services/health.py):

* компенсирующий блок — при сбое репровижена не перенесённые токены
  возвращаются на исходные строки Device;
* отзыв старых девайсов перенесён ПОСЛЕ успешного репровижена — при сбое
  живые девайсы остаются нетронутыми и ретрай миграции реально возможен.
"""
from __future__ import annotations

import types

from app import models
from app.security import compute_client_id_hmac
from app.services import provisioning as prov_mod
from app.services.health import migrate_subscriptions_off
from app.services.provisioning import ProvisioningOrchestrator

from .factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _sub_with_three_devices(db):
    """Сабка на old-ноде с 3 живыми девайсами, у каждого свой sub_token."""
    plan = make_plan(db)
    user = make_user(db, telegram_id="tg-id96")
    old = make_node(db, name="id96-old", region="ru")
    new = make_node(db, name="id96-new", region="ru", host="198.51.100.96")
    cfg = make_config(db, old)
    new_cfg = make_config(db, new)
    sub = make_subscription(db, user, plan, old)

    tokens = ["tok-id96-a", "tok-id96-b", "tok-id96-c"]
    devices = []
    for i, tok in enumerate(tokens, start=1):
        dev = make_device(db, sub, cfg, access_username=f"u-id96-{i}")
        dev.name = f"dev-{i}"
        dev.sub_token = tok
        dev.client_id_hmac = compute_client_id_hmac(tok)
        db.add(dev)
        devices.append(dev)
    db.commit()
    db.refresh(sub)
    return sub, old, new, new_cfg, tokens, devices


def test_reprovision_failure_keeps_all_tokens_resolvable(db_session, monkeypatch):
    """Сбой reprovision на 2-м девайсе из 3 → все 3 токена носит ровно одна
    строка Device: 1-й — новый девайс на целевой ноде, 2-й и 3-й —
    восстановлены на исходных строках (компенсация)."""
    sub, old, new, new_cfg, tokens, devices = _sub_with_three_devices(db_session)
    sub_id = sub.id
    orig_ids = {d.sub_token: d.id for d in devices}

    monkeypatch.setattr(prov_mod, "choose_node", lambda *a, **k: new)
    monkeypatch.setattr(prov_mod, "_node_has_vless_family", lambda *a, **k: False)

    revoke_calls: list[int] = []

    def fake_revoke(self, device, **kw):
        revoke_calls.append(device.id)
        return types.SimpleNamespace(id=0)

    monkeypatch.setattr(ProvisioningOrchestrator, "revoke_device", fake_revoke)

    calls: list[str] = []

    def fake_reprovision(
        self,
        sub_arg,
        *,
        device_name=None,
        target_node=None,
        reuse_sub_token=None,
        reuse_connection_uri=None,
        reuse_uuid=None,
    ):
        calls.append(device_name)
        if len(calls) == 2:
            raise RuntimeError("boom: target node down mid-batch")
        # Успешный вызов ведёт себя как настоящий: создаёт и КОММИТИТ новый
        # девайс, несущий reuse_sub_token (поэтому его нельзя «восстанавливать»).
        new_dev = models.Device(
            user_id=sub_arg.user_id,
            subscription_id=sub_arg.id,
            config_id=new_cfg.id,
            name=device_name,
            status=models.DeviceStatus.pending,
            access_username=f"nu-id96-{len(calls)}",
            sub_token=reuse_sub_token,
            client_id_hmac=(
                compute_client_id_hmac(reuse_sub_token) if reuse_sub_token else None
            ),
        )
        self.db.add(new_dev)
        self.db.commit()
        return new_dev, None

    monkeypatch.setattr(
        ProvisioningOrchestrator, "reprovision_subscription", fake_reprovision
    )

    result = migrate_subscriptions_off(db_session, old, reason="id96-test")

    # Сабка НЕ отчитана как мигрированная.
    assert sub_id not in result["subscription_ids"]

    db_session.expire_all()
    # Ключевой инвариант: каждый из 3 токенов носит ровно одна строка Device
    # → /api/sub/{token} резолвится для всех трёх.
    for tok in tokens:
        carriers = (
            db_session.query(models.Device)
            .filter(models.Device.sub_token == tok)
            .all()
        )
        assert len(carriers) == 1, f"token {tok} carriers={len(carriers)}"

    # Токен девайса из УСПЕШНОГО 1-го вызова переехал на НОВЫЙ девайс,
    # остальные два — восстановлены на исходных строках вместе с
    # client_id_hmac. Порядок sub.devices не гарантирован, поэтому какой
    # именно девайс успел — берём из фактического 1-го вызова reprovision.
    name_to_token = {f"dev-{i}": tok for i, tok in enumerate(tokens, start=1)}
    ok_token = name_to_token[calls[0]]
    restored_tokens = [t for t in tokens if t != ok_token]
    carrier_ok = (
        db_session.query(models.Device)
        .filter(models.Device.sub_token == ok_token)
        .one()
    )
    assert carrier_ok.id != orig_ids[ok_token]
    for tok in restored_tokens:
        carrier = (
            db_session.query(models.Device)
            .filter(models.Device.sub_token == tok)
            .one()
        )
        assert carrier.id == orig_ids[tok]
        assert carrier.client_id_hmac == compute_client_id_hmac(tok)
        # Старый девайс не отозван — ретрай миграции возможен.
        assert carrier.status == models.DeviceStatus.active

    # revoke_device при сбое НЕ вызывался — живые девайсы нетронуты.
    assert revoke_calls == []


def test_revoke_runs_only_after_successful_reprovision(db_session, monkeypatch):
    """Успешная миграция: отзыв старых девайсов идёт ПОСЛЕ всех репровиженов
    (а не до), и отзываются именно старые строки, а не новые."""
    sub, old, new, new_cfg, tokens, devices = _sub_with_three_devices(db_session)
    sub_id = sub.id
    old_device_ids = {d.id for d in devices}

    monkeypatch.setattr(prov_mod, "choose_node", lambda *a, **k: new)
    monkeypatch.setattr(prov_mod, "_node_has_vless_family", lambda *a, **k: False)

    order: list[str] = []
    revoked_ids: list[int] = []

    def fake_revoke(self, device, **kw):
        order.append("revoke")
        revoked_ids.append(device.id)
        return types.SimpleNamespace(id=0)

    def fake_reprovision(self, sub_arg, *, device_name=None, **kw):
        order.append("reprovision")
        new_dev = models.Device(
            user_id=sub_arg.user_id,
            subscription_id=sub_arg.id,
            config_id=new_cfg.id,
            name=device_name,
            status=models.DeviceStatus.pending,
            access_username=f"nu2-id96-{len(order)}",
            sub_token=kw.get("reuse_sub_token"),
            client_id_hmac=(
                compute_client_id_hmac(kw["reuse_sub_token"])
                if kw.get("reuse_sub_token")
                else None
            ),
        )
        self.db.add(new_dev)
        self.db.commit()
        return new_dev, None

    monkeypatch.setattr(ProvisioningOrchestrator, "revoke_device", fake_revoke)
    monkeypatch.setattr(
        ProvisioningOrchestrator, "reprovision_subscription", fake_reprovision
    )

    result = migrate_subscriptions_off(db_session, old, reason="id96-test-ok")

    assert sub_id in result["subscription_ids"]
    # Все reprovision строго раньше всех revoke.
    assert order == ["reprovision"] * 3 + ["revoke"] * 3
    # Отозваны именно СТАРЫЕ девайсы (новые, созданные репровиженом, не тронуты).
    assert set(revoked_ids) == old_device_ids
