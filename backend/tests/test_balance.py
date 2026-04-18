"""Stage 4 — balance billing tests.

Covers the public surface of ``app.services.balance``: topup atomicity
under contention, daily charge math + anchor advancement, insufficient
balance handling, freeze year-budget enforcement, and unfreeze (with
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
    """Plan with a daily rate set — required for charge_subscription."""
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


# ── charge_subscription ──────────────────────────────────────────────

def test_charge_advances_anchor_and_decrements_balance(db_session):
    user, plan, sub = _make_active_balance_sub(db_session)

    # Pre-load 10 days worth of balance.
    balance.topup(db_session, user.id, plan.daily_rate_kopecks * 10, reference="seed")
    db_session.commit()

    anchor_before = sub.next_charge_at
    ok = balance.charge_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(sub)
    db_session.refresh(user)

    assert ok is True
    # Exactly one daily rate burned.
    assert user.balance_kopecks == plan.daily_rate_kopecks * 9
    # Anchor moved by exactly +24h regardless of when the tick fired.
    assert sub.next_charge_at == anchor_before + timedelta(hours=24)

    spend_txs = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=user.id, kind=models.BalanceTxKind.spend)
        .all()
    )
    assert len(spend_txs) == 1
    assert spend_txs[0].amount_kopecks == -plan.daily_rate_kopecks


def test_charge_returns_false_on_insufficient_without_partial_spend(db_session):
    """Insufficient balance must not write any spend tx, must not move
    the anchor, and must leave the cached balance untouched."""
    user, plan, sub = _make_active_balance_sub(db_session)
    # Give them just under one day's worth.
    balance.topup(db_session, user.id, plan.daily_rate_kopecks - 1, reference="seed")
    db_session.commit()

    anchor_before = sub.next_charge_at
    ok = balance.charge_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(sub)
    db_session.refresh(user)

    assert ok is False
    assert user.balance_kopecks == plan.daily_rate_kopecks - 1
    assert sub.next_charge_at == anchor_before
    spend_txs = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=user.id, kind=models.BalanceTxKind.spend)
        .count()
    )
    assert spend_txs == 0


def test_charge_cost_extras_above_bundle(db_session):
    """Plan rate is flat for the bundled max_devices; each device above
    that adds EXTRA_DEVICE_DAILY_KOPECKS. With max_devices=1, going to
    2 devices should add exactly one extras-charge on top of the base."""
    user, plan, sub = _make_active_balance_sub(db_session)
    plan.max_devices = 1  # bundle of 1 → second device is an extra
    db_session.add(plan)
    # Add a second active device.
    extra_cfg = make_config(db_session, sub.node, name="vless-2", port=8443)
    make_device(db_session, sub, extra_cfg, access_username="user-1-1-extra")

    balance.topup(db_session, user.id, plan.daily_rate_kopecks * 10, reference="seed")
    db_session.commit()

    ok = balance.charge_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(user)

    assert ok is True
    expected_charge = plan.daily_rate_kopecks + balance.EXTRA_DEVICE_DAILY_KOPECKS
    assert user.balance_kopecks == plan.daily_rate_kopecks * 10 - expected_charge


def test_charge_cost_within_bundle_is_flat(db_session):
    """Two devices on a max_devices=3 plan → still flat daily_rate."""
    user, plan, sub = _make_active_balance_sub(db_session)
    extra_cfg = make_config(db_session, sub.node, name="vless-2", port=8443)
    make_device(db_session, sub, extra_cfg, access_username="user-1-1-extra")

    balance.topup(db_session, user.id, plan.daily_rate_kopecks * 5, reference="seed")
    db_session.commit()

    ok = balance.charge_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(user)

    assert ok is True
    assert user.balance_kopecks == plan.daily_rate_kopecks * 5 - plan.daily_rate_kopecks


def test_days_remaining(db_session):
    user, plan, _sub = _make_active_balance_sub(db_session)
    # 10 days worth on a 1-device sub.
    user.balance_kopecks = plan.daily_rate_kopecks * 10 + 50  # +noise
    db_session.add(user)
    db_session.commit()

    assert balance.days_remaining(user, plan, 1) == 10
    assert balance.days_remaining(user, plan, 2) == 5
    # Plan with no daily rate → 0 days, never crashes
    plan.daily_rate_kopecks = None
    assert balance.days_remaining(user, plan, 1) == 0


# ── freeze / unfreeze ────────────────────────────────────────────────

def test_freeze_revokes_devices_and_blocks_charge(db_session, monkeypatch):
    """Freeze: status=frozen, frozen_until=now+14d, devices revoked,
    next charge tick should skip the sub."""
    user, plan, sub = _make_active_balance_sub(db_session)

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
    # Within 1s of MAX_FREEZE_DAYS_PER_PERIOD days
    assert abs(delta - timedelta(days=balance.MAX_FREEZE_DAYS_PER_PERIOD)) < timedelta(seconds=1)
    assert sub.next_charge_at is None
    assert sub.frozen_year == utcnow().year
    assert revoked == [sub.devices[0].id]


def test_freeze_rejected_when_year_budget_exhausted(db_session, monkeypatch):
    """If frozen_days_used + MAX_FREEZE_DAYS_PER_PERIOD > FREEZE_YEAR_BUDGET_DAYS
    then freeze raises and the sub stays exactly as it was."""
    user, plan, sub = _make_active_balance_sub(db_session)

    # Pretend the user already burned almost the entire year budget.
    sub.frozen_days_used = balance.FREEZE_YEAR_BUDGET_DAYS - balance.MAX_FREEZE_DAYS_PER_PERIOD + 1
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


def test_freeze_resets_year_budget_on_calendar_rollover(db_session, monkeypatch):
    """If frozen_year is from a previous year, the used counter resets
    to zero before the budget check runs."""
    user, plan, sub = _make_active_balance_sub(db_session)
    # Old year, "fully used" — would normally block, but the year is stale.
    sub.frozen_days_used = balance.FREEZE_YEAR_BUDGET_DAYS
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
    assert sub.frozen_days_used == 0  # reset before this freeze


def test_unfreeze_resumes_billing_and_calls_reprovision(db_session, monkeypatch):
    """Unfreeze: status=active, anchor=now, frozen_days_used incremented,
    reprovision_subscription called once. Sub_token must survive."""
    user, plan, sub = _make_active_balance_sub(db_session)
    original_token = sub.sub_token = "stable-token-123"
    sub.status = models.SubscriptionStatus.frozen
    sub.frozen_at = utcnow() - timedelta(days=2)  # 2 days "elapsed"
    sub.frozen_until = utcnow() + timedelta(days=12)
    sub.frozen_year = utcnow().year
    sub.frozen_days_used = 0
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
    assert sub.next_charge_at is not None
    # Token preserved — devices that survived the freeze still work.
    assert sub.sub_token == original_token
    # 2 days elapsed → counted toward the year budget.
    assert sub.frozen_days_used == 2
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
