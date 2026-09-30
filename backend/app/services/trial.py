"""Бесплатные дни (триал): бонус и подписка на него.

Бонус = ``TRIAL_DURATION_DAYS`` дней по цене дня самого дешёвого видимого
30-дневного плана (Solo: 3 дня = 15 ₽). Приглашённому сверху зачисляется
подарок в днях из кода реферера (3 + 3 = 6 дней). Бонус сразу тратится на
подписку ровно на эти дни (плюс ``TRIAL_HIDDEN_HOURS`` скрытых часов, за которые
не списываем): :func:`activate_trial_full`. Дальше подписка продлевается с
баланса штатным ``renew_subscription``.

Точки входа:
  * :func:`activate_trial_full` — бот (``POST /api/trial/activate_full``) и
    кабинет без живой подписки. Одна функция, чтобы пути не разъехались.
  * :func:`activate_trial` — только бонус: кабинет при живой подписке
    (``set_expiry=False``, деньги пойдут на продление) и legacy
    ``POST /api/trial/activate``.

Обе функции оставляют транзакцию открытой: commit и аудит у вызывающего,
чтобы на любой ошибке HTTP-слой откатил бонус целиком.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow
from . import balance as balance_svc

logger = logging.getLogger(__name__)


class TrialAlreadyActivated(Exception):
    """Raised when ``trial_activated_at`` is already set. Maps to 409."""


class NoTrialPlan(Exception):
    """Raised when no visible 30-day plan exists in the DB. Maps to 503."""


class TrialLiveSubscription(Exception):
    """У юзера уже есть active/frozen подписка. API отвечает 409."""


class TrialAlreadyUsed(Exception):
    """Бесплатные дни уже были: триал активирован, и подписка у юзера была.

    Без этой защиты старая кнопка в чате давала истёкшему триальщику,
    пополнившему баланс, новую подписку мимо продления. API отвечает 409.
    """


@dataclass
class TrialActivationResult:
    trial_amount_kopecks: int
    referral_bonus_kopecks: int
    balance_kopecks: int
    # NULL у бонус-онли (``set_expiry=False``): возвращать нечего, таймер не нужен.
    trial_expires_at: datetime | None
    trial_days: int = 0
    # Дни подарка приглашённому; 0, если подарок не зачислен.
    referral_days: int = 0


@dataclass
class TrialFullResult:
    sub: models.Subscription
    plan: models.Plan
    # None на историческом пути (бонус 150 ₽ старого триала потрачен на месяц):
    # бот тогда пишет «Подписка активирована» без числа дней.
    trial_days: int | None
    referral_days: int | None
    hidden_hours: int | None
    charged_kopecks: int
    # True — бонус зачислен этим же вызовом; False — доделали выданный раньше.
    fresh: bool


def _trial_plan(db: Session) -> models.Plan | None:
    """Cheapest visible 30-day plan — that's what the trial mirrors.

    По нему считается цена дня для бонуса (``days_to_kopecks``), и на него же
    оформляется триальная подписка. Так списание и зачисление берутся из
    одного места и следуют за прайсом без деплоя.
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
    """Сколько зачислит свежий триал: ``TRIAL_DURATION_DAYS`` дней по цене дня.

    Кабинет использует значение только как условие «> 0» для баннера.
    0, если нет видимого 30-дневного плана: оценить день нечем.
    """
    if _trial_plan(db) is None:
        return 0
    return balance_svc.days_to_kopecks(db, balance_svc.TRIAL_DURATION_DAYS)


def invitee_bonus_days(db: Session, user: models.User) -> int:
    """Дни подарка приглашённому: из кода реферера, иначе общий дефолт.

    0, если юзера никто не приглашал.
    """
    if user.referred_by_id is None:
        return 0
    ref_code = (
        db.query(models.ReferralCode)
        .filter_by(owner_id=user.referred_by_id)
        .order_by(models.ReferralCode.id.desc())
        .first()
    )
    if ref_code and ref_code.bonus_days:
        return int(ref_code.bonus_days)
    return balance_svc.REFERRAL_INVITEE_DAYS


def trial_days_for(db: Session, user: models.User) -> tuple[int, int]:
    """``(бесплатные дни, дни по приглашению)`` — для текстов /register и /me.

    Считается теми же функциями, что и сама активация, поэтому цифра в
    тексте совпадёт с выданной.
    """
    return balance_svc.TRIAL_DURATION_DAYS, invitee_bonus_days(db, user)


def _trial_expiry(now: datetime, days: int) -> datetime:
    """Конец доступа: видимые дни плюс скрытые часы (за них не списываем)."""
    return now + timedelta(days=days, hours=balance_svc.TRIAL_HIDDEN_HOURS)


def activate_trial(
    db: Session, user_id: int, *, set_expiry: bool = True
) -> TrialActivationResult:
    """Atomically grant the one-time trial bonus to ``user_id``.

    Caller owns the outer commit — we flush the inserts but leave the
    transaction open so the HTTP layer can rollback on any later
    error. Raises :class:`TrialAlreadyActivated` if the user already
    claimed it (or raced another request to the lock), and
    :class:`NoTrialPlan` if the Plan table has no visible 30-day row.

    ``set_expiry=False`` — только бонус при живой подписке (кабинет):
    ``trial_expires_at`` остаётся NULL, поэтому ни предупреждения «пополни
    баланс», ни возврата бонуса у такого юзера не будет. Деньги просто
    пойдут на продление.
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

    if _trial_plan(db) is None:
        raise NoTrialPlan()
    trial_days = balance_svc.TRIAL_DURATION_DAYS
    amount = trial_amount_kopecks(db)

    now = utcnow()
    user.trial_activated_at = now

    balance_svc.trial_bonus(db, user.id, amount, reference=f"trial:{user.id}")

    ref_bonus = 0
    ref_days = 0
    if user.referred_by_id is not None:
        # Приглашённому — подарок в днях от кода, по которому он пришёл
        # (bonus_days), иначе общий дефолт. Дни прибавляются к триалу (3 + 3).
        # Реферер за это НЕ получает ничего: его награда привязана к первой
        # оплате приглашённого (api/invoices.py), иначе рефералка вырождается
        # в фарм триалов.
        invitee_days = invitee_bonus_days(db, user)
        try:
            tx = balance_svc.referral_bonus(
                db,
                user.id,
                reference=f"referral_signup:{user.id}",
                days=invitee_days,
                note=f"referral welcome: {invitee_days}d",
            )
            ref_bonus = tx.amount_kopecks
            ref_days = invitee_days
        except ValueError:
            # Награда не может быть нулевой ни при каких настройках (без прайса
            # есть фолбэк на легаси-сумму), так что сюда попадём только если
            # обнулили и её. Триал в любом случае выдаём — подарок приглашённому
            # это приятный довесок, а не условие.
            logger.warning("trial.activate: реферальный подарок обнулён настройками")

    user.trial_expires_at = (
        _trial_expiry(now, trial_days + ref_days) if set_expiry else None
    )

    logger.info(
        "trial.activate user=%s amount=%s ref_bonus=%s days=%s+%s expires=%s",
        user.id, amount, ref_bonus, trial_days, ref_days, user.trial_expires_at,
    )
    return TrialActivationResult(
        trial_amount_kopecks=amount,
        referral_bonus_kopecks=ref_bonus,
        balance_kopecks=user.balance_kopecks,
        trial_expires_at=user.trial_expires_at,
        trial_days=trial_days,
        referral_days=ref_days,
    )


def _ledger_amount(db: Session, user_id: int, reference: str) -> int | None:
    """Сумма записи журнала по reference; None, если записи нет."""
    row = (
        db.query(models.BalanceTransaction.amount_kopecks)
        .filter(
            models.BalanceTransaction.user_id == user_id,
            models.BalanceTransaction.reference == reference,
        )
        .first()
    )
    return None if row is None else int(row[0])


def _expire_unpaid_subscription(db: Session, sub_id: int) -> None:
    """Списание после провижининга не прошло: гасим подписку, если она уже в БД.

    Warm-путь не коммитит, и rollback убирает подписку целиком. Cold-путь
    коммитит внутри ``provision_subscription``, и ссылка уже выдана: такую
    подписку переводим в expired, как делал прежний код эндпоинта. Сюда
    попадаем только при гонке: баланс проверяется до провижининга.
    """
    db.rollback()
    persisted = db.get(models.Subscription, sub_id)
    if persisted is not None:
        persisted.status = models.SubscriptionStatus.expired
        db.add(persisted)
        db.commit()


def activate_trial_full(
    db: Session, user: models.User, *, source: str
) -> TrialFullResult:
    """Бонус и сразу подписка на него (бот и кабинет без живой подписки).

    Шаги:
      1. Бонус (:func:`activate_trial`). «Уже активирован» здесь не ошибка:
         подписка могла быть ещё не выдана (legacy ``/api/trial/activate``,
         старый кабинет).
      2. Живая подписка → :class:`TrialLiveSubscription`.
      3. Триал уже был, и подписка у юзера была (в любом статусе) →
         :class:`TrialAlreadyUsed`.
      4. Триал был, подписок не было: путь по сумме записи ``trial:{uid}``.
         Не меньше цены плана — исторический «застрял на бонусе» со старыми
         150 ₽: полная цена и срок плана. Меньше — новый бонус без подписки:
         тратим его на N дней по зачисленной сумме.
      5–9. Подписка на ``_trial_plan`` до now + N дней + скрытые часы,
         списываем ровно зачисленное (бонус + подарок), остаток 0. Своих денег
         юзера на балансе это не касается. ``trial_expires_at`` = реальный
         конец доступа.

    Исключения провижининга (``ColdPathThrottled``, ``RuntimeError``) летят
    до commit, поэтому вызывающий откатывает бонус и ``trial_activated_at``,
    и юзер может повторить. ``ValueError`` — не хватило баланса (только
    исторический путь или уже возвращённый бонус), API отвечает 402.
    Commit и аудит у вызывающего.
    """
    from .provisioning import ProvisioningOrchestrator

    fresh = True
    activation: TrialActivationResult | None = None
    try:
        activation = activate_trial(db, user.id)
    except TrialAlreadyActivated:
        fresh = False

    # Гонка двойного тапа: FOR UPDATE на юзере до проверки подписок,
    # как в webapp_activate.
    db.refresh(user, with_for_update=True)
    live = (
        db.query(models.Subscription.id)
        .filter(
            models.Subscription.user_id == user.id,
            models.Subscription.status.in_(
                [
                    models.SubscriptionStatus.active,
                    models.SubscriptionStatus.frozen,
                ]
            ),
        )
        .first()
    )
    if live is not None:
        raise TrialLiveSubscription()

    plan = _trial_plan(db)
    if plan is None:
        raise NoTrialPlan()

    historical = False
    if activation is not None:
        trial_days = activation.trial_days
        referral_days = activation.referral_days
        charge = activation.trial_amount_kopecks + activation.referral_bonus_kopecks
    else:
        had_subscription = (
            db.query(models.Subscription.id)
            .filter(models.Subscription.user_id == user.id)
            .first()
        )
        bonus = _ledger_amount(db, user.id, f"trial:{user.id}")
        # Нет записи бонуса при выставленном trial_activated_at — это
        # восстановленные юзеры (generate_restore_sql.py так блокирует им
        # триал): бесплатные дни им не положены.
        if had_subscription is not None or bonus is None:
            raise TrialAlreadyUsed()
        gift = _ledger_amount(db, user.id, f"referral_signup:{user.id}") or 0
        if bonus >= balance_svc.plan_price_kopecks(plan):
            historical = True
            trial_days = referral_days = None
            charge = balance_svc.plan_price_kopecks(plan)
        else:
            trial_days = balance_svc.TRIAL_DURATION_DAYS
            referral_days = invitee_bonus_days(db, user) if gift > 0 else 0
            charge = bonus + gift

    # Баланс проверяем ДО провижининга: иначе на 402 оставалась бы уже
    # выданная (cold-путь коммитит сам) подписка.
    if (user.balance_kopecks or 0) < charge:
        raise ValueError(
            f"insufficient balance: need {charge}, have {user.balance_kopecks or 0}"
        )

    expires: datetime | None = None
    if not historical:
        expires = _trial_expiry(utcnow(), trial_days + referral_days)

    orchestrator = ProvisioningOrchestrator(db)
    # notify_config_ready=False: бот и кабинет отдают ссылку сами сразу после
    # ответа, warm-пуш с той же ссылкой через 10 с был бы дублем. Cold-путь
    # (девайс pending) пуш шлёт по-прежнему.
    sub, _task = orchestrator.provision_subscription(
        user, plan, expires_at_override=expires, notify_config_ready=False
    )
    try:
        charged = balance_svc.activate_subscription(
            db,
            user.id,
            sub,
            reference=f"trial-full:{sub.id}",
            price_kopecks=None if historical else charge,
            expires_at=expires,
        )
    except ValueError:
        _expire_unpaid_subscription(db, sub.id)
        raise

    if not historical:
        user.trial_expires_at = sub.expires_at
        db.add(user)
        db.flush()

    logger.info(
        "trial.activate_full source=%s user=%s sub=%s fresh=%s historical=%s "
        "days=%s+%s charged=%s expires=%s",
        source, user.id, sub.id, fresh, historical, trial_days, referral_days,
        charged, sub.expires_at,
    )
    return TrialFullResult(
        sub=sub,
        plan=plan,
        trial_days=trial_days,
        referral_days=referral_days,
        hidden_hours=None if historical else balance_svc.TRIAL_HIDDEN_HOURS,
        charged_kopecks=charged,
        fresh=fresh,
    )
