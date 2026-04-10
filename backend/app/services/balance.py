"""User balance ledger and pay-as-you-go billing (stage 4).

The hitvpn-style billing model: each subscription has a per-day rate
(``Plan.daily_rate_kopecks``) and an anchor (``Subscription.next_charge_at``).
A worker tick wakes up every ``BALANCE_CHARGE_INTERVAL`` seconds, finds
subs whose anchor has elapsed, and decrements ``User.balance_kopecks``
by ``daily_rate × live_device_count``. Each mutation is row-locked
(``SELECT ... FOR UPDATE``) so concurrent topups and charges can never
race even when the WebApp, the bot, and a payment webhook all hit the
same user inside the same second.

Every change appends to :class:`BalanceTransaction` (signed amounts,
positive for top-ups, negative for spend). The current cached value on
``User.balance_kopecks`` must always equal ``SUM(amount_kopecks)`` over
that user's rows; we trust the cached column for reads but reconcile
nightly from the ledger if anything ever drifts.

Freeze is a *pause-only* model: while frozen, no charge ticks fire and
the user's devices are physically revoked from the node so the slot is
freed for someone else. The freeze window is bounded by
``MAX_FREEZE_DAYS_PER_PERIOD`` per call and ``FREEZE_YEAR_BUDGET_DAYS``
per calendar year (tracked on the subscription itself). Auto-unfreeze
fires when ``frozen_until`` lapses.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from prometheus_client import Counter, Gauge
from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────

# How many days a single freeze call locks. The user can call freeze
# again after unfreeze, but only as long as ``frozen_days_used`` for the
# current calendar year hasn't hit the year budget below.
MAX_FREEZE_DAYS_PER_PERIOD = int(os.getenv("MAX_FREEZE_DAYS_PER_PERIOD", "7"))
# Hard cap on total frozen days per calendar year per subscription.
# Resets when the calendar year flips (tracked via ``frozen_year``).
FREEZE_YEAR_BUDGET_DAYS = int(os.getenv("FREEZE_YEAR_BUDGET_DAYS", "30"))
# Bonus credited to *both* sides of a referral on first activation.
# Applied as a ``kind=bonus`` ledger entry — it spends like real money.
REFERRAL_BONUS_KOPECKS = int(os.getenv("REFERRAL_BONUS_KOPECKS", "5000"))
# Free-trial window. The trial itself is just a ``kind=bonus`` credit
# sized to the cheapest 30-day plan (looked up at activation time from
# the Plan table, not hardcoded). These two knobs only control the
# expiry timing: when the 3-day warning fires, and when the worker
# tries to claw back the unspent portion if the user never made a real
# ``kind=topup``. Raising TRIAL_DURATION_DAYS does NOT re-extend
# already-activated trials — it only affects new activations.
TRIAL_DURATION_DAYS = int(os.getenv("TRIAL_DURATION_DAYS", "30"))
TRIAL_EXPIRY_WARN_DAYS = int(os.getenv("TRIAL_EXPIRY_WARN_DAYS", "3"))
# Floor for any topup invoice. Stops users from creating dust invoices
# that the payment provider would refuse anyway, and keeps the ledger
# noise-free.
MIN_TOPUP_KOPECKS = int(os.getenv("MIN_TOPUP_KOPECKS", "10000"))
# Surcharge per device above the plan's bundled ``max_devices``. The
# plan's daily_rate covers everything inside the bundle (Family @ 10₽/day
# is the total for up to 3 devices, not per-device). Going over the cap
# adds this much per extra device per day. Default 333 коп/день ≈ 100₽/мес,
# matching the hitvpn-style "+100₽/mo per extra device" rule.
EXTRA_DEVICE_KOPECKS_PER_MONTH = int(os.getenv("EXTRA_DEVICE_KOPECKS_PER_MONTH", "10000"))
EXTRA_DEVICE_DAILY_KOPECKS = max(EXTRA_DEVICE_KOPECKS_PER_MONTH // 30, 1)


# ── Metrics ──────────────────────────────────────────────────────────

CHARGES_TOTAL = Counter(
    "vpn_balance_charges_total",
    "Daily-billing charge attempts on subscriptions",
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

def _live_device_count(db: Session, subscription_id: int) -> int:
    """Count devices still consuming a node slot for this subscription.

    Frozen subs have zero by construction (freeze revokes all of them);
    a fresh sub typically has 1. Used for ``cost = daily_rate × N``.
    """
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


def _daily_cost_kopecks(plan: models.Plan, device_count: int) -> int:
    """Plan-bundle pricing: ``base + extras_over_cap × EXTRA_DEVICE_DAILY``.

    The plan's ``daily_rate_kopecks`` is a *flat* rate that covers every
    device up to ``plan.max_devices`` (Family @ 10₽/day buys the whole
    3-device bundle, not per-device). Going over the cap adds
    ``EXTRA_DEVICE_DAILY_KOPECKS`` per extra device per day — the
    hitvpn-style "+100₽/mo per extra slot" rule.

    Subs with zero live devices still cost the base rate so a user can't
    let everything fall to ``disabled`` after a freeze and ride the sub
    for free. We never go negative on the extras: ``max(0, ...)``.
    """
    rate = plan.daily_rate_kopecks
    if rate is None or rate <= 0:
        # Legacy invoice-priced plan with no daily rate. Caller should
        # not be calling charge_subscription on these — guard anyway.
        raise RuntimeError(
            f"plan {plan.id} has no daily_rate_kopecks; cannot use balance billing"
        )
    bundled = plan.max_devices or 1
    extras = max(0, device_count - bundled)
    return rate + extras * EXTRA_DEVICE_DAILY_KOPECKS


def _lock_user(db: Session, user_id: int) -> models.User:
    """``SELECT * FROM users WHERE id=? FOR UPDATE``.

    All balance mutations go through this so a single user row is the
    serialization point — concurrent topups, charges and refunds queue
    up instead of clobbering each other. Caller must already be inside
    a transaction.
    """
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
    """Append a signed ledger row and bump the cached balance.

    The cached column is the source of truth for reads; the ledger is
    the source of truth for audits and the nightly reconciliation job.
    Both must move together — never write one without the other.
    """
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


# ── Public API ───────────────────────────────────────────────────────

def topup(
    db: Session,
    user_id: int,
    amount_kopecks: int,
    *,
    reference: str,
    note: str | None = None,
    kind: models.BalanceTxKind = models.BalanceTxKind.topup,
) -> models.BalanceTransaction:
    """Credit a user's balance.

    Used by:
      - the payment-paid hook for ``Invoice.kind='topup'`` (kind=topup),
      - referral activation (kind=bonus),
      - admin adjustments via the panel (kind=adjust),
      - refunds (kind=refund).

    ``amount_kopecks`` must be positive — refund-as-negative is *not*
    valid here, the ledger encodes direction via ``kind`` plus the sign
    written in ``_record_tx``. For refunds we still pass a positive
    amount; the column stays signed so a future "negative refund" rule
    can be added without a schema change.

    Caller is responsible for the outer commit. We only ``flush()`` so
    the row lock is released by the parent transaction at the right
    time.
    """
    if amount_kopecks <= 0:
        raise ValueError(f"topup amount must be positive, got {amount_kopecks}")

    user = _lock_user(db, user_id)
    tx = _record_tx(
        db,
        user,
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


def charge_subscription(db: Session, sub: models.Subscription) -> bool:
    """Charge one day of usage for a single subscription.

    Returns ``True`` on a successful debit, ``False`` if the user can't
    afford even one more day. On insufficient balance the caller should
    flip ``sub.status = expired``; we don't do it here because the
    expired-handling path also needs to revoke the device, which is the
    worker's job, not balance.py's.

    Atomic against:
      - other charges on the same user (different subs) — they queue
        on the user row lock,
      - concurrent topups — same row lock,
      - device adds/removes mid-charge — we read the device count
        *after* taking the lock, so a topup that lands between the
        check and the debit can't undercount.

    Anchor advancement: on success ``next_charge_at += 24h``. We use
    a fixed delta (not ``now``) so a tick that fires 5 minutes late
    doesn't permanently shift the user's billing window — they always
    get exactly 24h windows from their original activation moment.
    """
    if sub.status != models.SubscriptionStatus.active:
        # Defensive — the worker SELECT filters on status='active', but
        # a tick could race with a freeze. Skip silently.
        return True
    if sub.plan is None:
        raise RuntimeError(f"subscription {sub.id} has no plan loaded")

    # Row-lock the user for the ledger row, but the actual spend comes
    # from sub.prepaid_kopecks — a committed bucket that was debited
    # from the wallet at purchase time. The wallet column is untouched
    # on daily charges; it only moves on topup/refund/adjust.
    user = _lock_user(db, sub.user_id)
    devices = _live_device_count(db, sub.id)
    cost = _daily_cost_kopecks(sub.plan, devices)

    prepaid = sub.prepaid_kopecks or 0
    wallet = user.balance_kopecks or 0
    if prepaid + wallet < cost:
        CHARGES_TOTAL.labels(result="insufficient").inc()
        logger.info(
            "balance.charge insufficient user=%s sub=%s cost=%s prepaid=%s wallet=%s",
            user.id, sub.id, cost, prepaid, wallet,
        )
        return False

    today = utcnow().date().isoformat()
    bundled = sub.plan.max_devices or 1
    extras = max(0, devices - bundled)
    note = f"daily charge ({devices} device{'s' if devices != 1 else ''})"
    if extras > 0:
        note += f", +{extras} over plan"

    # Draw from the prepaid bucket first (no ledger row — that spend was
    # already booked at activate_prepaid time). When the bucket is empty
    # or short, top up the remainder from the wallet as a fresh kind=spend
    # ledger row so reconciliation stays honest.
    from_prepaid = min(prepaid, cost)
    from_wallet = cost - from_prepaid
    sub.prepaid_kopecks = prepaid - from_prepaid
    if from_wallet > 0:
        _record_tx(
            db, user,
            amount_kopecks=-from_wallet,
            kind=models.BalanceTxKind.spend,
            reference=f"daily:{sub.id}:{today}",
            note=f"{note} (wallet fallback)",
        )
    logger.debug(
        "balance.charge draw sub=%s cost=%s prepaid=%s wallet=%s %s",
        sub.id, cost, from_prepaid, from_wallet, note,
    )

    # Anchor moves by exactly 24h regardless of when this tick fired.
    # NULL anchor (legacy sub) shouldn't get here, but be safe.
    base = sub.next_charge_at or utcnow()
    sub.next_charge_at = base + timedelta(hours=24)
    db.add(sub)

    CHARGES_TOTAL.labels(result="success").inc()
    CHARGE_AMOUNT_TOTAL.inc(cost)
    logger.info(
        "balance.charge ok user=%s sub=%s cost=%s -> balance=%s next=%s",
        user.id, sub.id, cost, user.balance_kopecks, sub.next_charge_at,
    )
    return True


def freeze_subscription(db: Session, sub: models.Subscription) -> None:
    """Pause a sub: stop billing, physically revoke devices.

    Validates the year budget *before* mutating anything so a rejected
    freeze leaves the sub exactly as it was. After freeze:
      - ``status = frozen``
      - ``frozen_at = now``
      - ``frozen_until = now + MAX_FREEZE_DAYS_PER_PERIOD days``
      - all live devices are revoked (releases the node slot)
      - the charge tick skips this sub until unfreeze

    Year budget tracking is per-subscription, not per-user — that
    matches how subs are scoped (different plans = different freeze
    pools) and avoids cross-sub gaming.
    """
    if sub.status not in (models.SubscriptionStatus.active,):
        raise RuntimeError(
            f"subscription {sub.id} cannot be frozen from status={sub.status.value}"
        )

    now = utcnow()
    current_year = now.year

    # Reset year budget if the calendar flipped since the last freeze.
    used = sub.frozen_days_used or 0
    if sub.frozen_year != current_year:
        used = 0

    if used + MAX_FREEZE_DAYS_PER_PERIOD > FREEZE_YEAR_BUDGET_DAYS:
        remaining = max(FREEZE_YEAR_BUDGET_DAYS - used, 0)
        raise RuntimeError(
            f"freeze rejected: only {remaining} days left in {current_year} budget "
            f"(would consume {MAX_FREEZE_DAYS_PER_PERIOD})"
        )

    sub.status = models.SubscriptionStatus.frozen
    sub.frozen_at = now
    sub.frozen_until = now + timedelta(days=MAX_FREEZE_DAYS_PER_PERIOD)
    sub.frozen_days_used = used  # ensure year-rollover reset is persisted
    sub.frozen_year = current_year
    # Pause billing while frozen — unfreeze sets a fresh anchor.
    sub.next_charge_at = None
    db.add(sub)
    db.flush()

    # Revoke devices on the node. We import here to avoid a circular
    # import (provisioning.py imports warm_pool which imports models).
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
        "balance.freeze sub=%s user=%s until=%s used_after=%s/%s",
        sub.id, sub.user_id, sub.frozen_until, used, FREEZE_YEAR_BUDGET_DAYS,
    )


def unfreeze_subscription(
    db: Session, sub: models.Subscription, *, auto: bool = False
) -> None:
    """Resume a frozen sub.

    Adds the elapsed freeze duration to ``frozen_days_used`` (so a user
    that unfreezes early gets the unused days back into their year
    budget) and re-provisions one device via the warm pool. The
    original ``sub_token`` is preserved so the user's installed clients
    keep working without reconfiguration.

    ``auto=True`` is set by the worker tick when ``frozen_until`` lapsed
    on its own — purely an audit-log marker, the behavior is identical.
    """
    if sub.status != models.SubscriptionStatus.frozen:
        raise RuntimeError(
            f"subscription {sub.id} is not frozen (status={sub.status.value})"
        )

    now = utcnow()
    elapsed_days = 0
    if sub.frozen_at is not None:
        # Round up so an early unfreeze still costs the user at least
        # one day from their budget — otherwise rapid freeze/unfreeze
        # cycles cost nothing.
        delta = now - sub.frozen_at
        elapsed_days = max(int(delta.total_seconds() // 86400), 1) if delta.total_seconds() > 0 else 0

    sub.frozen_days_used = (sub.frozen_days_used or 0) + elapsed_days
    sub.frozen_at = None
    sub.frozen_until = None
    sub.status = models.SubscriptionStatus.active
    # Anchor restarts now — user gets a fresh 24h window from unfreeze.
    sub.next_charge_at = now
    db.add(sub)
    db.flush()

    from .provisioning import ProvisioningOrchestrator

    orchestrator = ProvisioningOrchestrator(db)
    try:
        orchestrator.reprovision_subscription(sub)
    except Exception:
        logger.exception(
            "unfreeze: reprovision failed sub=%s — leaving sub active with no device",
            sub.id,
        )
        # Don't roll back the unfreeze: user is unblocked, the worker
        # can retry device creation, and they can manually re-add via
        # the WebApp. Same failure mode as a cold provision miss.

    SUBS_FROZEN.dec()
    logger.info(
        "balance.unfreeze sub=%s user=%s auto=%s elapsed=%s used_after=%s",
        sub.id, sub.user_id, auto, elapsed_days, sub.frozen_days_used,
    )


def referral_bonus(
    db: Session, user_id: int, *, reference: str
) -> models.BalanceTransaction:
    """Credit ``REFERRAL_BONUS_KOPECKS`` as a ``kind=bonus`` topup.

    Bonus money is fungible with regular topups — it spends the same
    way through ``charge_subscription``. The only difference is the
    ``kind`` tag, which lets the panel split "real revenue" from
    "referral cost" in reports without a separate sub-ledger.
    """
    return topup(
        db,
        user_id,
        REFERRAL_BONUS_KOPECKS,
        reference=reference,
        kind=models.BalanceTxKind.bonus,
        note="referral bonus",
    )


def trial_bonus(
    db: Session, user_id: int, amount_kopecks: int, *, reference: str
) -> models.BalanceTransaction:
    """Credit a one-time trial bonus as a ``kind=bonus`` topup.

    Amount comes from the caller (looked up from the cheapest 30-day
    plan at activation time) — keeping it an argument rather than a
    module constant means the trial value auto-tracks price changes
    without a code deploy. Same fungibility rules as ``referral_bonus``.
    """
    return topup(
        db,
        user_id,
        amount_kopecks,
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
    """Write a signed ``kind=adjust`` ledger row.

    Unlike :func:`topup`, this accepts *negative* amounts — the only
    sanctioned way to decrement the cached balance outside of
    ``charge_subscription``. Used by the trial-expiry clawback in the
    worker tick to zero out an unspent trial bonus.

    The caller owns the outer transaction; we row-lock the user, flush,
    and return without committing. Floors the effective decrement at
    the user's current balance so the adjustment can never push them
    below zero (``min(|amount|, balance)`` for negatives).
    """
    user = _lock_user(db, user_id)
    if amount_kopecks < 0:
        # Cap clawback at the live balance so we never go negative.
        cap = -(user.balance_kopecks or 0)
        if amount_kopecks < cap:
            amount_kopecks = cap
    if amount_kopecks == 0:
        return models.BalanceTransaction(
            user_id=user.id, amount_kopecks=0,
            kind=models.BalanceTxKind.adjust, reference=reference, note=note,
        )
    tx = _record_tx(
        db,
        user,
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


def plan_price_kopecks(plan: models.Plan) -> int:
    """Total price of a plan in kopecks (int).

    ``Plan.price`` is Numeric(10,2) in rubles — canonical source for the
    upfront activation charge. Rounded to whole kopecks to dodge
    floating-point dust in the ledger.
    """
    if plan.price is None:
        return 0
    return int(round(float(plan.price) * 100))


def activate_prepaid(
    db: Session,
    user_id: int,
    sub: models.Subscription,
    *,
    reference: str,
) -> int:
    """Move the full plan price from wallet into ``sub.prepaid_kopecks``.

    Called at subscription activation. Raises ``ValueError`` if the
    wallet can't cover the full plan price — callers should surface a
    402 with the topup shortfall. Atomic under the user row lock so a
    concurrent charge can't race us into negative territory.

    Writes a single ``kind=spend`` ledger row for -plan_price so the
    wallet reconciles and the user sees «потрачено N₽ на активацию
    подписки» in their ledger. sub.prepaid_kopecks then becomes the
    bucket that ``charge_subscription`` draws down day-by-day.
    """
    price = plan_price_kopecks(sub.plan)
    if price <= 0:
        raise ValueError(f"plan {sub.plan_id} has no price; cannot prepay")

    user = _lock_user(db, user_id)
    if (user.balance_kopecks or 0) < price:
        raise ValueError(
            f"insufficient balance: need {price}, have {user.balance_kopecks or 0}"
        )

    _record_tx(
        db,
        user,
        amount_kopecks=-price,
        kind=models.BalanceTxKind.spend,
        reference=reference,
        note=f"prepay plan {sub.plan.name} (sub {sub.id})",
    )
    sub.prepaid_kopecks = (sub.prepaid_kopecks or 0) + price
    db.add(sub)
    db.flush()
    logger.info(
        "balance.activate_prepaid user=%s sub=%s amount=%s -> balance=%s prepaid=%s",
        user_id, sub.id, price, user.balance_kopecks, sub.prepaid_kopecks,
    )
    return price


def refund_prepaid(
    db: Session,
    sub: models.Subscription,
    *,
    reference: str,
    note: str | None = None,
) -> int:
    """Move ``sub.prepaid_kopecks`` remainder back to the user wallet.

    Used on manual revoke/enable flows where the user is giving up the
    subscription early. Not called on expired subs — by definition they
    burned through the bucket, so there should be nothing to refund
    (and if there is due to an off-by-one, we skip it).

    Returns the kopecks actually refunded (0 if nothing). Writes a
    ``kind=refund`` ledger row and zeroes the bucket.
    """
    remainder = sub.prepaid_kopecks or 0
    if remainder <= 0:
        return 0

    user = _lock_user(db, sub.user_id)
    _record_tx(
        db,
        user,
        amount_kopecks=remainder,
        kind=models.BalanceTxKind.refund,
        reference=reference,
        note=note or f"refund unused prepaid (sub {sub.id})",
    )
    sub.prepaid_kopecks = 0
    db.add(sub)
    db.flush()
    logger.info(
        "balance.refund_prepaid user=%s sub=%s amount=%s -> balance=%s",
        sub.user_id, sub.id, remainder, user.balance_kopecks,
    )
    return remainder


def sub_days_remaining(
    sub: models.Subscription,
    devices: int,
    wallet_kopecks: int = 0,
) -> int:
    """How many more daily charges the sub can survive.

    Counts ``sub.prepaid_kopecks`` plus an optional ``wallet_kopecks``
    fallback — ``charge_subscription`` draws from the wallet after the
    prepaid bucket is empty, so the UI should reflect the same runway.

    Pure read — no locks. Returns 0 if the combined pot can't cover one
    more day (the tick will expire the sub on its next run).

    Note on multi-sub users: when one wallet funds several subs the
    caller is responsible for splitting ``wallet_kopecks`` across them
    (or passing 0) to avoid double-counting. For the common 1-sub case
    just pass the full wallet.
    """
    if sub.plan is None:
        return 0
    try:
        cost = _daily_cost_kopecks(sub.plan, devices)
    except RuntimeError:
        return 0
    if cost <= 0:
        return 0
    pot = (sub.prepaid_kopecks or 0) + max(0, wallet_kopecks)
    return max(pot // cost, 0)


def days_remaining(user: models.User, plan: models.Plan, devices: int) -> int:
    """How many more daily charges this user can afford on this plan.

    Pure read — no DB writes, no locks. Used by the WebApp header to
    show «хватит на N дней» under the balance. Returns 0 if the user
    can't afford even one more day.
    """
    try:
        cost = _daily_cost_kopecks(plan, devices)
    except RuntimeError:
        return 0
    if cost <= 0:
        return 0
    return max((user.balance_kopecks or 0) // cost, 0)
