"""Триал 3 дня: сервис и биллинг (docs/operations/trial_3_days_plan_2026_09_30.md).

Покрывает Б1 (``activate_trial_full``, ``activate_subscription`` с ценой и
сроком, защита от повторного триала, путь по сумме ``trial:{uid}``), Б3
(3 + 3 дня, награда рефереру за первую оплату любым способом), Б5 (заморозка
после первой оплаты), 409 «on trial» и фильтр причин в воронке.

Номера из плана: 1 (бот), 2, 4, 7, 8, 9 (бот), 10, 11, 12. Кабинетная часть
тестов 1, 3 и 9 идёт вместе с API кабинета.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import models, worker
from app.api.invoices import ON_TRIAL_DETAIL
from app.api_webapp import issue_token
from app.config import get_settings
from app.services import balance as balance_svc
from app.services import onboarding_funnel, provisioning_throttle
from app.services.provisioning import ProvisioningOrchestrator
from app.services.provisioning_throttle import ColdPathThrottled
from app.time_utils import utcnow

from .factories import make_config, make_node, make_subscription, make_user

_ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_API_TOKEN", "")}
# Бот ходит в POST /api/invoices без admin-токена: только так срабатывает 409
# «другой тариф при живой подписке».
_NO_ADMIN = {"X-Admin-Token": ""}
SOLO_KOPECKS = 15000


@pytest.fixture(autouse=True)
def _reset_cold_throttle():
    """Cold-path троттл — глобальный in-memory bucket на процесс."""
    provisioning_throttle.reset_for_tests()
    yield
    provisioning_throttle.reset_for_tests()


# ── фикстуры ─────────────────────────────────────────────────────────


def _plan(
    db: Session, *, name: str = "Solo", price: int = 150, max_devices: int = 1
) -> models.Plan:
    plan = models.Plan(
        name=name,
        duration_days=30,
        max_devices=max_devices,
        price=price,
        is_visible=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _infra(db: Session) -> models.Plan:
    """Нода с конфигом и Solo 150 ₽ / 30 дней (3 дня = 15 ₽)."""
    node = make_node(db)
    make_config(db, node)
    return _plan(db)


def _invite(db: Session, user: models.User, *, bonus_days: int = 3) -> models.User:
    owner = make_user(db, telegram_id=f"owner-{user.telegram_id}")
    db.add(
        models.ReferralCode(
            owner_id=owner.id, code=f"code-{user.id}", bonus_days=bonus_days,
            reward_days=10,
        )
    )
    user.referred_by_id = owner.id
    db.add(user)
    db.commit()
    return owner


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


def _activate_full(client, user: models.User):
    return client.post(
        "/api/trial/activate_full",
        json={"telegram_id": user.telegram_id},
        headers=_ADMIN,
    )


def _ledger(db: Session, reference: str) -> list[models.BalanceTransaction]:
    return (
        db.query(models.BalanceTransaction)
        .filter(models.BalanceTransaction.reference == reference)
        .all()
    )


def _rejections(db: Session, user_id: int) -> list[str]:
    return [
        (row.extra or {}).get("reason")
        for row in db.query(models.AuditLog)
        .filter(
            models.AuditLog.action == "trial_activate_rejected",
            models.AuditLog.target_id == user_id,
        )
        .all()
    ]


def _auth(user_id: int) -> dict:
    token = issue_token(user_id, get_settings().webapp_jwt_secret, 600)
    return {"Authorization": f"Bearer {token}"}


def _near(a, b, *, seconds: int = 120) -> bool:
    return abs((a - b).total_seconds()) < seconds


# ── дефолты ──────────────────────────────────────────────────────────


def test_trial_defaults_are_3_days_and_24_hidden_hours() -> None:
    assert balance_svc.TRIAL_DURATION_DAYS == 3
    assert balance_svc.TRIAL_HIDDEN_HOURS == 24


def test_trial_amount_is_days_at_day_price(db_session: Session) -> None:
    from app.services import trial as trial_svc

    assert trial_svc.trial_amount_kopecks(db_session) == 0  # плана нет
    _plan(db_session)
    assert trial_svc.trial_amount_kopecks(db_session) == 1500


def test_trial_days_for_counts_invite(db_session: Session) -> None:
    from app.services import trial as trial_svc

    _plan(db_session)
    plain = make_user(db_session, telegram_id="plain")
    invited = make_user(db_session, telegram_id="invited")
    _invite(db_session, invited)
    assert trial_svc.trial_days_for(db_session, plain) == (3, 0)
    assert trial_svc.trial_days_for(db_session, invited) == (3, 3)


# ── 1. Триал на 3 и на 6 дней (бот) ──────────────────────────────────


@pytest.mark.parametrize(
    ("invited", "total_days", "charged"),
    [(False, 3, 1500), (True, 6, 3000)],
)
def test_activate_full_gives_trial_days(
    client, db_session: Session, invited: bool, total_days: int, charged: int
) -> None:
    _infra(db_session)
    user = make_user(db_session, telegram_id=f"t1-{invited}")
    if invited:
        _invite(db_session, user)
    before = utcnow()

    resp = _activate_full(client, user)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trial_days"] == 3
    assert data["referral_days"] == (3 if invited else 0)

    db_session.expire_all()
    sub = db_session.get(models.Subscription, data["subscription_id"])
    user = db_session.get(models.User, user.id)
    # Срок: видимые дни + 24 скрытых часа; списали только за видимые дни.
    assert _near(sub.expires_at, before + timedelta(days=total_days, hours=24))
    assert sub.auto_renew is True
    spend = _ledger(db_session, f"trial-full:{sub.id}")
    assert [row.amount_kopecks for row in spend] == [-charged]
    assert user.balance_kopecks == 0
    assert user.trial_expires_at == sub.expires_at

    audit = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "trial_activated_full")
        .one()
    )
    assert audit.extra["trial_days"] == 3
    assert audit.extra["referral_days"] == (3 if invited else 0)
    assert audit.extra["hidden_hours"] == 24
    assert audit.extra["charged_kopecks"] == charged
    assert audit.extra["source"] == "bot"


def test_invitee_gift_spent_with_bonus(client, db_session: Session) -> None:
    """3 + 3: +15 ₽ бонус, +15 ₽ подарок, −30 ₽ покупка. Новых видов записей нет."""
    _infra(db_session)
    user = make_user(db_session, telegram_id="t1-ledger")
    _invite(db_session, user)

    resp = _activate_full(client, user)
    assert resp.status_code == 200, resp.text

    rows = (
        db_session.query(models.BalanceTransaction)
        .filter(models.BalanceTransaction.user_id == user.id)
        .order_by(models.BalanceTransaction.id)
        .all()
    )
    assert [(r.kind, r.amount_kopecks) for r in rows] == [
        (models.BalanceTxKind.bonus, 1500),
        (models.BalanceTxKind.bonus, 1500),
        (models.BalanceTxKind.spend, -3000),
    ]


# ── 2. Повторный триал ───────────────────────────────────────────────


def test_repeat_after_trial_expired_is_already_used(client, db_session: Session) -> None:
    _infra(db_session)
    user = make_user(db_session, telegram_id="t2")
    first = _activate_full(client, user)
    assert first.status_code == 200, first.text

    # Триал истёк, человек пополнил баланс и жмёт старую кнопку в чате.
    db_session.expire_all()
    sub = db_session.get(models.Subscription, first.json()["subscription_id"])
    sub.status = models.SubscriptionStatus.expired
    db_session.commit()
    user = db_session.get(models.User, user.id)
    _tx(db_session, user, SOLO_KOPECKS, models.BalanceTxKind.topup, "invoice:1")

    resp = _activate_full(client, user)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Trial already used"
    db_session.expire_all()
    assert (
        db_session.query(models.Subscription)
        .filter(models.Subscription.user_id == user.id)
        .count()
        == 1
    )
    # Деньги не тронуты: продление — через /renew.
    assert db_session.get(models.User, user.id).balance_kopecks == SOLO_KOPECKS
    assert _rejections(db_session, user.id) == ["already_used"]


def test_live_subscription_is_409(client, db_session: Session) -> None:
    _infra(db_session)
    user = make_user(db_session, telegram_id="t2-live")
    assert _activate_full(client, user).status_code == 200

    resp = _activate_full(client, user)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "User already has a live subscription"
    assert _rejections(db_session, user.id) == ["live"]


# ── 8. activate_subscription без новых параметров ───────────────────


def test_activate_subscription_defaults_unchanged(db_session: Session) -> None:
    plan = _plan(db_session)
    node = make_node(db_session)
    user = make_user(db_session, telegram_id="t8")
    _tx(db_session, user, 20000, models.BalanceTxKind.topup, "invoice:8")
    sub = make_subscription(db_session, user, plan, node)
    before = utcnow()

    charged = balance_svc.activate_subscription(
        db_session, user.id, sub, reference=f"activate:{sub.id}"
    )
    db_session.commit()

    assert charged == SOLO_KOPECKS
    assert _near(sub.expires_at, before + timedelta(days=30))
    assert db_session.get(models.User, user.id).balance_kopecks == 5000


def test_activate_subscription_with_price_and_expiry(db_session: Session) -> None:
    plan = _plan(db_session)
    node = make_node(db_session)
    user = make_user(db_session, telegram_id="t8b")
    _tx(db_session, user, 1500, models.BalanceTxKind.bonus, f"trial:{user.id}")
    sub = make_subscription(db_session, user, plan, node)
    until = utcnow() + timedelta(days=4)

    charged = balance_svc.activate_subscription(
        db_session, user.id, sub, reference=f"trial-full:{sub.id}",
        price_kopecks=1500, expires_at=until,
    )
    db_session.commit()

    assert charged == 1500
    assert sub.expires_at == until
    assert db_session.get(models.User, user.id).balance_kopecks == 0


# ── 9. Отказы через эндпоинт: бонус откатывается ─────────────────────


def test_provision_failure_rolls_back_bonus_and_retry_works(
    client, db_session: Session
) -> None:
    plan = _plan(db_session)  # плана хватает, а нод нет: RuntimeError провижининга
    user = make_user(db_session, telegram_id="t9")

    resp = _activate_full(client, user)

    assert resp.status_code == 409, resp.text
    db_session.expire_all()
    fresh = db_session.get(models.User, user.id)
    assert fresh.trial_activated_at is None
    assert fresh.trial_expires_at is None
    assert fresh.balance_kopecks == 0
    assert _ledger(db_session, f"trial:{user.id}") == []
    assert _rejections(db_session, user.id) == ["provision_failed"]

    node = make_node(db_session)
    make_config(db_session, node)
    retry = _activate_full(client, user)
    assert retry.status_code == 200, retry.text
    assert retry.json()["plan_name"] == plan.name


def test_cold_path_throttle_is_503_and_rolls_back(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _infra(db_session)
    user = make_user(db_session, telegram_id="t9-busy")

    def _busy(self, *args, **kwargs):
        raise ColdPathThrottled(42)

    monkeypatch.setattr(ProvisioningOrchestrator, "provision_subscription", _busy)
    resp = _activate_full(client, user)

    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "42"
    db_session.expire_all()
    assert db_session.get(models.User, user.id).trial_activated_at is None
    assert _ledger(db_session, f"trial:{user.id}") == []
    assert _rejections(db_session, user.id) == ["throttled"]


def test_queue_down_after_cold_commit_still_charges_trial(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью 30.09: очередь провижна упала ПОСЛЕ коммита cold-пути. Раньше
    RuntimeError вылетал наружу — подписка оставалась без списания trial-full
    и без автопродления («деньги на балансе, VPN нет»). Теперь таска остаётся
    pending для rescue-тика, триал выдаётся целиком."""
    _infra(db_session)
    user = make_user(db_session, telegram_id="t9-queue")

    def _queue_down(self, task, node=None):
        raise RuntimeError("Provisioning queue is unavailable")

    monkeypatch.setattr(ProvisioningOrchestrator, "run_task_async", _queue_down)
    resp = _activate_full(client, user)

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    sub = db_session.query(models.Subscription).filter_by(user_id=user.id).one()
    assert sub.auto_renew is True
    assert len(_ledger(db_session, f"trial-full:{sub.id}")) == 1
    task = (
        db_session.query(models.ProvisioningTask)
        .filter(models.ProvisioningTask.action == "apply")
        .order_by(models.ProvisioningTask.id.desc())
        .first()
    )
    assert task is not None and task.status == models.ProvisioningTaskStatus.pending


def test_live_refusal_leaves_no_bonus_for_payer(client, db_session: Session) -> None:
    """Платящий жмёт кнопку в боте: 409, и ни бонуса, ни таймера триала."""
    plan = _infra(db_session)
    node = db_session.query(models.VPNNode).first()
    user = make_user(db_session, telegram_id="t9-payer")
    make_subscription(db_session, user, plan, node)

    resp = _activate_full(client, user)

    assert resp.status_code == 409
    db_session.expire_all()
    fresh = db_session.get(models.User, user.id)
    assert fresh.trial_activated_at is None
    assert fresh.trial_expires_at is None
    assert fresh.balance_kopecks == 0
    assert _ledger(db_session, f"trial:{user.id}") == []


def test_no_trial_plan_is_503(client, db_session: Session) -> None:
    user = make_user(db_session, telegram_id="t9-noplan")
    resp = _activate_full(client, user)
    assert resp.status_code == 503
    assert _rejections(db_session, user.id) == ["no_trial_plan"]


# ── 10. Бонус без подписки и исторический путь ──────────────────────


def test_legacy_bonus_then_activate_full_gives_3_days(client, db_session: Session) -> None:
    _infra(db_session)
    user = make_user(db_session, telegram_id="t10")
    legacy = client.post(
        "/api/trial/activate", json={"telegram_id": user.telegram_id}, headers=_ADMIN
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["trial_amount_kopecks"] == 1500
    before = utcnow()

    resp = _activate_full(client, user)

    assert resp.status_code == 200, resp.text
    assert resp.json()["trial_days"] == 3
    db_session.expire_all()
    sub = db_session.get(models.Subscription, resp.json()["subscription_id"])
    assert _near(sub.expires_at, before + timedelta(days=3, hours=24))
    assert [r.amount_kopecks for r in _ledger(db_session, f"trial-full:{sub.id}")] == [-1500]
    fresh = db_session.get(models.User, user.id)
    assert fresh.balance_kopecks == 0
    assert fresh.trial_expires_at == sub.expires_at


def _stuck_old_trial(db: Session, *, balance: int) -> models.User:
    """Старый триал: бонус 150 ₽ зачислен, подписки не было («застрял на бонусе»)."""
    user = make_user(db, telegram_id=f"t10-old-{balance}")
    now = utcnow()
    user.trial_activated_at = now - timedelta(days=2)
    user.trial_expires_at = now + timedelta(days=28)
    db.add(user)
    db.commit()
    _tx(db, user, SOLO_KOPECKS, models.BalanceTxKind.bonus, f"trial:{user.id}")
    if balance != SOLO_KOPECKS:
        _tx(db, user, balance - SOLO_KOPECKS, models.BalanceTxKind.adjust, "clawback:x")
    return user


def test_historical_stuck_bonus_buys_full_month(client, db_session: Session) -> None:
    _infra(db_session)
    user = _stuck_old_trial(db_session, balance=SOLO_KOPECKS)
    old_timer = db_session.get(models.User, user.id).trial_expires_at
    before = utcnow()

    resp = _activate_full(client, user)

    assert resp.status_code == 200, resp.text
    assert resp.json()["trial_days"] is None
    db_session.expire_all()
    sub = db_session.get(models.Subscription, resp.json()["subscription_id"])
    assert _near(sub.expires_at, before + timedelta(days=30))
    assert [r.amount_kopecks for r in _ledger(db_session, f"trial-full:{sub.id}")] == [
        -SOLO_KOPECKS
    ]
    # Таймер старого триала не трогаем.
    assert db_session.get(models.User, user.id).trial_expires_at == old_timer


def test_historical_path_short_balance_is_402_without_subscription(
    client, db_session: Session
) -> None:
    _infra(db_session)
    user = _stuck_old_trial(db_session, balance=0)

    resp = _activate_full(client, user)

    assert resp.status_code == 402, resp.text
    db_session.expire_all()
    # Баланс проверен до провижининга: выданной и тут же погашенной подписки нет.
    assert (
        db_session.query(models.Subscription)
        .filter(models.Subscription.user_id == user.id)
        .count()
        == 0
    )
    assert _rejections(db_session, user.id) == ["insufficient"]


def test_restored_user_without_bonus_row_is_already_used(
    client, db_session: Session
) -> None:
    """generate_restore_sql.py ставит trial_activated_at без записи бонуса."""
    _infra(db_session)
    user = make_user(db_session, telegram_id="t10-restored")
    user.trial_activated_at = utcnow()
    user.balance_kopecks = SOLO_KOPECKS
    db_session.commit()

    resp = _activate_full(client, user)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Trial already used"


# ── 7. Пополнение во время триала → та же подписка продлевается ─────


def test_topup_during_trial_renews_same_subscription(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _infra(db_session)
    user = make_user(db_session, telegram_id="t7")
    resp = _activate_full(client, user)
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    sub = db_session.get(models.Subscription, resp.json()["subscription_id"])
    link_token = sub.link_token
    assert link_token
    user = db_session.get(models.User, user.id)
    _tx(db_session, user, SOLO_KOPECKS, models.BalanceTxKind.topup, "invoice:7")
    # Триал кончился: срок в прошлом.
    old_expiry = utcnow() - timedelta(minutes=1)
    sub.expires_at = old_expiry
    db_session.commit()

    monkeypatch.setattr("app.queue.schedule_tick", lambda *a, **k: None)
    worker.run_renewal_check()

    db_session.expire_all()
    sub = db_session.get(models.Subscription, sub.id)
    assert sub.status == models.SubscriptionStatus.active
    assert sub.expires_at == old_expiry + timedelta(days=30)
    assert sub.link_token == link_token
    assert [r.amount_kopecks for r in _ledger(db_session, f"renew:{sub.id}")] == [
        -SOLO_KOPECKS
    ]
    assert db_session.get(models.User, user.id).balance_kopecks == 0
    assert balance_svc.is_unpaid_trial(db_session, sub) is False


# ── 4. Заморозка только после первой оплаты ─────────────────────────


def _trial_sub(db: Session, tg: str) -> tuple[models.User, models.Subscription]:
    """Подписка, купленная на триальный бонус (spend trial-full)."""
    plan = db.query(models.Plan).filter_by(name="Solo").first() or _plan(db)
    node = db.query(models.VPNNode).first() or make_node(db)
    user = make_user(db, telegram_id=tg)
    user.trial_activated_at = utcnow()
    db.add(user)
    db.commit()
    _tx(db, user, 1500, models.BalanceTxKind.bonus, f"trial:{user.id}")
    sub = make_subscription(db, user, plan, node)
    sub.auto_renew = True
    db.commit()
    _tx(db, user, -1500, models.BalanceTxKind.spend, f"trial-full:{sub.id}")
    return user, sub


def _old_cabinet_sub(db: Session, tg: str) -> tuple[models.User, models.Subscription]:
    """Старый кабинетный триал: бонус потрачен через activate:{sub.id}."""
    plan = db.query(models.Plan).filter_by(name="Solo").first() or _plan(db)
    node = db.query(models.VPNNode).first() or make_node(db)
    user = make_user(db, telegram_id=tg)
    user.trial_activated_at = utcnow()
    db.add(user)
    db.commit()
    _tx(db, user, SOLO_KOPECKS, models.BalanceTxKind.bonus, f"trial:{user.id}")
    sub = make_subscription(db, user, plan, node)
    sub.auto_renew = True
    db.commit()
    _tx(db, user, -SOLO_KOPECKS, models.BalanceTxKind.spend, f"activate:{sub.id}")
    return user, sub


def _can_freeze(client, user: models.User, sub: models.Subscription) -> bool:
    res = client.get("/api/webapp/me", headers=_auth(user.id))
    assert res.status_code == 200, res.text
    extras = {e["subscription_id"]: e for e in res.json()["subscription_extras"]}
    return extras[sub.id]["can_freeze"]


def _freeze(client, user: models.User, sub: models.Subscription):
    return client.post(
        f"/api/webapp/subscriptions/{sub.id}/freeze", headers=_auth(user.id)
    )


def _paid_invoice(
    db: Session, user: models.User, sub: models.Subscription | None, *, amount: int = 150
) -> models.Invoice:
    plan = db.query(models.Plan).filter_by(name="Solo").first()
    inv = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        subscription_id=sub.id if sub is not None else None,
        amount=amount,
        currency="RUB",
        status=models.InvoiceStatus.paid,
        action=models.InvoiceAction.renewal,
        kind="subscription",
    )
    db.add(inv)
    db.commit()
    return inv


def test_unpaid_trial_cannot_freeze(client, db_session: Session) -> None:
    user, sub = _trial_sub(db_session, "t4-trial")

    assert _can_freeze(client, user, sub) is False
    resp = _freeze(client, user, sub)
    assert resp.status_code == 400
    assert resp.json()["detail"] == balance_svc.FREEZE_NEEDS_PAYMENT_DETAIL
    db_session.expire_all()
    assert db_session.get(models.Subscription, sub.id).status == models.SubscriptionStatus.active


def test_trial_with_partial_topup_still_cannot_freeze(client, db_session: Session) -> None:
    """Пополнил, но на продление не хватает: сама подписка ещё триал —
    заморозка растянула бы бесплатные дни на неделю."""
    user, sub = _trial_sub(db_session, "t4-topup")
    _tx(db_session, user, SOLO_KOPECKS - 5000, models.BalanceTxKind.topup, "invoice:41")

    assert _can_freeze(client, user, sub) is False
    assert _freeze(client, user, sub).status_code == 400


def test_trial_with_balance_covering_renewal_can_freeze(client, db_session: Session) -> None:
    """Решение владельца 30.09 (user 1000058: 1500 ₽ на балансе на триальной
    подписке): деньги на продление уже лежат — человек фактически купил,
    заморозку показываем."""
    user, sub = _trial_sub(db_session, "t4-topup-full")
    _tx(db_session, user, SOLO_KOPECKS, models.BalanceTxKind.topup, "invoice:42")

    assert _can_freeze(client, user, sub) is True
    assert _freeze(client, user, sub).status_code == 200


def test_old_cabinet_trial_without_payment_cannot_freeze(client, db_session: Session) -> None:
    user, sub = _old_cabinet_sub(db_session, "t4-old")
    assert _can_freeze(client, user, sub) is False


@pytest.mark.parametrize("payment", ["invoice", "admin_topup", "renew", "restored"])
def test_payers_can_freeze(client, db_session: Session, payment: str) -> None:
    if payment == "restored":
        # Восстановленный: ни журнала, ни счетов, баланс прямо в колонке.
        plan = _plan(db_session)
        node = make_node(db_session)
        user = make_user(db_session, telegram_id="t4-restored")
        user.trial_activated_at = utcnow()
        user.balance_kopecks = 5000
        db_session.commit()
        sub = make_subscription(db_session, user, plan, node)
        sub.auto_renew = True
        db_session.commit()
    else:
        user, sub = _old_cabinet_sub(db_session, f"t4-{payment}")
        if payment == "invoice":
            _paid_invoice(db_session, user, None)
        elif payment == "admin_topup":
            _tx(db_session, user, SOLO_KOPECKS, models.BalanceTxKind.adjust, "admin_topup:1")
        else:
            _tx(db_session, user, SOLO_KOPECKS, models.BalanceTxKind.topup, "invoice:9")
            _tx(db_session, user, -SOLO_KOPECKS, models.BalanceTxKind.spend, f"renew:{sub.id}")

    assert _can_freeze(client, user, sub) is True
    resp = _freeze(client, user, sub)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "frozen"


def test_trial_paid_by_card_can_freeze_and_is_not_on_trial(
    client, db_session: Session
) -> None:
    """Триал продлён renewal-счётом картой: подписка больше не триал."""
    user, sub = _trial_sub(db_session, "t4-card")
    family = _plan(db_session, name="Family", price=300, max_devices=3)
    _paid_invoice(db_session, user, sub)

    assert _can_freeze(client, user, sub) is True
    resp = client.post(
        "/api/invoices",
        json={"telegram_id": user.telegram_id, "plan_id": family.id},
        headers=_NO_ADMIN,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] != ON_TRIAL_DETAIL


def test_trial_after_change_plan_can_freeze_and_is_not_on_trial(
    client, db_session: Session
) -> None:
    user, sub = _trial_sub(db_session, "t4-change")
    family = _plan(db_session, name="Family", price=300, max_devices=3)
    # Пополнил и сменил тариф в кабинете: тот же sub.id, запись change_plan.
    _tx(db_session, user, SOLO_KOPECKS, models.BalanceTxKind.topup, "invoice:42")
    _tx(db_session, user, -SOLO_KOPECKS, models.BalanceTxKind.spend, f"change_plan:{sub.id}")

    assert _can_freeze(client, user, sub) is True
    resp = client.post(
        "/api/invoices",
        json={"telegram_id": user.telegram_id, "plan_id": family.id},
        headers=_NO_ADMIN,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] != ON_TRIAL_DETAIL


# ── 12. Другой тариф во время триала и воронка ──────────────────────


def test_other_plan_during_trial_is_409_on_trial(client, db_session: Session) -> None:
    _infra(db_session)
    family = _plan(db_session, name="Family", price=300, max_devices=3)
    user = make_user(db_session, telegram_id="t12")
    assert _activate_full(client, user).status_code == 200

    resp = client.post(
        "/api/invoices",
        json={"telegram_id": user.telegram_id, "plan_id": family.id},
        headers=_NO_ADMIN,
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == ON_TRIAL_DETAIL
    assert db_session.query(models.Invoice).count() == 0


def test_same_plan_during_trial_becomes_renewal(client, db_session: Session) -> None:
    """/plans → Solo во время триала — счёт на продление той же подписки."""
    solo = _infra(db_session)
    user = make_user(db_session, telegram_id="t12-solo")
    sub_id = _activate_full(client, user).json()["subscription_id"]

    resp = client.post(
        "/api/invoices",
        json={"telegram_id": user.telegram_id, "plan_id": solo.id},
        headers=_NO_ADMIN,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "renewal"
    assert resp.json()["subscription_id"] == sub_id


def test_funnel_counts_only_real_failures(db_session: Session) -> None:
    reasons = {
        "live": "f-live",
        "already_used": "f-used",
        "already_activated": "f-act",
        "provision_failed": "f-prov",
        "throttled": "f-busy",
    }
    for reason, tg in reasons.items():
        user = make_user(db_session, telegram_id=tg)
        db_session.add(
            models.AuditLog(
                actor=f"user:{user.id}", action="trial_activate_rejected",
                target_type="user", target_id=user.id,
                extra={"reason": reason}, actor_type=models.AuditActor.user,
            )
        )
    db_session.commit()

    data = onboarding_funnel.compute(db_session, days=None)

    assert data["trial_failures"] == 2


# ── 11. Награда рефереру за первую оплату любым способом ────────────


def _mark_paid(client, invoice_id: int):
    resp = client.post(f"/api/invoices/{invoice_id}/mark_paid", headers=_ADMIN)
    assert resp.status_code == 200, resp.text
    return resp


def _topup_invoice(db: Session, user: models.User, rub: int) -> int:
    inv = models.Invoice(
        user_id=user.id, plan_id=None, amount=rub, currency="RUB", kind="topup"
    )
    db.add(inv)
    db.commit()
    return inv.id


def _payouts(db: Session, invitee: models.User) -> list[models.BalanceTransaction]:
    return _ledger(db, f"referral_payout:{invitee.id}")


def test_renewal_invoice_by_card_pays_referrer_once(client, db_session: Session) -> None:
    solo = _infra(db_session)
    invitee = make_user(db_session, telegram_id="t11")
    owner = _invite(db_session, invitee)
    assert _activate_full(client, invitee).status_code == 200

    # /plans → Solo во время триала: renewal-счёт, оплата картой.
    created = client.post(
        "/api/invoices",
        json={"telegram_id": invitee.telegram_id, "plan_id": solo.id},
        headers=_NO_ADMIN,
    )
    assert created.status_code == 200, created.text
    _mark_paid(client, created.json()["id"])

    db_session.expire_all()
    payouts = _payouts(db_session, invitee)
    assert len(payouts) == 1
    assert payouts[0].user_id == owner.id
    # 10 дней по прайсу Solo = 50 ₽.
    assert payouts[0].amount_kopecks == 5000

    # Следующее пополнение награду не повторяет.
    _mark_paid(client, _topup_invoice(db_session, invitee, 150))
    db_session.expire_all()
    assert len(_payouts(db_session, invitee)) == 1


def test_new_subscription_invoice_pays_referrer(client, db_session: Session) -> None:
    """Первая оплата — покупка тарифа без триала."""
    solo = _infra(db_session)
    invitee = make_user(db_session, telegram_id="t11-buy")
    owner = _invite(db_session, invitee)
    inv = models.Invoice(
        user_id=invitee.id, plan_id=solo.id, amount=150, currency="RUB",
        action=models.InvoiceAction.new_subscription,
    )
    db_session.add(inv)
    db_session.commit()

    _mark_paid(client, inv.id)

    db_session.expire_all()
    payouts = _payouts(db_session, invitee)
    assert [p.user_id for p in payouts] == [owner.id]


@pytest.mark.parametrize("earlier", ["invoice", "topup_row", "admin_topup"])
def test_payer_bound_to_referrer_later_brings_no_payout(
    client, db_session: Session, earlier: str
) -> None:
    """Платил до привязки реферала: /users/register привязывает реферера любому
    юзеру с пустым полем, даже давно платящему. Награды за него нет."""
    solo = _infra(db_session)
    payer = make_user(db_session, telegram_id=f"t11-{earlier}")
    if earlier == "invoice":
        db_session.add(
            models.Invoice(
                user_id=payer.id, plan_id=solo.id, amount=150, currency="RUB",
                status=models.InvoiceStatus.paid,
                action=models.InvoiceAction.new_subscription,
            )
        )
        db_session.commit()
    elif earlier == "topup_row":
        _tx(db_session, payer, 10000, models.BalanceTxKind.topup, "invoice:legacy")
    else:
        _tx(db_session, payer, 10000, models.BalanceTxKind.adjust, "admin_topup:7")
    _invite(db_session, payer)

    _mark_paid(client, _topup_invoice(db_session, payer, 150))

    db_session.expire_all()
    assert _payouts(db_session, payer) == []


def test_zero_amount_invoice_brings_no_payout(client, db_session: Session) -> None:
    solo = _infra(db_session)
    invitee = make_user(db_session, telegram_id="t11-zero")
    _invite(db_session, invitee)
    inv = models.Invoice(
        user_id=invitee.id, plan_id=solo.id, amount=0, currency="RUB",
        action=models.InvoiceAction.new_subscription,
    )
    db_session.add(inv)
    db_session.commit()

    _mark_paid(client, inv.id)

    db_session.expire_all()
    assert _payouts(db_session, invitee) == []
