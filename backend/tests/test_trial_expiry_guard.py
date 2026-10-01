"""Коммит-страховка плана «триал 3 дня» (docs/operations/trial_3_days_plan_2026_09_30.md).

Покрывает:
  * ``balance.user_has_paid`` и ``balance.is_unpaid_trial`` (Б1);
  * гейт предупреждения ``trial_expiry_warning`` в воркере (3a, Б4) — тест 6;
  * clawback по непотраченной части бонуса из журнала (3b, Б4) — тест 5;
  * новый текст пуша ``trial_expiry_warning`` без «месяца» и «50 ₽» (Б6).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import models, worker
from app.services import balance as balance_svc
from tests.factories import make_node, make_subscription, make_user

SOLO_KOPECKS = 15000


# ── фикстуры ─────────────────────────────────────────────────────────


def _plan(db: Session, *, name: str = "Solo", price: int = 150) -> models.Plan:
    plan = models.Plan(
        name=name,
        duration_days=30,
        max_devices=1,
        price=price,
        is_visible=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _tx(
    db: Session,
    user: models.User,
    amount: int,
    kind: models.BalanceTxKind,
    reference: str,
) -> None:
    """Строка журнала + кэш баланса, как пишет ``balance._record_tx``."""
    db.add(
        models.BalanceTransaction(
            user_id=user.id, amount_kopecks=amount, kind=kind, reference=reference
        )
    )
    user.balance_kopecks = (user.balance_kopecks or 0) + amount
    db.add(user)
    db.commit()


def _trial_user(
    db: Session,
    tg: str,
    *,
    bonus: int,
    expires_in: timedelta,
) -> models.User:
    """Юзер, взявший триал: бонус ``trial:{uid}`` и таймер ``trial_expires_at``."""
    user = make_user(db, telegram_id=tg)
    now = datetime.utcnow()
    user.trial_activated_at = now - timedelta(days=1)
    user.trial_expires_at = now + expires_in
    db.add(user)
    db.commit()
    _tx(db, user, bonus, models.BalanceTxKind.bonus, f"trial:{user.id}")
    return user


def _sub(db: Session, user: models.User, plan: models.Plan) -> models.Subscription:
    node = db.query(models.VPNNode).first() or make_node(db)
    sub = make_subscription(db, user, plan, node)
    sub.auto_renew = True
    db.add(sub)
    db.commit()
    return sub


def _paid_invoice(
    db: Session,
    user: models.User,
    plan: models.Plan,
    *,
    sub: models.Subscription | None = None,
    amount: int = 150,
    status: models.InvoiceStatus = models.InvoiceStatus.paid,
) -> models.Invoice:
    inv = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        subscription_id=sub.id if sub is not None else None,
        amount=amount,
        currency="RUB",
        status=status,
        action=models.InvoiceAction.renewal,
        kind="subscription",
    )
    db.add(inv)
    db.commit()
    return inv


def _run_pass(db: Session) -> dict:
    stats: dict = {}
    worker._run_trial_expiry_pass(db, stats)
    db.expire_all()
    return stats


def _warnings(db: Session, user: models.User) -> list[models.AuditLog]:
    return (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.target_type == "user",
            models.AuditLog.target_id == user.id,
            models.AuditLog.action.like("trial_expiry_warning%"),
        )
        .all()
    )


def _clawbacks(db: Session, user: models.User) -> list[models.BalanceTransaction]:
    return (
        db.query(models.BalanceTransaction)
        .filter_by(reference=f"trial_expiry_clawback:{user.id}")
        .all()
    )


# ── user_has_paid ────────────────────────────────────────────────────


def test_user_has_paid_false_for_trial_only(db_session: Session) -> None:
    plan = _plan(db_session)
    user = _trial_user(db_session, "501", bonus=1500, expires_in=timedelta(days=2))
    sub = _sub(db_session, user, plan)
    _tx(db_session, user, -1500, models.BalanceTxKind.spend, f"trial-full:{sub.id}")
    # Неоплаченный счёт, счёт на 0 и clawback-овый adjust оплатой не считаются.
    _paid_invoice(db_session, user, plan, sub=sub, status=models.InvoiceStatus.pending)
    _paid_invoice(db_session, user, plan, sub=sub, amount=0)
    _tx(db_session, user, 1500, models.BalanceTxKind.bonus, f"referral_payout:{user.id + 1}")
    _tx(db_session, user, -100, models.BalanceTxKind.adjust, f"trial_expiry_clawback:{user.id}")

    assert balance_svc.user_has_paid(db_session, user.id) is False


def test_user_has_paid_true_for_each_payment_kind(db_session: Session) -> None:
    plan = _plan(db_session)

    by_invoice = _trial_user(db_session, "511", bonus=1500, expires_in=timedelta(days=2))
    _paid_invoice(db_session, by_invoice, plan)

    by_topup = _trial_user(db_session, "512", bonus=1500, expires_in=timedelta(days=2))
    _tx(db_session, by_topup, 10000, models.BalanceTxKind.topup, "invoice:77")

    by_admin = _trial_user(db_session, "513", bonus=1500, expires_in=timedelta(days=2))
    _tx(
        db_session, by_admin, 15000, models.BalanceTxKind.adjust,
        f"admin_topup:{by_admin.id}:1726000000",
    )

    by_renew = _trial_user(db_session, "514", bonus=15000, expires_in=timedelta(days=2))
    _tx(db_session, by_renew, -15000, models.BalanceTxKind.spend, "renew:42")

    # Восстановленный через generate_restore_sql.py: журнала нет вовсе.
    restored = make_user(db_session, telegram_id="515")
    restored.balance_kopecks = 30000
    db_session.add(restored)
    db_session.commit()

    for u in (by_invoice, by_topup, by_admin, by_renew, restored):
        assert balance_svc.user_has_paid(db_session, u.id) is True, u.telegram_id


# ── is_unpaid_trial ──────────────────────────────────────────────────


def test_is_unpaid_trial_only_with_trial_full_spend(db_session: Session) -> None:
    plan = _plan(db_session)
    user = _trial_user(db_session, "521", bonus=15000, expires_in=timedelta(days=2))
    old_webapp = _sub(db_session, user, plan)
    _tx(db_session, user, -15000, models.BalanceTxKind.spend, f"activate:{old_webapp.id}")
    assert balance_svc.is_unpaid_trial(db_session, old_webapp) is False

    other = make_user(db_session, telegram_id="522")
    _tx(db_session, other, 1500, models.BalanceTxKind.bonus, f"trial:{other.id}")
    trial_sub = _sub(db_session, other, plan)
    _tx(db_session, other, -1500, models.BalanceTxKind.spend, f"trial-full:{trial_sub.id}")
    # Неоплаченный renewal-счёт от воркера (3d-напоминание) триал не снимает.
    _paid_invoice(db_session, other, plan, sub=trial_sub, status=models.InvoiceStatus.pending)
    assert balance_svc.is_unpaid_trial(db_session, trial_sub) is True


def test_is_unpaid_trial_false_after_any_payment_of_this_sub(db_session: Session) -> None:
    plan = _plan(db_session)

    def trial_sub(tg: str) -> tuple[models.User, models.Subscription]:
        u = make_user(db_session, telegram_id=tg)
        _tx(db_session, u, 1500, models.BalanceTxKind.bonus, f"trial:{u.id}")
        s = _sub(db_session, u, plan)
        _tx(db_session, u, -1500, models.BalanceTxKind.spend, f"trial-full:{s.id}")
        return u, s

    u1, renewed = trial_sub("531")
    _tx(db_session, u1, 15000, models.BalanceTxKind.topup, "invoice:1")
    _tx(db_session, u1, -15000, models.BalanceTxKind.spend, f"renew:{renewed.id}")

    u2, changed = trial_sub("532")
    _tx(db_session, u2, 30000, models.BalanceTxKind.topup, "invoice:2")
    _tx(db_session, u2, -30000, models.BalanceTxKind.spend, f"change_plan:{changed.id}")

    u3, by_card = trial_sub("533")
    _paid_invoice(db_session, u3, plan, sub=by_card)

    for s in (renewed, changed, by_card):
        assert balance_svc.is_unpaid_trial(db_session, s) is False, s.id


# ── тест 6: предупреждение о конце триала ────────────────────────────


def test_warning_text_has_no_month_and_no_50_rub(client, db_session: Session) -> None:
    user = make_user(db_session, telegram_id="601")
    db_session.add(
        models.AuditLog(
            actor="system",
            actor_type=models.AuditActor.system,
            action="trial_expiry_warning",
            target_type="user",
            target_id=user.id,
            extra={"telegram_id": user.telegram_id},
        )
    )
    db_session.commit()

    resp = client.get("/api/notifications/pending?limit=10")
    assert resp.status_code == 200
    items = [n for n in resp.json() if n["type"] == "trial_expiry_warning"]
    assert len(items) == 1
    text = items[0]["text"]
    assert "месяц" not in text
    assert "50 ₽" not in text
    assert "Бесплатные дни скоро закончатся" in text
    assert "/balance" in text


def test_warning_skipped_for_bonus_only_payer(db_session: Session) -> None:
    """Платящий забрал в кабинете только бонус (старый код ставит ему таймер):
    его живая подписка не триал, «пополни баланс» ему ложь."""
    plan = _plan(db_session)
    user = _trial_user(db_session, "611", bonus=SOLO_KOPECKS, expires_in=timedelta(days=2))
    sub = _sub(db_session, user, plan)
    _paid_invoice(db_session, user, plan, sub=sub)
    # Даже с пустым балансом: решает то, что подписка уже оплачена.
    _tx(db_session, user, -SOLO_KOPECKS, models.BalanceTxKind.spend, f"renew:{sub.id}")

    stats = _run_pass(db_session)

    assert _warnings(db_session, user) == []
    assert stats.get("trial_warn_skipped") == 1


def test_warning_skipped_when_topup_covers_renewal(db_session: Session) -> None:
    plan = _plan(db_session)
    user = _trial_user(db_session, "621", bonus=1500, expires_in=timedelta(days=2))
    sub = _sub(db_session, user, plan)
    _tx(db_session, user, -1500, models.BalanceTxKind.spend, f"trial-full:{sub.id}")
    _tx(db_session, user, SOLO_KOPECKS, models.BalanceTxKind.topup, "invoice:5")

    _run_pass(db_session)

    assert _warnings(db_session, user) == []


def test_warning_sent_when_topup_short_of_renewal(db_session: Session) -> None:
    """Пополнил на 100 ₽ при продлении за 150 ₽: user_has_paid уже true, но
    предупреждение правдиво и должно уйти."""
    plan = _plan(db_session)
    user = _trial_user(db_session, "631", bonus=1500, expires_in=timedelta(days=2))
    sub = _sub(db_session, user, plan)
    _tx(db_session, user, -1500, models.BalanceTxKind.spend, f"trial-full:{sub.id}")
    _tx(db_session, user, 10000, models.BalanceTxKind.topup, "invoice:6")
    assert balance_svc.user_has_paid(db_session, user.id) is True

    stats = _run_pass(db_session)

    logs = _warnings(db_session, user)
    assert len(logs) == 1
    assert logs[0].extra["telegram_id"] == "631"
    assert stats.get("trial_warned") == 1

    # Дедуп: второй тик не дублирует.
    _run_pass(db_session)
    assert len(_warnings(db_session, user)) == 1


def test_warning_sent_without_live_subscription(db_session: Session) -> None:
    """Без живой подписки гейт не пропускает, как и раньше."""
    user = _trial_user(db_session, "641", bonus=SOLO_KOPECKS, expires_in=timedelta(days=2))

    _run_pass(db_session)

    assert len(_warnings(db_session, user)) == 1


# ── тест 5: clawback ─────────────────────────────────────────────────


def _expired_trial_user(db: Session, tg: str, *, bonus: int) -> models.User:
    return _trial_user(db, tg, bonus=bonus, expires_in=-timedelta(hours=1))


def test_clawback_old_trial_takes_whole_unspent_bonus(db_session: Session) -> None:
    user = _expired_trial_user(db_session, "701", bonus=SOLO_KOPECKS)

    stats = _run_pass(db_session)

    user = db_session.get(models.User, user.id)
    assert user.balance_kopecks == 0
    rows = _clawbacks(db_session, user)
    assert [r.amount_kopecks for r in rows] == [-SOLO_KOPECKS]
    assert rows[0].kind == models.BalanceTxKind.adjust
    assert user.trial_expires_at is None
    assert stats.get("trial_clawback") == 1


def test_clawback_skips_card_payer(db_session: Session) -> None:
    """Оплата тарифа картой (счёт без topup-строки) раньше не спасала."""
    plan = _plan(db_session)
    user = _expired_trial_user(db_session, "711", bonus=SOLO_KOPECKS)
    _paid_invoice(db_session, user, plan)

    stats = _run_pass(db_session)

    user = db_session.get(models.User, user.id)
    assert user.balance_kopecks == SOLO_KOPECKS
    assert _clawbacks(db_session, user) == []
    assert user.trial_expires_at is None
    assert stats.get("trial_kept") == 1


def test_clawback_keeps_referral_payout_of_trial_full_user(db_session: Session) -> None:
    plan = _plan(db_session)
    user = _expired_trial_user(db_session, "721", bonus=1500)
    sub = _sub(db_session, user, plan)
    _tx(db_session, user, -1500, models.BalanceTxKind.spend, f"trial-full:{sub.id}")
    _tx(db_session, user, 5000, models.BalanceTxKind.bonus, "referral_payout:999")

    _run_pass(db_session)

    user = db_session.get(models.User, user.id)
    assert user.balance_kopecks == 5000
    assert _clawbacks(db_session, user) == []
    assert user.trial_expires_at is None


def test_clawback_keeps_referral_payout_of_old_webapp_trial(db_session: Session) -> None:
    """Старый кабинетный триал тратил бонус через activate:{sub.id}."""
    plan = _plan(db_session)
    user = _expired_trial_user(db_session, "731", bonus=SOLO_KOPECKS)
    sub = _sub(db_session, user, plan)
    _tx(db_session, user, -SOLO_KOPECKS, models.BalanceTxKind.spend, f"activate:{sub.id}")
    _tx(db_session, user, 5000, models.BalanceTxKind.bonus, "referral_payout:998")

    _run_pass(db_session)

    user = db_session.get(models.User, user.id)
    assert user.balance_kopecks == 5000
    assert _clawbacks(db_session, user) == []
    assert user.trial_expires_at is None


def test_clawback_takes_unspent_referral_signup_gift(db_session: Session) -> None:
    """Подарок приглашённому тоже часть бонуса: непотраченный остаток уходит,
    деньги сверх бонуса (referral_payout) остаются."""
    plan = _plan(db_session)
    user = _expired_trial_user(db_session, "741", bonus=SOLO_KOPECKS)
    _tx(db_session, user, 1500, models.BalanceTxKind.bonus, f"referral_signup:{user.id}")
    sub = _sub(db_session, user, plan)
    _tx(db_session, user, -SOLO_KOPECKS, models.BalanceTxKind.spend, f"activate:{sub.id}")
    _tx(db_session, user, 5000, models.BalanceTxKind.bonus, "referral_payout:997")

    _run_pass(db_session)

    user = db_session.get(models.User, user.id)
    assert user.balance_kopecks == 5000
    assert [r.amount_kopecks for r in _clawbacks(db_session, user)] == [-1500]


def test_clawback_skips_admin_topup_payer(db_session: Session) -> None:
    user = _expired_trial_user(db_session, "751", bonus=SOLO_KOPECKS)
    _tx(
        db_session, user, 15000, models.BalanceTxKind.adjust,
        f"admin_topup:{user.id}:1726000000",
    )

    _run_pass(db_session)

    user = db_session.get(models.User, user.id)
    assert user.balance_kopecks == 30000
    assert _clawbacks(db_session, user) == []
    assert user.trial_expires_at is None
