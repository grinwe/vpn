"""User balance ledger and monthly billing (V2).

Simple model: on activation ``plan.price`` is debited from the wallet and
``expires_at`` is set to ``now + plan.duration_days``. A worker tick runs
hourly: when ``expires_at <= now`` and ``auto_renew`` is on, we try to
debit another month. If the wallet is short the sub is expired.

Each mutation is row-locked (``SELECT ... FOR UPDATE``) so concurrent
topups and charges can never race.

Freeze: 1 per calendar year, 7 days. ``expires_at += 7d`` so the user
doesn't lose paid time. Devices are revoked on freeze, re-provisioned on
unfreeze. Early unfreeze is allowed but blocks further freezes until the
next calendar year.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta

from prometheus_client import Counter, Gauge
from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────

FREEZE_DAYS = int(os.getenv("FREEZE_DAYS", "7"))
REFERRAL_BONUS_KOPECKS = int(os.getenv("REFERRAL_BONUS_KOPECKS", "5000"))
TRIAL_DURATION_DAYS = int(os.getenv("TRIAL_DURATION_DAYS", "30"))
TRIAL_EXPIRY_WARN_DAYS = int(os.getenv("TRIAL_EXPIRY_WARN_DAYS", "3"))
MIN_TOPUP_KOPECKS = int(os.getenv("MIN_TOPUP_KOPECKS", "10000"))

# Legacy tunables kept for import compatibility (unused in V2).
MAX_FREEZE_DAYS_PER_PERIOD = FREEZE_DAYS
FREEZE_YEAR_BUDGET_DAYS = FREEZE_DAYS
EXTRA_DEVICE_DAILY_KOPECKS = 0


# ── Metrics ──────────────────────────────────────────────────────────

CHARGES_TOTAL = Counter(
    "vpn_balance_charges_total",
    "Monthly renewal attempts on subscriptions",
    ["result"],  # success | insufficient
)
TOPUPS_TOTAL = Counter(
    "vpn_balance_topups_total",
    "Balance ledger entries written",
    ["kind"],  # topup | bonus | refund | adjust | spend
)
CHARGE_AMOUNT_TOTAL = Counter(
    "vpn_balance_charge_amount_kopecks_total",
    "Total kopecks spent across all successful charges",
)
SUBS_FROZEN = Gauge(
    "vpn_balance_subs_frozen",
    "Subscriptions currently in frozen state",
)


# ── Helpers ──────────────────────────────────────────────────────────

def _lock_user(db: Session, user_id: int) -> models.User:
    """``SELECT * FROM users WHERE id=? FOR UPDATE``."""
    user = (
        db.query(models.User)
        .filter(models.User.id == user_id)
        .with_for_update()
        .one_or_none()
    )
    if user is None:
        raise RuntimeError(f"user {user_id} not found")
    return user


def _record_tx(
    db: Session,
    user: models.User,
    *,
    amount_kopecks: int,
    kind: models.BalanceTxKind,
    reference: str | None,
    note: str | None = None,
) -> models.BalanceTransaction:
    """Append a signed ledger row and bump the cached balance."""
    user.balance_kopecks = (user.balance_kopecks or 0) + amount_kopecks
    tx = models.BalanceTransaction(
        user_id=user.id,
        amount_kopecks=amount_kopecks,
        kind=kind,
        reference=reference,
        note=note,
    )
    db.add(tx)
    db.flush()
    TOPUPS_TOTAL.labels(kind=kind.value).inc()
    return tx


def plan_price_kopecks(plan: models.Plan) -> int:
    """Total price of a plan in kopecks (int).

    ``Plan.price`` is Numeric(10,2) in rubles. Rounded to whole kopecks.
    """
    if plan.price is None:
        return 0
    return int(round(float(plan.price) * 100))


def min_topup_kopecks(db: Session) -> int:
    """Dynamic minimum topup = cheapest visible plan price."""
    cheapest = (
        db.query(models.Plan)
        .filter(models.Plan.is_visible.is_(True))
        .order_by(models.Plan.price.asc())
        .first()
    )
    if cheapest:
        price = plan_price_kopecks(cheapest)
        if price > 0:
            return price
    return MIN_TOPUP_KOPECKS


# ── Public API: topup / bonus / adjustment ───────────────────────────

def topup(
    db: Session,
    user_id: int,
    amount_kopecks: int,
    *,
    reference: str,
    note: str | None = None,
    kind: models.BalanceTxKind = models.BalanceTxKind.topup,
) -> models.BalanceTransaction:
    """Credit a user's balance."""
    if amount_kopecks <= 0:
        raise ValueError(f"topup amount must be positive, got {amount_kopecks}")
    user = _lock_user(db, user_id)
    tx = _record_tx(
        db, user,
        amount_kopecks=amount_kopecks,
        kind=kind,
        reference=reference,
        note=note,
    )
    logger.info(
        "balance.topup user=%s amount=%s kind=%s ref=%s -> balance=%s",
        user_id, amount_kopecks, kind.value, reference, user.balance_kopecks,
    )
    return tx


def referral_bonus(
    db: Session, user_id: int, *, reference: str
) -> models.BalanceTransaction:
    """Credit ``REFERRAL_BONUS_KOPECKS`` as a ``kind=bonus`` topup."""
    return topup(
        db, user_id, REFERRAL_BONUS_KOPECKS,
        reference=reference,
        kind=models.BalanceTxKind.bonus,
        note="referral bonus",
    )


def trial_bonus(
    db: Session, user_id: int, amount_kopecks: int, *, reference: str
) -> models.BalanceTransaction:
    """Credit a one-time trial bonus."""
    return topup(
        db, user_id, amount_kopecks,
        reference=reference,
        kind=models.BalanceTxKind.bonus,
        note="trial bonus",
    )


def adjustment(
    db: Session,
    user_id: int,
    amount_kopecks: int,
    *,
    reference: str,
    note: str | None = None,
) -> models.BalanceTransaction:
    """Signed ``kind=adjust`` ledger row. Accepts negatives (capped at balance)."""
    user = _lock_user(db, user_id)
    if amount_kopecks < 0:
        cap = -(user.balance_kopecks or 0)
        if amount_kopecks < cap:
            amount_kopecks = cap
    if amount_kopecks == 0:
        return models.BalanceTransaction(
            user_id=user.id, amount_kopecks=0,
            kind=models.BalanceTxKind.adjust, reference=reference, note=note,
        )
    tx = _record_tx(
        db, user,
        amount_kopecks=amount_kopecks,
        kind=models.BalanceTxKind.adjust,
        reference=reference,
        note=note,
    )
    logger.info(
        "balance.adjustment user=%s amount=%s ref=%s -> balance=%s",
        user_id, amount_kopecks, reference, user.balance_kopecks,
    )
    return tx


# ── Subscription billing (V2 monthly) ───────────────────────────────

def activate_subscription(
    db: Session,
    user_id: int,
    sub: models.Subscription,
    *,
    reference: str,
) -> int:
    """Debit ``plan.price`` from wallet, set ``expires_at``, ``auto_renew=True``.

    Called at subscription activation. Raises ``ValueError`` on insufficient
    balance (callers surface 402 with topup hint).
    """
    price = plan_price_kopecks(sub.plan)
    if price <= 0:
        raise ValueError(f"plan {sub.plan_id} has no price")

    user = _lock_user(db, user_id)
    if (user.balance_kopecks or 0) < price:
        raise ValueError(
            f"insufficient balance: need {price}, have {user.balance_kopecks or 0}"
        )

    _record_tx(
        db, user,
        amount_kopecks=-price,
        kind=models.BalanceTxKind.spend,
        reference=reference,
        note=f"activate {sub.plan.name} (sub {sub.id})",
    )

    sub.expires_at = utcnow() + timedelta(days=sub.plan.duration_days)
    sub.auto_renew = True
    # Clear V1 fields.
    sub.prepaid_kopecks = 0
    sub.next_charge_at = None
    db.add(sub)
    db.flush()

    CHARGES_TOTAL.labels(result="success").inc()
    CHARGE_AMOUNT_TOTAL.inc(price)
    logger.info(
        "balance.activate user=%s sub=%s price=%s expires=%s -> balance=%s",
        user_id, sub.id, price, sub.expires_at, user.balance_kopecks,
    )
    return price


def renew_subscription(db: Session, sub: models.Subscription) -> bool:
    """Try to renew a subscription for another billing period.

    Called by the worker tick when ``expires_at <= now`` and
    ``auto_renew = True``. Returns True on success, False on insufficient
    balance (caller should expire the sub).
    """
    if sub.plan is None:
        raise RuntimeError(f"subscription {sub.id} has no plan loaded")

    price = plan_price_kopecks(sub.plan)
    if price <= 0:
        logger.warning("renew: plan %s has no price, skipping sub %s", sub.plan_id, sub.id)
        return True  # free plan, nothing to charge

    user = _lock_user(db, sub.user_id)
    if (user.balance_kopecks or 0) < price:
        CHARGES_TOTAL.labels(result="insufficient").inc()
        logger.info(
            "balance.renew insufficient user=%s sub=%s price=%s balance=%s",
            user.id, sub.id, price, user.balance_kopecks,
        )
        return False

    _record_tx(
        db, user,
        amount_kopecks=-price,
        kind=models.BalanceTxKind.spend,
        reference=f"renew:{sub.id}",
        note=f"renew {sub.plan.name} (sub {sub.id})",
    )

    # Extend from the current expires_at (not from now) so we don't
    # shorten the period if the tick fires a few hours late.
    base = sub.expires_at or utcnow()
    sub.expires_at = base + timedelta(days=sub.plan.duration_days)
    db.add(sub)
    db.flush()

    CHARGES_TOTAL.labels(result="success").inc()
    CHARGE_AMOUNT_TOTAL.inc(price)
    logger.info(
        "balance.renew ok user=%s sub=%s price=%s -> expires=%s balance=%s",
        user.id, sub.id, price, sub.expires_at, user.balance_kopecks,
    )
    return True


def change_plan(
    db: Session,
    sub: models.Subscription,
    new_plan: models.Plan,
) -> dict:
    """Switch a subscription to a different plan with proration.

    Returns ``{"refunded_kopecks": int, "charged_kopecks": int}``.
    Raises ``ValueError`` on insufficient balance after proration refund.
    """
    now = utcnow()
    old_plan = sub.plan
    old_price = plan_price_kopecks(old_plan)
    new_price = plan_price_kopecks(new_plan)

    # Proration: refund remaining fraction of old plan.
    refund = 0
    if old_price > 0 and sub.expires_at and sub.expires_at > now:
        total_days = old_plan.duration_days
        remaining_seconds = (sub.expires_at - now).total_seconds()
        remaining_days = max(remaining_seconds / 86400, 0)
        refund = int(math.floor(old_price * remaining_days / total_days))

    user = _lock_user(db, sub.user_id)

    # Credit the refund.
    if refund > 0:
        _record_tx(
            db, user,
            amount_kopecks=refund,
            kind=models.BalanceTxKind.refund,
            reference=f"prorate:{sub.id}",
            note=f"prorate {old_plan.name} -> {new_plan.name}",
        )

    # Charge new plan.
    if (user.balance_kopecks or 0) < new_price:
        raise ValueError(
            f"insufficient balance after proration: "
            f"need {new_price}, have {user.balance_kopecks or 0} "
            f"(refunded {refund})"
        )

    _record_tx(
        db, user,
        amount_kopecks=-new_price,
        kind=models.BalanceTxKind.spend,
        reference=f"change_plan:{sub.id}",
        note=f"change to {new_plan.name} (sub {sub.id})",
    )

    sub.plan_id = new_plan.id
    sub.expires_at = now + timedelta(days=new_plan.duration_days)
    sub.auto_renew = True
    db.add(sub)
    db.flush()

    logger.info(
        "balance.change_plan user=%s sub=%s %s->%s refund=%s charge=%s -> balance=%s",
        sub.user_id, sub.id, old_plan.name, new_plan.name,
        refund, new_price, user.balance_kopecks,
    )
    return {"refunded_kopecks": refund, "charged_kopecks": new_price}


# ── Freeze / unfreeze (V2: 1 per year, 7 days) ──────────────────────

def freeze_subscription(db: Session, sub: models.Subscription) -> None:
    """Pause a sub for ``FREEZE_DAYS`` days.

    Rules:
      - Only active subs with auto_renew=True can freeze.
      - 1 freeze per calendar year (tracked via ``has_frozen_this_year``).
      - ``expires_at += FREEZE_DAYS`` so the user doesn't lose paid time.
      - Devices are physically revoked.
    """
    if sub.status != models.SubscriptionStatus.active:
        raise RuntimeError(
            f"subscription {sub.id} cannot be frozen from status={sub.status.value}"
        )
    if not sub.auto_renew:
        raise RuntimeError("cannot freeze a subscription with auto-renew off")

    now = utcnow()
    current_year = now.year

    # Reset flag if calendar year flipped.
    already_froze = sub.has_frozen_this_year or False
    if sub.frozen_year != current_year:
        already_froze = False

    if already_froze:
        raise RuntimeError(
            f"freeze rejected: already used freeze in {current_year}"
        )

    sub.status = models.SubscriptionStatus.frozen
    sub.frozen_at = now
    sub.frozen_until = now + timedelta(days=FREEZE_DAYS)
    sub.has_frozen_this_year = True
    sub.frozen_year = current_year
    # Extend paid period so user doesn't lose days.
    if sub.expires_at:
        sub.expires_at = sub.expires_at + timedelta(days=FREEZE_DAYS)
    sub.next_charge_at = None
    db.add(sub)
    db.flush()

    from .provisioning import ProvisioningOrchestrator

    orchestrator = ProvisioningOrchestrator(db)
    for device in list(sub.devices):
        if device.status in (
            models.DeviceStatus.revoked,
            models.DeviceStatus.disabled,
        ):
            continue
        try:
            orchestrator.revoke_device(
                device, reason=f"freeze sub {sub.id}", background=True
            )
        except Exception:
            logger.exception(
                "freeze: revoke_device failed for device=%s sub=%s",
                device.id, sub.id,
            )

    SUBS_FROZEN.inc()
    logger.info(
        "balance.freeze sub=%s user=%s until=%s expires=%s",
        sub.id, sub.user_id, sub.frozen_until, sub.expires_at,
    )


def unfreeze_subscription(
    db: Session, sub: models.Subscription, *, auto: bool = False
) -> None:
    """Resume a frozen sub.

    Early unfreeze is allowed, but ``has_frozen_this_year`` stays True —
    no more freezes until next calendar year. Devices are re-provisioned.
    """
    if sub.status != models.SubscriptionStatus.frozen:
        raise RuntimeError(
            f"subscription {sub.id} is not frozen (status={sub.status.value})"
        )

    sub.frozen_at = None
    sub.frozen_until = None
    sub.status = models.SubscriptionStatus.active
    db.add(sub)
    db.flush()

    from .provisioning import ProvisioningOrchestrator

    orchestrator = ProvisioningOrchestrator(db)
    try:
        orchestrator.reprovision_subscription(sub)
    except Exception:
        logger.exception(
            "unfreeze: reprovision failed sub=%s — leaving active with no device",
            sub.id,
        )

    SUBS_FROZEN.dec()
    logger.info(
        "balance.unfreeze sub=%s user=%s auto=%s",
        sub.id, sub.user_id, auto,
    )


# ── Legacy compat stubs ─────────────────────────────────────────────
# These are referenced by old code paths; they're no-ops or thin
# wrappers now.

def activate_prepaid(
    db: Session, user_id: int, sub: models.Subscription, *, reference: str,
) -> int:
    """V1 compat — redirects to V2 activate_subscription."""
    return activate_subscription(db, user_id, sub, reference=reference)


def charge_subscription(db: Session, sub: models.Subscription) -> bool:
    """V1 compat — no-op in V2 (monthly billing via renew_subscription)."""
    return True


def refund_prepaid(
    db: Session, sub: models.Subscription, *, reference: str, note: str | None = None,
) -> int:
    """V1 compat — no-op in V2 (prepaid_kopecks is unused)."""
    return 0


def sub_days_remaining(
    sub: models.Subscription, devices: int, wallet_kopecks: int = 0,
) -> int:
    """V1 compat — returns days until expires_at."""
    if not sub.expires_at:
        return 0
    remaining = (sub.expires_at - utcnow()).total_seconds()
    return max(int(remaining // 86400), 0)


def days_remaining(user: models.User, plan: models.Plan, devices: int) -> int:
    """V1 compat — how many months this balance can buy."""
    price = plan_price_kopecks(plan)
    if price <= 0:
        return 0
    months = (user.balance_kopecks or 0) // price
    return months * (plan.duration_days or 30)


def _daily_cost_kopecks(plan: models.Plan, device_count: int) -> int:
    """V1 compat stub — returns monthly price / 30 for display."""
    price = plan_price_kopecks(plan)
    return max(price // 30, 1) if price > 0 else 0


def _live_device_count(db: Session, subscription_id: int) -> int:
    """Count live devices for a subscription."""
    return (
        db.query(models.Device)
        .filter(
            models.Device.subscription_id == subscription_id,
            models.Device.status.notin_(
                [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
            ),
        )
        .count()
    )
