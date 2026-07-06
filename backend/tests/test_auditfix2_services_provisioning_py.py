"""Аудит-фиксы (волна 2) в services/provisioning.py.

53:  revoke_subscription_devices не должен гонять ansible-revoke по уже
     отозванным (revoked/disabled) историческим девайсам — только по живым.
56:  отменённая device-apply таска не должна оставлять девайс в pending
     навсегда (фантом в лимитах и «настраивается» в ЛК) — переводим в failed.
58:  switch_subscription_exit перепинывает exit_id только у кредов ЦЕЛЕВОЙ
     ноды; креды диверс-набора на других нодах не трогаются.
243: _release_sub_token обнуляет sub_token И client_id_hmac одним хелпером.
"""
from __future__ import annotations

from app import models
from app.security import encrypt
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


# ---------------------------------------------------------------------------
# Находка 53 — revoke_subscription_devices скипает терминальные девайсы
# ---------------------------------------------------------------------------

def test_revoke_subscription_devices_skips_retired(db_session) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session)
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)

    live = make_device(db_session, sub, cfg, access_username="user-1-1-live")
    revoked = make_device(db_session, sub, cfg, access_username="user-1-1-rev")
    disabled = make_device(db_session, sub, cfg, access_username="user-1-1-dis")
    revoked.status = models.DeviceStatus.revoked
    disabled.status = models.DeviceStatus.disabled
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    tasks = orch.revoke_subscription_devices(sub, reason="block")

    # Ровно одна revoke-таска — на живой девайс, а не 3 (по одной на каждую
    # историческую строку).
    assert len(tasks) == 1
    assert tasks[0].target_id == live.id

    revoke_task_count = (
        db_session.query(models.ProvisioningTask)
        .filter(
            models.ProvisioningTask.target_type == "device",
            models.ProvisioningTask.action == "revoke",
        )
        .count()
    )
    assert revoke_task_count == 1

    db_session.refresh(sub)
    assert sub.status == models.SubscriptionStatus.blocked


# ---------------------------------------------------------------------------
# Находка 56 — отменённая device-apply таска переводит девайс в failed
# ---------------------------------------------------------------------------

def test_cancelled_apply_marks_pending_device_failed(db_session) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session)
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)
    device = make_device(db_session, sub, cfg)
    device.status = models.DeviceStatus.pending
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    task = orch.create_task(
        "device", device.id, "apply", {"username": device.access_username}
    )
    # Оператор отменил таску, пока она ждала в очереди (pre-start ветка).
    task.cancel_requested_at = utcnow()
    db_session.commit()

    orch.run_task(task, node=node)

    db_session.refresh(task)
    db_session.refresh(device)
    assert task.status == models.ProvisioningTaskStatus.cancelled
    # Ключевое: девайс НЕ остался в pending (иначе — вечный фантом).
    assert device.status == models.DeviceStatus.failed


def test_cancelled_apply_does_not_downgrade_active_device(db_session) -> None:
    """Регресс-гард: cancel re-apply уже активного девайса не роняет его."""
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session)
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)
    device = make_device(db_session, sub, cfg)  # status=active по умолчанию
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    task = orch.create_task(
        "device", device.id, "apply", {"username": device.access_username}
    )
    task.cancel_requested_at = utcnow()
    db_session.commit()

    orch.run_task(task, node=node)

    db_session.refresh(device)
    assert device.status == models.DeviceStatus.active


# ---------------------------------------------------------------------------
# Находка 243 — _release_sub_token обнуляет оба UNIQUE-поля
# ---------------------------------------------------------------------------

def test_release_sub_token_clears_both_unique_fields(db_session) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session)
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)
    device = make_device(db_session, sub, cfg)
    device.sub_token = "tok-abc"
    device.client_id_hmac = "hmac-abc"
    db_session.commit()

    orch = ProvisioningOrchestrator(db_session)
    orch._release_sub_token(device)  # noqa: SLF001

    db_session.refresh(device)
    assert device.sub_token is None
    # client_id_hmac производный от sub_token и тоже под UNIQUE-индексом —
    # если бы хелпер его не обнулил, reprovision INSERT упёрся бы в
    # ix_devices_client_id_hmac.
    assert device.client_id_hmac is None


# ---------------------------------------------------------------------------
# Находка 58 — switch_subscription_exit трогает только креды целевой ноды
# ---------------------------------------------------------------------------

def _make_exit(db, *, name: str, host: str) -> models.WGExitNode:
    exit_node = models.WGExitNode(
        name=name,
        region="eu",
        host=host,
        status=models.WGExitNodeStatus.active,
        is_active=True,
    )
    db.add(exit_node)
    db.commit()
    db.refresh(exit_node)
    return exit_node


def _attach_exit(db, relay, exit_node, *, interface, client_ipv4):
    link = models.RelayExitLink(
        relay_node_id=relay.id,
        exit_id=exit_node.id,
        wg_interface_name=interface,
        wg_client_private_key_enc=encrypt("dummy-privkey"),
        wg_client_public_key=f"pubkey-{interface}",
        wg_client_address_v4=client_ipv4,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def _cred(db, *, sub, device, cfg, node_id, exit_id, username):
    cred = models.Credential(
        subscription_id=sub.id,
        device_id=device.id,
        config_id=cfg.id,
        node_id=node_id,
        exit_id=exit_id,
        proto=cfg.protocol.value,
        config_text=encrypt(f"dummy-cred-{username}"),
        access_username=username,
        is_active=True,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


def test_switch_subscription_exit_leaves_other_node_creds(db_session) -> None:
    relay = make_node(db_session, name="relay-A", host="10.0.0.10")
    cfg_a = make_config(db_session, relay)
    node_b = make_node(db_session, name="diverse-B", host="10.0.0.20")
    cfg_b = make_config(db_session, node_b, name="vless-b")

    exit_x = _make_exit(db_session, name="exit-X", host="203.0.113.10")
    exit_y = _make_exit(db_session, name="exit-Y", host="203.0.113.20")
    _attach_exit(db_session, relay, exit_x, interface="wg0", client_ipv4="10.77.0.5/32")
    _attach_exit(db_session, relay, exit_y, interface="wg1", client_ipv4="10.78.0.5/32")

    plan = make_plan(db_session)
    user = make_user(db_session)
    sub = make_subscription(db_session, user, plan, relay)
    device = make_device(db_session, sub, cfg_a)

    # Cred на ноде relay-A (целевая) и диверс-cred на ноде B.
    cred_a = _cred(
        db_session, sub=sub, device=device, cfg=cfg_a,
        node_id=relay.id, exit_id=exit_x.id, username="user-1-1-a",
    )
    cred_b = _cred(
        db_session, sub=sub, device=device, cfg=cfg_b,
        node_id=node_b.id, exit_id=None, username="user-1-1-b",
    )

    orch = ProvisioningOrchestrator(db_session)
    orch.switch_subscription_exit(sub, exit_y.id)

    db_session.refresh(cred_a)
    db_session.refresh(cred_b)
    # Целевая нода перепинута.
    assert cred_a.exit_id == exit_y.id
    # Диверс-cred на ноде B НЕ получил чужой exit_id.
    assert cred_b.exit_id is None
