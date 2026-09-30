"""User balance ledger and monthly billing (V2).

Simple model: on activation ``plan.price`` is debited from the wallet and
``expires_at`` is set to ``now + plan.duration_days``. A worker tick runs
hourly: when ``expires_at <= now`` and ``auto_renew`` is on, we try to
debit another month. If the wallet is short the sub is expired.

Each mutation is row-locked (``SELECT ... FOR UPDATE``) so concurrent
topups and charges can never race.

Freeze: 1 per calendar year, ``FREEZE_DAYS`` days (default 7).
``expires_at += FREEZE_DAYS`` so the user doesn't lose paid time.
Devices are revoked on freeze, re-provisioned on unfreeze. Early
unfreeze is allowed but blocks further freezes until the next calendar
year.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta

from prometheus_client import Counter, Gauge
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────

FREEZE_DAYS = int(os.getenv("FREEZE_DAYS", "7"))
REFERRAL_BONUS_KOPECKS = int(os.getenv("REFERRAL_BONUS_KOPECKS", "5000"))
# Награда рефереру — в ДНЯХ подписки, начисляется когда приглашённый ВПЕРВЫЕ
# заплатил (см. api/invoices.py).
#
# 10 дней = 50 ₽ по текущему прайсу (Solo 150 ₽ / 30 дней) — ровно та сумма,
# которую платили фиксированной константой до перехода на дни. Экономика не
# изменилась, но «10 дней подписки» звучит весомее, чем «50 ₽»: в этом и смысл
# перехода на дни, а не в увеличении расходов. Месяц в подарок (150 ₽ при
# платеже приглашённого 150 ₽) съедал бы первый платёж целиком.
REFERRAL_REWARD_DAYS = int(os.getenv("REFERRAL_REWARD_DAYS", "10"))
# Подарок приглашённому при активации триала — прибавляется к бесплатным дням
# триала (3 + 3), а он ещё ничего не заплатил. Поэтому втрое меньше награды
# реферера: щедрость здесь оплачивает фарм триалов, а не рост.
REFERRAL_INVITEE_DAYS = int(os.getenv("REFERRAL_INVITEE_DAYS", "3"))
# Видимые бесплатные дни триала: бонус = столько дней по цене дня Solo, и он
# сразу тратится на подписку на эти дни (services/trial.py).
TRIAL_DURATION_DAYS = int(os.getenv("TRIAL_DURATION_DAYS", "3"))
# Скрытый запас сверх видимых дней: подписка живёт до now + дни + эти часы, а
# списываем только за видимые дни. Счётчики (кабинет, Happ) округляют вниз и
# показывают ровно «3 дня», а «истекает завтра» приходит в конце обещанного
# срока, и пополнить можно, не теряя ссылку. 0 выключает.
TRIAL_HIDDEN_HOURS = int(os.getenv("TRIAL_HIDDEN_HOURS", "24"))
TRIAL_EXPIRY_WARN_DAYS = int(os.getenv("TRIAL_EXPIRY_WARN_DAYS", "3"))
MIN_TOPUP_KOPECKS = int(os.getenv("MIN_TOPUP_KOPECKS", "10000"))
EXTRA_DEVICE_MONTHLY_KOPECKS = int(os.getenv("EXTRA_DEVICE_KOPECKS_PER_MONTH", "10000"))


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


def _plan_months(plan: models.Plan | None) -> int:
    """Число 30-дневных «месяцев» в периоде плана.

    ``EXTRA_DEVICE_MONTHLY_KOPECKS`` — цена за календарный месяц, поэтому
    доп. слоты на годовом плане стоят ``N * EXTRA * 12`` за весь период.
    Единый источник множителя для renewal / change_plan / runway.
    """
    if plan is None:
        return 1
    return max(1, (plan.duration_days or 30) // 30)


def total_renewal_cost_kopecks(sub: models.Subscription) -> int:
    """Full kopecks that the next renewal will bill: plan + slots.

    ``EXTRA_DEVICE_MONTHLY_KOPECKS`` is a *per calendar month* price, so
    an annual plan with N slots prepays ``N * EXTRA * 12`` on renewal —
    device-slots are scoped to the sub's period, not the calendar
    month. Without this scaling a user with 2 slots on a yearly plan
    would look like they only owe 2 × monthly_fee for the whole year,
    and UI runway (``balance // renewal_cost``) would overstate by 11×.

    Used by every surface that asks "can this wallet afford the next
    renewal / how many renewals fit": webapp /me, bot /balance, worker
    low-balance warning, legacy days_remaining.
    """
    plan = sub.plan
    if plan is None:
        return 0
    base = plan_price_kopecks(plan)
    slots = sub.extra_device_slots or 0
    months = _plan_months(plan)
    return base + slots * EXTRA_DEVICE_MONTHLY_KOPECKS * months


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


def days_to_kopecks(db: Session, days: int) -> int:
    """Во сколько обходится подарок в ``days`` дней подписки.

    Считаем по самому дешёвому видимому 30-дневному плану — тому же, на который
    ориентируется триал. Так «30 дней в подарок» автоматически следует за
    прайсом, а не застывает константой в коде.

    Награда именно в днях, а не в рублях, — сознательный выбор: день стоит нам
    маржи, а не выручки, и весь рынок (Paper VPN, FKey) платит рефералам
    временем. Плюс фиксированная сумма в 50 ₽ обесценивалась при каждом
    повышении прайса, а «месяц» читается одинаково всегда.
    """
    from .trial import _trial_plan  # локально: trial импортирует balance

    plan = _trial_plan(db)
    if plan is None or not plan.duration_days:
        return 0
    per_day = float(plan.price) * 100 / plan.duration_days
    return int(round(per_day * max(0, days)))


def referral_bonus(
    db: Session,
    user_id: int,
    *,
    reference: str,
    days: int | None = None,
    note: str = "referral bonus",
) -> models.BalanceTransaction:
    """Начислить реферальную награду как ``kind=bonus``.

    ``days`` — размер подарка в днях подписки (конвертируется в копейки по
    текущему прайсу). Без него падаем на легаси-константу
    ``REFERRAL_BONUS_KOPECKS``: она осталась ради обратной совместимости с
    прод-env, где сумма задана явно.
    """
    amount = days_to_kopecks(db, days) if days is not None else REFERRAL_BONUS_KOPECKS
    if amount <= 0:
        # Прайс не настроен (нет видимого 30-дневного плана) — оценить подарок в
        # днях нечем. Падать здесь нельзя: реферер сделал свою работу, а мы
        # лишили бы его награды из-за нашей же незаполненной таблицы планов.
        # Поэтому фолбэк на легаси-сумму; ноль не пишем ни при каких раскладах —
        # нулевая транзакция заняла бы reference и заблокировала выплату
        # навсегда.
        logger.warning(
            "referral: подарок в днях (%s) не оценён — нет видимого 30-дневного "
            "плана, начисляю легаси-сумму %s копеек",
            days, REFERRAL_BONUS_KOPECKS,
        )
        amount = REFERRAL_BONUS_KOPECKS
    if amount <= 0:
        raise ValueError("referral reward is zero — nothing to credit")
    return topup(
        db, user_id, amount,
        reference=reference,
        kind=models.BalanceTxKind.bonus,
        note=note,
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
    price_kopecks: int | None = None,
    expires_at: datetime | None = None,
) -> int:
    """Debit ``plan.price`` from wallet, set ``expires_at``, ``auto_renew=True``.

    Called at subscription activation. Raises ``ValueError`` on insufficient
    balance (callers surface 402 with topup hint).

    ``price_kopecks`` и ``expires_at`` нужны только триалу
    (``services/trial.activate_trial_full``): списать ровно зачисленный бонус
    (N дней по цене дня, а не цену плана) и поставить срок «N дней + скрытые
    часы». Без них поведение прежнее: полная цена плана и
    ``now + plan.duration_days``.
    """
    price = plan_price_kopecks(sub.plan) if price_kopecks is None else price_kopecks
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

    sub.expires_at = expires_at or utcnow() + timedelta(days=sub.plan.duration_days)
    sub.auto_renew = True
    # Новый оплаченный период — шкала трафика начинает с нуля.
    sub.traffic_used_bytes = 0
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

    base_price = plan_price_kopecks(sub.plan)
    extra_slots = sub.extra_device_slots or 0
    # Доп. слоты — помесячная цена, масштабируем на длительность плана
    # (годовой = ×12), как в total_renewal_cost_kopecks. Иначе слоты на
    # годовом плане недосчитываются в 12 раз.
    months = _plan_months(sub.plan)
    device_surcharge = extra_slots * EXTRA_DEVICE_MONTHLY_KOPECKS * months
    price = base_price + device_surcharge
    if price <= 0:
        logger.warning("renew: plan %s has no price, skipping sub %s", sub.plan_id, sub.id)
        return True  # free plan, nothing to charge

    user = _lock_user(db, sub.user_id)
    if (user.balance_kopecks or 0) < price:
        CHARGES_TOTAL.labels(result="insufficient").inc()
        logger.info(
            "balance.renew insufficient user=%s sub=%s price=%s (base=%s + %s slots * %s) balance=%s",
            user.id, sub.id, price, base_price, extra_slots,
            EXTRA_DEVICE_MONTHLY_KOPECKS, user.balance_kopecks,
        )
        return False

    _record_tx(
        db, user,
        amount_kopecks=-price,
        kind=models.BalanceTxKind.spend,
        reference=f"renew:{sub.id}",
        note=f"renew {sub.plan.name} (sub {sub.id}){f' +{extra_slots} devices' if extra_slots else ''}",
    )

    # Extend from the current expires_at (not from now) so we don't
    # shorten the period if the tick fires a few hours late.
    base = sub.expires_at or utcnow()
    sub.expires_at = base + timedelta(days=sub.plan.duration_days)
    # Новый оплаченный период — шкала трафика начинает с нуля.
    sub.traffic_used_bytes = 0
    db.add(sub)
    db.flush()

    CHARGES_TOTAL.labels(result="success").inc()
    CHARGE_AMOUNT_TOTAL.inc(price)
    logger.info(
        "balance.renew ok user=%s sub=%s price=%s (base=%s +devices=%s) -> expires=%s balance=%s",
        user.id, sub.id, price, base_price, device_surcharge, sub.expires_at, user.balance_kopecks,
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
        # Кап: заморозка (expires_at += FREEZE_DAYS) растягивает остаток
        # сверх duration_days → рефанд не должен превышать цену плана,
        # иначе создаём деньги из воздуха в леджере.
        remaining_days = min(remaining_days, total_days)
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
    # Смена плана оплачивает новый период — шкала трафика начинает с нуля.
    sub.traffic_used_bytes = 0

    # Count live devices and set extra_device_slots for any that exceed
    # the new plan's bundle. This avoids "free" devices lingering after
    # a downgrade (e.g. Family 3 → Solo 1 keeps 3 live devices).
    live_devices = _live_device_count(db, sub.id)
    new_bundled = new_plan.max_devices or 1
    overflow = max(live_devices - new_bundled, 0)
    sub.extra_device_slots = overflow

    # Charge for overflow slots (за весь период нового плана; помесячная
    # цена × число месяцев, чтобы годовой план не недосчитывал слоты).
    months = _plan_months(new_plan)
    device_surcharge = overflow * EXTRA_DEVICE_MONTHLY_KOPECKS * months
    if device_surcharge > 0:
        if (user.balance_kopecks or 0) < device_surcharge:
            raise ValueError(
                f"insufficient balance for {overflow} extra device(s): "
                f"need {device_surcharge}, have {user.balance_kopecks or 0}"
            )
        _record_tx(
            db, user,
            amount_kopecks=-device_surcharge,
            kind=models.BalanceTxKind.spend,
            reference=f"change_plan_devices:{sub.id}",
            note=f"{overflow} extra device(s) on {new_plan.name}",
        )

    db.add(sub)
    db.flush()

    logger.info(
        "balance.change_plan user=%s sub=%s %s->%s refund=%s charge=%s device_surcharge=%s (%s overflow) -> balance=%s",
        sub.user_id, sub.id, old_plan.name, new_plan.name,
        refund, new_price, device_surcharge, overflow, user.balance_kopecks,
    )
    return {"refunded_kopecks": refund, "charged_kopecks": new_price + device_surcharge}


# ── Платил ли юзер / неоплаченный триал ─────────────────────────────

def user_has_paid(db: Session, user_id: int) -> bool:
    """Платил ли юзер хоть раз (или триального бонуса у него нет вовсе).

    Правда, если есть хоть одно из:
      * оплаченный счёт с суммой > 0 (карта, СБП, крипта, ручной mark paid);
      * запись ``kind=topup`` (пополнение баланса);
      * ручное зачисление ``adjust admin_topup:%`` > 0: так проводили оплаты
        без счёта, пока lava лежал (24.08–19.09.2026);
      * продление с баланса ``spend renew:%``;
      * у юзера нет записи триального бонуса ``trial:{uid}``. Подписку без
        бонуса можно было только купить или получить от админа, а юзеры,
        восстановленные через ``generate_restore_sql.py``, журнала не имеют
        вовсе (баланс пишется прямо в ``users.balance_kopecks``).

    Последняя ветка безопасна только для заморозки и clawback-а: в clawback
    попадают юзеры с ``trial_expires_at``, а он ставится вместе с
    ``trial:{uid}``. В гейт предупреждения о конце триала функция не идёт:
    она истинна после любого пополнения, например на 100 ₽, а продление за
    150 ₽ оно не покрывает.
    """
    paid_invoice = (
        db.query(models.Invoice.id)
        .filter(
            models.Invoice.user_id == user_id,
            models.Invoice.status == models.InvoiceStatus.paid,
            models.Invoice.amount > 0,
        )
        .first()
    )
    if paid_invoice is not None:
        return True

    tx = models.BalanceTransaction
    paid_tx = (
        db.query(tx.id)
        .filter(
            tx.user_id == user_id,
            or_(
                tx.kind == models.BalanceTxKind.topup,
                and_(
                    tx.kind == models.BalanceTxKind.adjust,
                    tx.reference.like("admin_topup:%"),
                    tx.amount_kopecks > 0,
                ),
                and_(
                    tx.kind == models.BalanceTxKind.spend,
                    tx.reference.like("renew:%"),
                ),
            ),
        )
        .first()
    )
    if paid_tx is not None:
        return True

    trial_bonus_row = (
        db.query(tx.id)
        .filter(tx.user_id == user_id, tx.reference == f"trial:{user_id}")
        .first()
    )
    return trial_bonus_row is None


def is_unpaid_trial(db: Session, sub: models.Subscription) -> bool:
    """Подписка выдана на бесплатные дни, и за неё ещё не платили.

    Триал = есть ``spend trial-full:{sub.id}`` (подписка куплена на
    триальный бонус). Оплатой этой подписки считается любое из:
      * продление с баланса ``spend renew:{sub.id}``;
      * смена тарифа в кабинете ``spend change_plan:{sub.id}`` (sub.id тот же);
      * оплаченный счёт с ``subscription_id = sub.id`` и суммой > 0:
        продление картой или СБП (``?fix=1``, ``/renew``, ``/plans`` на тот же
        тариф). Такая оплата строк в журнал не пишет, только сдвигает срок.

    Без последних двух условий подписка, оплаченная картой или сменившая
    тариф, навсегда осталась бы «триалом». Старые кабинетные триалы
    (``activate:{sub.id}``, без ``trial-full``) сюда не попадают.
    """
    tx = models.BalanceTransaction
    trial_spend = (
        db.query(tx.id)
        .filter(
            tx.user_id == sub.user_id,
            tx.kind == models.BalanceTxKind.spend,
            tx.reference == f"trial-full:{sub.id}",
        )
        .first()
    )
    if trial_spend is None:
        return False

    paid_spend = (
        db.query(tx.id)
        .filter(
            tx.user_id == sub.user_id,
            tx.kind == models.BalanceTxKind.spend,
            tx.reference.in_([f"renew:{sub.id}", f"change_plan:{sub.id}"]),
        )
        .first()
    )
    if paid_spend is not None:
        return False

    paid_invoice = (
        db.query(models.Invoice.id)
        .filter(
            models.Invoice.subscription_id == sub.id,
            models.Invoice.status == models.InvoiceStatus.paid,
            models.Invoice.amount > 0,
        )
        .first()
    )
    return paid_invoice is None


FREEZE_NEEDS_PAYMENT_DETAIL = "Заморозка станет доступна после первой оплаты"


def freeze_allowed_by_payment(
    db: Session, sub: models.Subscription, *, has_paid: bool | None = None
) -> bool:
    """Правило заморозки по оплате: юзер платил и эта подписка не неоплаченный
    триал — либо на балансе уже лежит полная стоимость её продления.

    Правило по подписке, а не по юзеру. Без ``is_unpaid_trial`` триальщик,
    пополнивший баланс в первый день, заморозил бы сам триал: +FREEZE_DAYS к
    сроку, и первое списание уехало бы на неделю. Исключение (решение
    владельца 30.09.2026, user 1000058 с 1500 ₽ на балансе на триальной
    подписке): кто уже положил деньги на продление, фактически купил — ему
    заморозку показываем; цена лазейки — до FREEZE_DAYS дней платящему.
    ``has_paid`` можно передать заранее посчитанным, когда подписок у юзера
    несколько (кабинет, /me).

    ``freeze_subscription`` это правило не проверяет: его зовёт только
    ``webapp_freeze``, и проверка стоит там (сервис покрыт тестами без журнала).
    """
    if has_paid is None:
        has_paid = user_has_paid(db, sub.user_id)
    if not has_paid:
        return False
    if not is_unpaid_trial(db, sub):
        return True
    user = db.get(models.User, sub.user_id)
    return bool(user) and (user.balance_kopecks or 0) >= total_renewal_cost_kopecks(sub)


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


# ── Extra device billing ──────────────────────────────────────────────


def prorated_extra_device_fee(sub: models.Subscription) -> int:
    """Pro-rated cost (kopecks) for one extra device slot.

    Full price is ``EXTRA_DEVICE_MONTHLY_KOPECKS`` for
    ``plan.duration_days``. We charge only the remaining fraction.
    """
    if not sub.expires_at or not sub.plan:
        return EXTRA_DEVICE_MONTHLY_KOPECKS
    remaining = (sub.expires_at - utcnow()).total_seconds()
    remaining_days = max(remaining / 86400, 0)
    period = sub.plan.duration_days or 30
    return max(int(math.ceil(EXTRA_DEVICE_MONTHLY_KOPECKS * remaining_days / period)), 0)


def charge_extra_device(
    db: Session,
    user_id: int,
    sub: models.Subscription,
    fee: int,
    *,
    reference: str,
) -> int:
    """Debit ``fee`` kopecks for an extra device slot.

    Increments ``sub.extra_device_slots``. Raises ``ValueError`` if
    balance is insufficient (caller should pre-check).
    """
    user = _lock_user(db, user_id)
    if (user.balance_kopecks or 0) < fee:
        raise ValueError(f"insufficient balance: need {fee}, have {user.balance_kopecks}")

    _record_tx(
        db, user,
        amount_kopecks=-fee,
        kind=models.BalanceTxKind.spend,
        reference=reference,
        note=f"extra device slot (sub {sub.id})",
    )
    sub.extra_device_slots = (sub.extra_device_slots or 0) + 1
    db.add(sub)
    CHARGE_AMOUNT_TOTAL.inc(fee)
    db.flush()
    logger.info(
        "extra_device charged sub=%s user=%s fee=%s slots=%s",
        sub.id, user_id, fee, sub.extra_device_slots,
    )
    return fee


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
    """V1 compat — how many days this balance can buy at the sub level.

    Picks the user's active sub on this plan so device-slot surcharge
    is included via ``total_renewal_cost_kopecks``. Falls back to bare
    plan price when there's no live sub yet (e.g. pre-activation
    preview).
    """
    sub = next(
        (
            s
            for s in (user.subscriptions or [])
            if s.plan_id == plan.id
            and s.status
            in (
                models.SubscriptionStatus.active,
                models.SubscriptionStatus.frozen,
            )
        ),
        None,
    )
    if sub is not None:
        cost = total_renewal_cost_kopecks(sub)
    else:
        cost = plan_price_kopecks(plan)
    if cost <= 0:
        return 0
    periods = (user.balance_kopecks or 0) // cost
    return periods * (plan.duration_days or 30)


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
