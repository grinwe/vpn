"""Аудит-фиксы в services/provisioning.py (находки 49, 50).

49: провал revoke-таски не должен «воскрешать» списанный девайс —
    disabled/revoked терминальны, даунгрейд в failed делал фантом живым
    в active_device_count / ЛК / снапшотах миграции.
50: сбой best-effort добора _maybe_attach_diverse не должен откатывать
    ВСЮ сессию — в warm-пути подписка/девайс к этому моменту только
    flush'нуты, общий rollback стирал их из БД («фантомный» успех).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services import provisioning, warm_pool
from app.services.provisioning import ProvisioningOrchestrator
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


# ---------------------------------------------------------------------------
# Находка 49 — failed revoke не даунгрейдит disabled → failed
# ---------------------------------------------------------------------------

def _live_device_count(db: Session, user_id: int, plan_id: int) -> int:
    """Тот же фильтр «живых», что в provision_subscription (лимит девайсов)."""
    return (
        db.query(models.Device)
        .join(models.Subscription)
        .filter(
            models.Subscription.user_id == user_id,
            models.Subscription.plan_id == plan_id,
            models.Device.status.notin_([
                models.DeviceStatus.revoked, models.DeviceStatus.disabled
            ]),
        )
        .count()
    )


def test_failed_revoke_keeps_device_disabled(db_session: Session) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session)
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)
    device = make_device(db_session, sub, cfg)

    orch = ProvisioningOrchestrator(db_session)
    # Ровно как revoke_device: девайс списывается ДО запуска ansible.
    task = orch.create_task(
        "device", device.id, "revoke", {"username": device.access_username}
    )
    device.status = models.DeviceStatus.disabled
    db_session.commit()

    # ansible-revoke упал (типовой кейс: нода мертва при failover/migrate)
    orch._handle_task_outcome(task, success=False)

    db_session.expire_all()
    refreshed = db_session.get(models.Device, device.id)
    assert refreshed is not None
    assert refreshed.status == models.DeviceStatus.disabled
    # Зомби не всплывает в лимите девайсов плана.
    assert _live_device_count(db_session, user.id, plan.id) == 0


def test_failed_apply_still_marks_device_failed(db_session: Session) -> None:
    """Регресс-гард: провал ОБЫЧНОГО apply по-прежнему помечает failed."""
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
    db_session.commit()

    orch._handle_task_outcome(task, success=False)

    db_session.expire_all()
    refreshed = db_session.get(models.Device, device.id)
    assert refreshed is not None
    assert refreshed.status == models.DeviceStatus.failed


# ---------------------------------------------------------------------------
# Находка 50 — сбой добора не стирает flush'нутые строки вызывающего кода
# ---------------------------------------------------------------------------

def test_diverse_attach_failure_keeps_uncommitted_rows(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIVERSE_SUB_NODES", "2")
    plan = make_plan(db_session)
    user = make_user(db_session)
    primary = make_node(db_session, name="ru-1", region="ru")
    cfg = make_config(db_session, primary)
    diverse = make_node(db_session, name="de-1", region="de", host="198.51.100.20")

    # Warm-путь provision_subscription: подписка и девайс только flush'нуты,
    # commit'а ещё НЕ было — ровно то состояние, где старый rollback их стирал.
    sub = models.Subscription(
        user_id=user.id,
        plan_id=plan.id,
        node_id=primary.id,
        expires_at=datetime.utcnow() + timedelta(days=30),
        status=models.SubscriptionStatus.active,
        traffic_limit_mb=plan.traffic_limit_mb,
    )
    db_session.add(sub)
    db_session.flush()
    device = models.Device(
        user_id=user.id,
        subscription_id=sub.id,
        config_id=cfg.id,
        name="primary",
        status=models.DeviceStatus.active,
        access_username=f"user-{user.id}-{sub.id}",
    )
    db_session.add(device)
    db_session.flush()
    sub_id, device_id = sub.id, device.id

    monkeypatch.setattr(provisioning, "choose_node", lambda *a, **k: diverse)

    def _boom(*a, **k):  # гонка в warm-пуле / любой сбой добора
        raise RuntimeError("warm-pool race")

    monkeypatch.setattr(warm_pool, "try_assign_bundle", _boom)

    orch = ProvisioningOrchestrator(db_session)
    # Не должно ни бросить, ни откатить внешнюю транзакцию.
    orch._maybe_attach_diverse(sub, device, plan, primary)

    # Коммит вызывающего кода фиксирует подписку и девайс как обычно.
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(models.Subscription, sub_id) is not None
    assert db_session.get(models.Device, device_id) is not None
