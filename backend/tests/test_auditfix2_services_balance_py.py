"""Audit-fix wave 2 — regressions in ``app.services.balance``.

Covers finding 109 (доп. слоты на годовом плане должны масштабироваться
на число месяцев при renew/change_plan) и 110 (проration-рефанд при
смене плана не должен превышать цену старого плана даже после заморозки,
которая растягивает ``expires_at`` на ``FREEZE_DAYS``).
"""
from __future__ import annotations

from datetime import timedelta

from app import models
from app.services import balance
from app.time_utils import utcnow

from .factories import make_node, make_plan, make_subscription, make_user


EXTRA = balance.EXTRA_DEVICE_MONTHLY_KOPECKS


# ── 109: renew scales device slots by plan months ────────────────────

def test_renew_annual_scales_slots_by_months(db_session):
    """Годовой план + 2 слота → списывается base + 2*EXTRA*12."""
    node = make_node(db_session)
    plan = make_plan(db_session, name="annual-renew")
    plan.duration_days = 365
    plan.price = 39  # 3900 копеек base
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)

    user = make_user(db_session, telegram_id="tg-renew-annual")
    sub = make_subscription(db_session, user, plan, node)
    sub.extra_device_slots = 2
    sub.auto_renew = True
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    months = 365 // 30  # 12
    expected_price = 3900 + 2 * EXTRA * months
    # Кошелёк ровно на одно продление.
    balance.topup(db_session, user.id, expected_price, reference="seed")
    db_session.commit()

    ok = balance.renew_subscription(db_session, sub)
    db_session.commit()
    db_session.refresh(user)

    assert ok is True
    assert user.balance_kopecks == 0

    spend = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=user.id, kind=models.BalanceTxKind.spend)
        .one()
    )
    assert spend.amount_kopecks == -expected_price


# ── 110: change_plan refund capped at old plan price ─────────────────

def test_change_plan_refund_capped_after_freeze(db_session):
    """expires_at растянут за duration_days → рефанд == old_price, не больше."""
    node = make_node(db_session)
    old_plan = make_plan(db_session, name="cap-old")
    old_plan.duration_days = 30
    old_plan.price = 100  # 10000 копеек
    db_session.add(old_plan)
    new_plan = make_plan(db_session, name="cap-new")
    new_plan.duration_days = 30
    new_plan.price = 70  # 7000 копеек
    db_session.add(new_plan)
    db_session.commit()
    db_session.refresh(old_plan)
    db_session.refresh(new_plan)

    user = make_user(db_session, telegram_id="tg-cap")
    sub = make_subscription(db_session, user, old_plan, node)
    # Симулируем заморозку: expires_at += FREEZE_DAYS сверх полного периода.
    sub.expires_at = utcnow() + timedelta(days=30 + balance.FREEZE_DAYS)
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    result = balance.change_plan(db_session, sub, new_plan)
    db_session.commit()

    old_price = balance.plan_price_kopecks(old_plan)
    # Без капа было бы floor(10000 * 44/30) = 14666.
    assert result["refunded_kopecks"] == old_price

    refund_tx = (
        db_session.query(models.BalanceTransaction)
        .filter_by(user_id=user.id, kind=models.BalanceTxKind.refund)
        .one()
    )
    assert refund_tx.amount_kopecks == old_price
