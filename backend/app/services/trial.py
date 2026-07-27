"""Free-trial activation service.

Shared by ``POST /api/trial/activate`` (admin-scoped, called from the
bot) and the WebApp wrapper that authenticates via Telegram initData.
Both paths need the same logic: gate on ``User.trial_activated_at IS
NULL``, row-lock to block concurrent activations, credit a bonus sized
to the cheapest 30-day plan, and — if the user was referred — credit
the second ``REFERRAL_BONUS_KOPECKS`` on top. Keeping this in one
function means the two HTTP entry points can't drift.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from . import balance as balance_svc

logger = logging.getLogger(__name__)


class TrialAlreadyActivated(Exception):
    """Raised when ``trial_activated_at`` is already set. Maps to 409."""


class NoTrialPlan(Exception):
    """Raised when no visible 30-day plan exists in the DB. Maps to 503."""


@dataclass
class TrialActivationResult:
    trial_amount_kopecks: int
    referral_bonus_kopecks: int
    balance_kopecks: int
    trial_expires_at: datetime


def _trial_plan(db: Session) -> models.Plan | None:
    """Cheapest visible 30-day plan — that's what the trial mirrors.

    We look this up instead of hardcoding 15000 kopecks so if the
    operator changes Basic 1m pricing in the DB, new trials auto-track
    without a code deploy.
    """
    return (
        db.query(models.Plan)
        .filter(
            models.Plan.duration_days == 30,
            models.Plan.is_visible.is_(True),
        )
        .order_by(models.Plan.price.asc())
        .first()
    )


def trial_amount_kopecks(db: Session) -> int:
    """Public: the amount a fresh trial would credit right now.

    WebApp /me calls this to render the banner label. Returns 0 if no
    visible 30-day plan exists — the banner won't render in that case.
    """
    plan = _trial_plan(db)
    if plan is None:
        return 0
    return int(round(float(plan.price) * 100))


def activate_trial(db: Session, user_id: int) -> TrialActivationResult:
    """Atomically grant the one-time trial bonus to ``user_id``.

    Caller owns the outer commit — we flush the inserts but leave the
    transaction open so the HTTP layer can rollback on any later
    error. Raises :class:`TrialAlreadyActivated` if the user already
    claimed it (or raced another request to the lock), and
    :class:`NoTrialPlan` if the Plan table has no visible 30-day row.
    """
    # Row-lock first, then re-check. Two webapp taps in the same
    # second both see trial_activated_at=NULL on a stale read; only
    # the one that wins the lock gets past this guard.
    user = (
        db.query(models.User)
        .filter_by(id=user_id)
        .with_for_update()
        .one_or_none()
    )
    if user is None:
        raise ValueError(f"user {user_id} not found")
    if user.trial_activated_at is not None:
        raise TrialAlreadyActivated()

    plan = _trial_plan(db)
    if plan is None:
        raise NoTrialPlan()
    amount = int(round(float(plan.price) * 100))

    now = datetime.utcnow()
    user.trial_activated_at = now
    user.trial_expires_at = now + timedelta(days=balance_svc.TRIAL_DURATION_DAYS)

    balance_svc.trial_bonus(db, user.id, amount, reference=f"trial:{user.id}")

    ref_bonus = 0
    if user.referred_by_id is not None:
        # Приглашённому — подарок в днях от кода, по которому он пришёл
        # (bonus_days), иначе общий дефолт. Реферер за это НЕ получает ничего:
        # его награда привязана к первому платежу приглашённого
        # (api/invoices.py), иначе рефералка вырождается в фарм триалов.
        ref_code = (
            db.query(models.ReferralCode)
            .filter_by(owner_id=user.referred_by_id)
            .order_by(models.ReferralCode.id.desc())
            .first()
        )
        invitee_days = (
            ref_code.bonus_days
            if ref_code and ref_code.bonus_days
            else balance_svc.REFERRAL_INVITEE_DAYS
        )
        try:
            tx = balance_svc.referral_bonus(
                db,
                user.id,
                reference=f"referral_signup:{user.id}",
                days=invitee_days,
                note=f"referral welcome: {invitee_days}d",
            )
            ref_bonus = tx.amount_kopecks
        except ValueError:
            # Награда не может быть нулевой ни при каких настройках (без прайса
            # есть фолбэк на легаси-сумму), так что сюда попадём только если
            # обнулили и её. Триал в любом случае выдаём — подарок приглашённому
            # это приятный довесок, а не условие.
            logger.warning("trial.activate: реферальный подарок обнулён настройками")

    logger.info(
        "trial.activate user=%s amount=%s ref_bonus=%s expires=%s",
        user.id, amount, ref_bonus, user.trial_expires_at,
    )
    return TrialActivationResult(
        trial_amount_kopecks=amount,
        referral_bonus_kopecks=ref_bonus,
        balance_kopecks=user.balance_kopecks,
        trial_expires_at=user.trial_expires_at,
    )
