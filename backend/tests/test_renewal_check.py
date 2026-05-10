"""Integration tests for ``run_renewal_check`` after the May 2026 billing
fix: notification dedup honours the ``:delivered`` ACK suffix, balance-gate
suppresses redundant reminders, and the overdue-expire pass attempts a V2
balance renewal before flipping ``auto_renew=True`` subs to expired.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import models, worker
from tests.factories import make_node, make_plan, make_subscription, make_user


def _fast_forward(sub: models.Subscription, *, days_left: int) -> None:
    """Move expires_at to ``now + days_left``, committing through the session."""
    sub.expires_at = datetime.utcnow() + timedelta(days=days_left)


def _set_balance(user: models.User, kopecks: int) -> None:
    user.balance_kopecks = kopecks


# ── Bug 1: notification dedup honours :delivered suffix ────────────────

def test_renewal_reminder_1d_not_resent_after_ack(db_session, monkeypatch):
    """Bot ACK rewrites action to ``:delivered`` — second tick must NOT
    create a duplicate AuditLog. Before the fix this re-spammed every
    5 minutes for the entire 1-day window."""
    user = make_user(db_session, telegram_id="100")
    _set_balance(user, 0)  # below renewal cost so reminder fires
    plan = make_plan(db_session)
    node = make_node(db_session)
    sub = make_subscription(db_session, user, plan, node)
    _fast_forward(sub, days_left=1)
    db_session.add_all([user, sub])
    db_session.commit()

    # Reschedule path uses the queue — bypass.
    monkeypatch.setattr(worker, "schedule_tick", lambda *a, **k: None)

    worker.run_renewal_check()

    logs = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.target_type == "subscription",
                models.AuditLog.target_id == sub.id,
                models.AuditLog.action.like("renewal_reminder_1d%"))
        .all()
    )
    assert len(logs) == 1, f"first tick should create exactly one log, got {len(logs)}"

    # Simulate bot ACK.
    logs[0].action = "renewal_reminder_1d:delivered"
    db_session.add(logs[0])
    db_session.commit()

    worker.run_renewal_check()

    logs2 = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.target_type == "subscription",
                models.AuditLog.target_id == sub.id,
                models.AuditLog.action.like("renewal_reminder_1d%"))
        .all()
    )
    assert len(logs2) == 1, "post-ACK re-tick must not duplicate the reminder"


def test_expiry_reminder_3d_not_resent_after_ack(db_session, monkeypatch):
    """Same bug applied to non-auto-renew 3-day reminders."""
    user = make_user(db_session, telegram_id="200")
    plan = make_plan(db_session)
    node = make_node(db_session)
    sub = make_subscription(db_session, user, plan, node)
    sub.auto_renew = False
    _fast_forward(sub, days_left=2)
    db_session.add(sub)
    db_session.commit()

    monkeypatch.setattr(worker, "schedule_tick", lambda *a, **k: None)
    worker.run_renewal_check()

    logs = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action.like("expiry_reminder%"),
                models.AuditLog.target_id == sub.id)
        .all()
    )
    assert len(logs) == 1
    logs[0].action = "expiry_reminder:delivered"
    db_session.add(logs[0])
    db_session.commit()

    worker.run_renewal_check()
    logs2 = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action.like("expiry_reminder%"),
                models.AuditLog.target_id == sub.id)
        .all()
    )
    assert len(logs2) == 1


# ── Bug 1.1: skip reminder when balance covers next renewal ────────────

def test_renewal_reminder_skipped_when_balance_sufficient(db_session, monkeypatch):
    """Auto-renew + funds enough → no reminder; balance tick handles it silently."""
    user = make_user(db_session, telegram_id="300")
    plan = make_plan(db_session)  # price=10 RUB → 1000 kopecks
    _set_balance(user, 100_000)  # plenty
    node = make_node(db_session)
    sub = make_subscription(db_session, user, plan, node)
    _fast_forward(sub, days_left=2)
    db_session.add_all([user, sub])
    db_session.commit()

    monkeypatch.setattr(worker, "schedule_tick", lambda *a, **k: None)
    worker.run_renewal_check()

    logs = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.target_id == sub.id,
                models.AuditLog.action.in_(["renewal_reminder", "renewal_reminder_1d"]))
        .all()
    )
    assert logs == [], "balance covers cost → no reminder should be emitted"


def test_renewal_reminder_emitted_when_balance_insufficient(db_session, monkeypatch):
    user = make_user(db_session, telegram_id="301")
    plan = make_plan(db_session)
    _set_balance(user, 100)  # well below 1000 kopecks
    node = make_node(db_session)
    sub = make_subscription(db_session, user, plan, node)
    _fast_forward(sub, days_left=2)
    db_session.add_all([user, sub])
    db_session.commit()

    monkeypatch.setattr(worker, "schedule_tick", lambda *a, **k: None)
    worker.run_renewal_check()

    logs = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.target_id == sub.id,
                models.AuditLog.action == "renewal_reminder")
        .all()
    )
    assert len(logs) == 1, "low balance must still produce a reminder"


# ── Bug 2: race-fix — overdue auto_renew sub renews instead of expiring ──

def test_overdue_auto_renew_with_balance_renews(db_session, monkeypatch):
    """V2 balance renewal MUST fire before the overdue→expired flip,
    otherwise auto-renew with sufficient balance never renews because
    `run_balance_charge_tick` only sees status=active rows."""
    user = make_user(db_session, telegram_id="400")
    plan = make_plan(db_session)
    _set_balance(user, 1_000_000)
    node = make_node(db_session)
    sub = make_subscription(db_session, user, plan, node)
    sub.expires_at = datetime.utcnow() - timedelta(minutes=10)  # already expired
    db_session.add_all([user, sub])
    db_session.commit()

    monkeypatch.setattr(worker, "schedule_tick", lambda *a, **k: None)
    worker.run_renewal_check()

    db_session.refresh(sub)
    db_session.refresh(user)
    assert sub.status == models.SubscriptionStatus.active
    assert sub.expires_at > datetime.utcnow()
    # 30-day plan @ 10 RUB → 1000 kopecks debited
    assert user.balance_kopecks == 1_000_000 - 1000


def test_overdue_auto_renew_without_balance_expires(db_session, monkeypatch):
    user = make_user(db_session, telegram_id="401")
    plan = make_plan(db_session)
    _set_balance(user, 50)  # not enough
    node = make_node(db_session)
    sub = make_subscription(db_session, user, plan, node)
    sub.expires_at = datetime.utcnow() - timedelta(minutes=10)
    db_session.add_all([user, sub])
    db_session.commit()

    monkeypatch.setattr(worker, "schedule_tick", lambda *a, **k: None)
    worker.run_renewal_check()

    db_session.refresh(sub)
    assert sub.status == models.SubscriptionStatus.expired


def test_overdue_manual_sub_still_expires(db_session, monkeypatch):
    """auto_renew=False subs are not in scope of the renewal attempt —
    they go straight to expired. Balance is irrelevant in this branch."""
    user = make_user(db_session, telegram_id="402")
    _set_balance(user, 1_000_000)  # has money but opted out of auto-renew
    plan = make_plan(db_session)
    node = make_node(db_session)
    sub = make_subscription(db_session, user, plan, node)
    sub.auto_renew = False
    sub.expires_at = datetime.utcnow() - timedelta(minutes=10)
    db_session.add_all([user, sub])
    db_session.commit()

    monkeypatch.setattr(worker, "schedule_tick", lambda *a, **k: None)
    worker.run_renewal_check()

    db_session.refresh(sub)
    assert sub.status == models.SubscriptionStatus.expired


# ── Yearly plan parity ─────────────────────────────────────────────────

def test_yearly_plan_renews_correctly(db_session, monkeypatch):
    """365-day plans must follow the same auto-renew path; cost is
    just plan.price × 100, not amplified by the period."""
    user = make_user(db_session, telegram_id="500")
    yearly = make_plan(db_session, name="annual")
    yearly.duration_days = 365
    yearly.price = 100  # 100 RUB → 10 000 kopecks
    _set_balance(user, 1_000_000)
    node = make_node(db_session)
    sub = make_subscription(db_session, user, yearly, node)
    sub.expires_at = datetime.utcnow() - timedelta(minutes=5)
    db_session.add_all([user, yearly, sub])
    db_session.commit()

    monkeypatch.setattr(worker, "schedule_tick", lambda *a, **k: None)
    worker.run_renewal_check()

    db_session.refresh(sub)
    db_session.refresh(user)
    assert sub.status == models.SubscriptionStatus.active
    # 365-day extension from prior expires_at (already in the past, but
    # services.balance.renew_subscription extends from current expires_at).
    assert sub.expires_at > datetime.utcnow() + timedelta(days=360)
    assert user.balance_kopecks == 1_000_000 - 10_000
