"""Stage 4 — balance billing tests (V2 monthly billing).

Covers the public surface of ``app.services.balance``: topup atomicity
under contention, monthly renewal math (``renew_subscription`` +
``total_renewal_cost_kopecks``), insufficient-balance handling, freeze
(1 per calendar year, ``FREEZE_DAYS``), and unfreeze (with
``reprovision_subscription`` mocked since the warm pool / ansible path
isn't exercised here).
"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app import models
from app.db import SessionLocal
from app.services import balance
from app.time_utils import utcnow

from .factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


# ── helpers ──────────────────────────────────────────────────────────

def _make_balance_plan(db, *, daily_rate=500, max_devices=3):
    """Базовый план для биллинг-тестов. ``daily_rate_kopecks`` — вестигиальное
    V1-поле (колонка ещё есть в модели), V2 биллит по ``plan.price``."""
    plan = make_plan(db, max_devices=max_devices)
    plan.daily_rate_kopecks = daily_rate
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _make_active_balance_sub(db):
    """User + plan + node + sub + 1 active device, balance pre-loaded."""
    node = make_node(db)
    cfg = make_config(db, node)
    plan = _make_balance_plan(db)
    user = make_user(db)
    sub = make_subscription(db, user, plan, node)
    make_device(db, sub, cfg)
    sub.next_charge_at = utcnow()
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return user, plan, sub


# ── topup atomicity ──────────────────────────────────────────────────

def test_topup_under_contention_is_atomic(db_session):
    """10 concurrent topups → balance == sum(amounts), ledger has 10 rows.

    The cached column on ``users`` is the source of truth for reads;
    if the row lock leaks, two threads could read the same value, both
    add their delta, and one increment gets lost. We assert the cached
    column matches both ``SUM(ledger)`` and the expected total.
    """
    user = make_user(db_session)
    user_id = user.id

    AMOUNTS = [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]
    expected = sum(AMOUNTS)
    errors: list[Exception] = []

    def worker(amount: int) -> None:
        session = SessionLocal()
        try:
            balance.topup(
                session, user_id, amount, reference=f"thread:{amount}"
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(a,)) for a in AMOUNTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"topup workers raised: {errors}"

    db_session.expire_all()
    fresh = db_session.get(models.User, user_id)
    assert fresh.balance_kopecks == expected

    txs = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=user_id)
        .all()
    )
    assert len(txs) == len(AMOUNTS)
    assert sum(t.amount_kopecks for t in txs) == expected
    assert all(t.kind == models.BalanceTxKind.topup for t in txs)


def test_topup_rejects_non_positive(db_session):
    user = make_user(db_session)
    with pytest.raises(ValueError):
        balance.topup(db_session, user.id, 0, reference="bad")
    with pytest.raises(ValueError):
        balance.topup(db_session, user.id, -100, reference="bad")


# ── renew_subscription (V2 monthly) ──────────────────────────────────
#
# V1 daily-charge + анкер (next_charge_at += 24h) удалены в V2 billing:
# charge_subscription теперь no-op, биллинг идёт помесячно через
# renew_subscription (план + помесячная цена extra_device_slots).

def test_renew_extends_period_and_decrements_balance(db_session):
    """Успешный renew списывает цену плана и продлевает expires_at на
    duration_days (продление от текущего expires_at, а не от now)."""
    user, plan, sub = _make_active_balance_sub(db_session)
    price = balance.plan_price_kopecks(plan)

    balance.topup(db_session, user.id, price * 10, reference="seed")
    db_session.commit()
    db_session.refresh(sub)

    expires_before = sub.expires_at
    ok = balance.renew_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(sub)
    db_session.refresh(user)

    assert ok is True
    # Списана ровно одна цена плана.
    assert user.balance_kopecks == price * 9
    # Период продлён на duration_days плана.
    assert sub.expires_at == expires_before + timedelta(days=plan.duration_days)

    spend_txs = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=user.id, kind=models.BalanceTxKind.spend)
        .all()
    )
    assert len(spend_txs) == 1
    assert spend_txs[0].amount_kopecks == -price


def test_renew_returns_false_on_insufficient_without_partial_spend(db_session):
    """Недостаток баланса → renew возвращает False, не пишет spend tx,
    не двигает expires_at и не трогает кэш баланса."""
    user, plan, sub = _make_active_balance_sub(db_session)
    price = balance.plan_price_kopecks(plan)
    # Чуть меньше одной цены плана.
    balance.topup(db_session, user.id, price - 1, reference="seed")
    db_session.commit()
    db_session.refresh(sub)

    expires_before = sub.expires_at
    ok = balance.renew_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(sub)
    db_session.refresh(user)

    assert ok is False
    assert user.balance_kopecks == price - 1
    assert sub.expires_at == expires_before
    spend_txs = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=user.id, kind=models.BalanceTxKind.spend)
        .count()
    )
    assert spend_txs == 0


def test_renew_charges_extra_device_slots(db_session):
    """renew биллит план + помесячную цену extra_device_slots
    (30-дневный план → ×1 месяц)."""
    user, plan, sub = _make_active_balance_sub(db_session)
    sub.extra_device_slots = 1
    db_session.add(sub)

    base = balance.plan_price_kopecks(plan)
    surcharge = balance.EXTRA_DEVICE_MONTHLY_KOPECKS  # 30 дней // 30 == 1 месяц
    balance.topup(db_session, user.id, base + surcharge, reference="seed")
    db_session.commit()

    ok = balance.renew_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(user)

    assert ok is True
    assert user.balance_kopecks == 0
    spend_txs = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=user.id, kind=models.BalanceTxKind.spend)
        .all()
    )
    assert len(spend_txs) == 1
    assert spend_txs[0].amount_kopecks == -(base + surcharge)


def test_renew_ignores_live_devices_only_slots_billed(db_session):
    """Живые девайсы в пределах бандла не влияют на renew — биллится
    только цена плана (extra_device_slots == 0)."""
    user, plan, sub = _make_active_balance_sub(db_session)
    # Второй живой девайс в пределах бандла (max_devices=3).
    extra_cfg = make_config(db_session, sub.node, name="vless-2", port=8443)
    make_device(db_session, sub, extra_cfg, access_username="user-1-1-extra")

    base = balance.plan_price_kopecks(plan)
    balance.topup(db_session, user.id, base * 5, reference="seed")
    db_session.commit()

    ok = balance.renew_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(user)

    assert ok is True
    # Только базовая цена плана — доп. девайсы (без слотов) не биллятся.
    assert user.balance_kopecks == base * 5 - base


def test_days_remaining(db_session):
    """V2 days_remaining: сколько дней покрывает баланс при помесячном
    renewal (balance // total_renewal_cost * duration_days)."""
    user, plan, sub = _make_active_balance_sub(db_session)
    cost = balance.total_renewal_cost_kopecks(sub)  # цена плана, slots=0
    period = plan.duration_days
    # Ровно 3 renewal'а по силам (+шум < одной цены).
    user.balance_kopecks = cost * 3 + 50
    db_session.add(user)
    db_session.commit()

    assert balance.days_remaining(user, plan, 1) == 3 * period
    # Аргумент devices в V2 игнорируется — надбавка живёт в слотах сабы.
    assert balance.days_remaining(user, plan, 2) == 3 * period

    # План без цены → 0 дней, без падений.
    plan.price = 0
    db_session.add(plan)
    db_session.commit()
    db_session.expire_all()
    fresh = db_session.get(models.User, user.id)
    assert balance.days_remaining(fresh, plan, 1) == 0


# ── freeze / unfreeze ────────────────────────────────────────────────

def test_freeze_revokes_devices_and_blocks_charge(db_session, monkeypatch):
    """Freeze: status=frozen, frozen_until=now+FREEZE_DAYS, devices
    revoked, next_charge_at очищен, has_frozen_this_year выставлен."""
    user, plan, sub = _make_active_balance_sub(db_session)
    # V2: заморозить можно только саб с включённым auto_renew.
    sub.auto_renew = True
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    # Stub revoke_device — we don't want to call into ansible from a
    # unit test. We do want to assert the orchestrator was asked.
    revoked: list[int] = []
    from app.services.provisioning import ProvisioningOrchestrator

    def fake_revoke(self, device, *, reason=None, background=True):
        revoked.append(device.id)
        device.status = models.DeviceStatus.disabled
        return None

    monkeypatch.setattr(ProvisioningOrchestrator, "revoke_device", fake_revoke)

    balance.freeze_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(sub)

    assert sub.status == models.SubscriptionStatus.frozen
    assert sub.frozen_at is not None
    assert sub.frozen_until is not None
    delta = sub.frozen_until - sub.frozen_at
    # Within 1s of FREEZE_DAYS days.
    assert abs(delta - timedelta(days=balance.FREEZE_DAYS)) < timedelta(seconds=1)
    assert sub.next_charge_at is None
    assert sub.has_frozen_this_year is True
    assert sub.frozen_year == utcnow().year
    assert revoked == [sub.devices[0].id]


def test_freeze_rejected_when_already_frozen_this_year(db_session, monkeypatch):
    """V2: 1 заморозка в календарный год. Если has_frozen_this_year уже
    выставлен в текущем году — freeze падает, саб не меняется."""
    user, plan, sub = _make_active_balance_sub(db_session)
    sub.auto_renew = True
    # Год уже использовал свою заморозку.
    sub.has_frozen_this_year = True
    sub.frozen_year = utcnow().year
    db_session.add(sub)
    db_session.commit()

    from app.services.provisioning import ProvisioningOrchestrator

    monkeypatch.setattr(
        ProvisioningOrchestrator, "revoke_device",
        lambda *a, **kw: pytest.fail("revoke_device should not be called"),
    )

    with pytest.raises(RuntimeError, match="freeze rejected"):
        balance.freeze_subscription(db_session, sub)

    db_session.rollback()
    db_session.refresh(sub)
    # Untouched.
    assert sub.status == models.SubscriptionStatus.active
    assert sub.frozen_at is None
    assert sub.frozen_until is None


def test_freeze_allowed_after_calendar_rollover(db_session, monkeypatch):
    """V2: если has_frozen_this_year относится к прошлому году, флаг
    сбрасывается и заморозка снова доступна."""
    user, plan, sub = _make_active_balance_sub(db_session)
    sub.auto_renew = True
    # Прошлогодняя заморозка — не должна блокировать в новом году.
    sub.has_frozen_this_year = True
    sub.frozen_year = utcnow().year - 1
    db_session.add(sub)
    db_session.commit()

    from app.services.provisioning import ProvisioningOrchestrator

    monkeypatch.setattr(
        ProvisioningOrchestrator, "revoke_device",
        lambda self, device, **kw: setattr(device, "status", models.DeviceStatus.disabled),
    )

    balance.freeze_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(sub)

    assert sub.status == models.SubscriptionStatus.frozen
    assert sub.frozen_year == utcnow().year
    assert sub.has_frozen_this_year is True  # выставлен заново в этом году


def test_unfreeze_resumes_billing_and_calls_reprovision(db_session, monkeypatch):
    """Unfreeze: status=active, frozen_at/until очищены,
    reprovision_subscription вызван один раз. Sub_token переживает."""
    user, plan, sub = _make_active_balance_sub(db_session)
    original_token = sub.sub_token = "stable-token-123"
    sub.status = models.SubscriptionStatus.frozen
    sub.frozen_at = utcnow() - timedelta(days=2)
    sub.frozen_until = utcnow() + timedelta(days=12)
    sub.frozen_year = utcnow().year
    sub.has_frozen_this_year = True
    sub.next_charge_at = None
    db_session.add(sub)
    db_session.commit()

    reprovision_calls: list[int] = []
    from app.services.provisioning import ProvisioningOrchestrator

    def fake_reprovision(self, subscription, *, device_name=None):
        reprovision_calls.append(subscription.id)
        return None, None

    monkeypatch.setattr(
        ProvisioningOrchestrator, "reprovision_subscription", fake_reprovision
    )

    balance.unfreeze_subscription(db_session, sub, auto=False)
    db_session.commit()
    db_session.refresh(sub)

    assert sub.status == models.SubscriptionStatus.active
    assert sub.frozen_at is None
    assert sub.frozen_until is None
    # Token preserved — devices that survived the freeze still work.
    assert sub.sub_token == original_token
    # V2 unfreeze не пере-анкерит биллинг (next_charge_at не трогается).
    assert sub.next_charge_at is None
    assert reprovision_calls == [sub.id]


def test_unfreeze_rejects_non_frozen(db_session):
    """Active subs can't be 'unfrozen' — that would reset the anchor
    and trash the device state."""
    _user, _plan, sub = _make_active_balance_sub(db_session)
    with pytest.raises(RuntimeError, match="not frozen"):
        balance.unfreeze_subscription(db_session, sub)


# ── referral bonus ───────────────────────────────────────────────────

def test_referral_bonus_credits_balance_with_kind_bonus(db_session):
    user = make_user(db_session)
    tx = balance.referral_bonus(db_session, user.id, reference="referral:test")
    db_session.commit()
    db_session.refresh(user)

    assert tx.kind == models.BalanceTxKind.bonus
    assert tx.amount_kopecks == balance.REFERRAL_BONUS_KOPECKS
    assert user.balance_kopecks == balance.REFERRAL_BONUS_KOPECKS


# ── total_renewal_cost_kopecks ───────────────────────────────────────
#
# Truth table: plan period × extra slots. The monthly+slots row is the
# 2026-04 regression source (webapp runway showed 532 days instead of
# ~120 because the helper didn't exist and callers divided balance by
# bare plan price). The yearly+slots row guards against a subtler bug —
# EXTRA_DEVICE_MONTHLY_KOPECKS is per calendar month, so annual renewal
# prepays 12× that per slot, not 1×.

def _annual_plan(db):
    plan = make_plan(db, name="annual")
    plan.duration_days = 365
    plan.price = 39  # 39 ₽ → 3900 копеек base
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _monthly_plan(db):
    plan = make_plan(db, name="monthly")
    plan.price = 7
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _sub_with_slots(db, plan, slots: int):
    node = make_node(db)
    user = make_user(db, telegram_id=f"tg-slots-{plan.id}-{slots}")
    sub = make_subscription(db, user, plan, node)
    sub.extra_device_slots = slots
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_total_renewal_cost_monthly_no_slots(db_session):
    plan = _monthly_plan(db_session)
    sub = _sub_with_slots(db_session, plan, 0)
    assert balance.total_renewal_cost_kopecks(sub) == 700


def test_total_renewal_cost_monthly_with_slots(db_session):
    plan = _monthly_plan(db_session)
    sub = _sub_with_slots(db_session, plan, 2)
    # 700 + 2 * 10000 * 1 month
    assert balance.total_renewal_cost_kopecks(sub) == 700 + 2 * balance.EXTRA_DEVICE_MONTHLY_KOPECKS


def test_total_renewal_cost_annual_no_slots(db_session):
    plan = _annual_plan(db_session)
    sub = _sub_with_slots(db_session, plan, 0)
    assert balance.total_renewal_cost_kopecks(sub) == 3900


def test_total_renewal_cost_annual_with_slots(db_session):
    plan = _annual_plan(db_session)
    sub = _sub_with_slots(db_session, plan, 2)
    # 3900 + 2 * 10000 * 12 months (365 // 30 == 12)
    expected = 3900 + 2 * balance.EXTRA_DEVICE_MONTHLY_KOPECKS * 12
    assert balance.total_renewal_cost_kopecks(sub) == expected


def test_total_renewal_cost_no_plan_returns_zero():
    """Defensive path: detached/orphan sub should not crash callers."""
    sub = models.Subscription()
    assert balance.total_renewal_cost_kopecks(sub) == 0
