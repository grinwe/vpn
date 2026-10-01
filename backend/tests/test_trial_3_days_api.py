"""Триал 3 дня: API кабинета и поля для текстов (docs/operations/trial_3_days_plan_2026_09_30.md, Б2).

Кабинетные части тестов плана 1, 3 и 9: ``/webapp/trial/activate`` выдаёт
подписку на бесплатные дни на сервере (тем же сервисом, что бот), при живой
подписке даёт только бонус без таймера (В3); ``/register``, ``/me``,
``/referral/code`` и ``/webapp/referral`` отдают дни для текстов; отказы
откатывают бонус до записи аудита.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app import models
from app.api_webapp import issue_token
from app.config import get_settings
from app.services import provisioning_throttle
from app.services import trial as trial_svc
from app.services.provisioning import ProvisioningOrchestrator
from app.services.provisioning_throttle import ColdPathThrottled
from app.time_utils import utcnow

from .factories import make_config, make_node, make_subscription, make_user

_ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_API_TOKEN", "")}


@pytest.fixture(autouse=True)
def _reset_cold_throttle():
    """Cold-path троттл — глобальный in-memory bucket на процесс."""
    provisioning_throttle.reset_for_tests()
    yield
    provisioning_throttle.reset_for_tests()


# ── фикстуры ─────────────────────────────────────────────────────────


def _plan(db: Session) -> models.Plan:
    """Solo 150 ₽ / 30 дней: 3 дня = 15 ₽."""
    plan = models.Plan(
        name="Solo", duration_days=30, max_devices=1, price=150, is_visible=True
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _infra(db: Session) -> models.Plan:
    node = make_node(db)
    make_config(db, node)
    return _plan(db)


def _invite(db: Session, user: models.User, *, bonus_days: int = 3) -> models.ReferralCode:
    owner = make_user(db, telegram_id=f"owner-{user.telegram_id}")
    code = models.ReferralCode(
        owner_id=owner.id, code=f"code-{user.id}", bonus_days=bonus_days,
        reward_days=10,
    )
    db.add(code)
    user.referred_by_id = owner.id
    db.add(user)
    db.commit()
    return code


def _auth(user_id: int) -> dict:
    token = issue_token(user_id, get_settings().webapp_jwt_secret, 600)
    return {"Authorization": f"Bearer {token}"}


def _activate(client, user: models.User):
    return client.post("/api/webapp/trial/activate", headers=_auth(user.id))


def _ledger(db: Session, reference: str) -> list[models.BalanceTransaction]:
    return (
        db.query(models.BalanceTransaction)
        .filter(models.BalanceTransaction.reference == reference)
        .all()
    )


def _audits(db: Session, user_id: int, action: str) -> list[dict]:
    return [
        row.extra or {}
        for row in db.query(models.AuditLog)
        .filter(
            models.AuditLog.action == action,
            models.AuditLog.target_id == user_id,
        )
        .all()
    ]


def _subs(db: Session, user_id: int) -> list[models.Subscription]:
    return (
        db.query(models.Subscription)
        .filter(models.Subscription.user_id == user_id)
        .all()
    )


def _near(a, b, *, seconds: int = 120) -> bool:
    return abs((a - b).total_seconds()) < seconds


# ── 1. Триал на 3 и на 6 дней (кабинет) ──────────────────────────────


@pytest.mark.parametrize(
    ("invited", "total_days", "charged"),
    [(False, 3, 1500), (True, 6, 3000)],
)
def test_webapp_trial_gives_subscription_for_trial_days(
    client, db_session: Session, invited: bool, total_days: int, charged: int
) -> None:
    _infra(db_session)
    user = make_user(db_session, telegram_id=f"w1-{invited}")
    if invited:
        _invite(db_session, user)
    before = utcnow()

    resp = _activate(client, user)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trial_days"] == 3
    assert data["referral_days"] == (3 if invited else 0)
    assert data["trial_amount_kopecks"] == 1500
    assert data["referral_bonus_kopecks"] == (1500 if invited else 0)
    assert data["balance_kopecks"] == 0
    # Ссылка прямо в ответе: кабинет показывает экран «Готово» без второго шага.
    assert data["subscription_id"] is not None
    assert data["sub_token"]
    assert data["sub_url"]

    db_session.expire_all()
    sub = db_session.get(models.Subscription, data["subscription_id"])
    user = db_session.get(models.User, user.id)
    assert _near(sub.expires_at, before + timedelta(days=total_days, hours=24))
    assert sub.auto_renew is True
    spend = _ledger(db_session, f"trial-full:{sub.id}")
    assert [row.amount_kopecks for row in spend] == [-charged]
    assert user.balance_kopecks == 0
    assert user.trial_expires_at == sub.expires_at
    assert data["trial_expires_at"] == sub.expires_at.isoformat()
    assert len(_subs(db_session, user.id)) == 1

    [audit] = _audits(db_session, user.id, "trial_activated")
    assert audit["full"] is True
    assert audit["trial_days"] == 3
    assert audit["referral_days"] == (3 if invited else 0)
    assert audit["subscription_id"] == sub.id
    assert audit["charged_kopecks"] == charged


def test_webapp_trial_link_matches_cabinet_card(client, db_session: Session) -> None:
    """Ссылка из ответа — та же, что покажет карточка подписки в /me."""
    _infra(db_session)
    user = make_user(db_session, telegram_id="w1-link")

    data = _activate(client, user).json()

    me = client.get("/api/webapp/me", headers=_auth(user.id)).json()
    card = next(s for s in me["subscriptions"] if s["id"] == data["subscription_id"])
    assert data["sub_token"] in {card.get("link_token"), card.get("sub_token")}


# ── В3. Бонус-онли при живой подписке ────────────────────────────────


def test_webapp_trial_with_live_sub_gives_only_bonus(client, db_session: Session) -> None:
    """Платящий забирает подарок: 15 ₽ на баланс, подписка прежняя, таймера
    триала нет — значит, ни «бесплатные дни скоро закончатся», ни возврата."""
    plan = _infra(db_session)
    node = db_session.query(models.VPNNode).first()
    user = make_user(db_session, telegram_id="w-bonus-only")
    live = make_subscription(db_session, user, plan, node)

    me = client.get("/api/webapp/me", headers=_auth(user.id)).json()["balance"]
    assert me["trial_bonus_only"] is True
    assert me["trial_autoactivate_allowed"] is False

    resp = _activate(client, user)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["subscription_id"] is None
    assert data["sub_token"] is None and data["sub_url"] is None
    assert data["trial_expires_at"] is None
    assert data["balance_kopecks"] == 1500
    db_session.expire_all()
    fresh = db_session.get(models.User, user.id)
    assert fresh.trial_activated_at is not None
    assert fresh.trial_expires_at is None
    assert fresh.balance_kopecks == 1500
    assert [s.id for s in _subs(db_session, user.id)] == [live.id]
    [audit] = _audits(db_session, user.id, "trial_activated")
    assert audit["full"] is False
    assert audit["trial_days"] == 3

    # Повторный тап: 409, отказ в аудите.
    again = _activate(client, user)
    assert again.status_code == 409
    assert [a["reason"] for a in _audits(db_session, user.id, "trial_activate_rejected")] == [
        "already_activated"
    ]


# ── 2. Повторный триал (кабинет) ─────────────────────────────────────


def test_webapp_repeat_after_trial_expired_is_already_used(
    client, db_session: Session
) -> None:
    _infra(db_session)
    user = make_user(db_session, telegram_id="w2")
    first = _activate(client, user)
    assert first.status_code == 200, first.text

    db_session.expire_all()
    sub = db_session.get(models.Subscription, first.json()["subscription_id"])
    sub.status = models.SubscriptionStatus.expired
    db_session.commit()

    resp = _activate(client, user)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Trial already used"
    db_session.expire_all()
    assert len(_subs(db_session, user.id)) == 1
    rejected = _audits(db_session, user.id, "trial_activate_rejected")
    assert [(a["reason"], a["source"]) for a in rejected] == [("already_used", "webapp")]


def test_webapp_spends_bonus_granted_without_subscription(
    client, db_session: Session
) -> None:
    """Бонус, выданный legacy ``/api/trial/activate`` без подписки, кабинет
    тратит на бесплатные дни, а не отвечает «уже забран»."""
    _infra(db_session)
    user = make_user(db_session, telegram_id="w2-legacy")
    legacy = client.post(
        "/api/trial/activate", json={"telegram_id": user.telegram_id}, headers=_ADMIN
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["trial_amount_kopecks"] == 1500

    resp = _activate(client, user)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trial_days"] == 3
    db_session.expire_all()
    assert db_session.get(models.User, user.id).balance_kopecks == 0
    assert data["trial_amount_kopecks"] == 1500


# ── 9. Отказы через эндпоинт кабинета: бонус откатывается ───────────


def test_webapp_provision_failure_rolls_back_bonus_and_retry_works(
    client, db_session: Session
) -> None:
    _plan(db_session)  # плана хватает, а нод нет: RuntimeError провижининга
    user = make_user(db_session, telegram_id="w9")

    resp = _activate(client, user)

    assert resp.status_code == 409, resp.text
    db_session.expire_all()
    fresh = db_session.get(models.User, user.id)
    assert fresh.trial_activated_at is None
    assert fresh.trial_expires_at is None
    assert fresh.balance_kopecks == 0
    assert _ledger(db_session, f"trial:{user.id}") == []
    rejected = _audits(db_session, user.id, "trial_activate_rejected")
    assert [(a["reason"], a["source"]) for a in rejected] == [("provision_failed", "webapp")]

    node = make_node(db_session)
    make_config(db_session, node)
    retry = _activate(client, user)
    assert retry.status_code == 200, retry.text
    assert retry.json()["subscription_id"] is not None


def test_webapp_cold_path_throttle_is_503_and_rolls_back(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _infra(db_session)
    user = make_user(db_session, telegram_id="w9-busy")

    def _busy(self, *args, **kwargs):
        raise ColdPathThrottled(42)

    monkeypatch.setattr(ProvisioningOrchestrator, "provision_subscription", _busy)
    resp = _activate(client, user)

    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "42"
    db_session.expire_all()
    assert db_session.get(models.User, user.id).trial_activated_at is None
    assert _ledger(db_session, f"trial:{user.id}") == []
    assert [a["reason"] for a in _audits(db_session, user.id, "trial_activate_rejected")] == [
        "throttled"
    ]


def test_webapp_live_race_leaves_no_bonus(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Живая подписка появилась между выбором ветки и проверкой в сервисе
    (двойной тап): 409 ``live``, и аудит отказа не закрепляет бонус."""
    plan = _infra(db_session)
    node = db_session.query(models.VPNNode).first()
    user = make_user(db_session, telegram_id="w9-race")
    original = trial_svc.activate_trial

    def _grant_then_parallel_sub(db, user_id, **kwargs):
        result = original(db, user_id, **kwargs)
        db.add(
            models.Subscription(
                user_id=user_id, plan_id=plan.id, node_id=node.id,
                expires_at=utcnow() + timedelta(days=30),
                status=models.SubscriptionStatus.active, traffic_used_mb=0,
            )
        )
        db.flush()
        return result

    monkeypatch.setattr(trial_svc, "activate_trial", _grant_then_parallel_sub)
    resp = _activate(client, user)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "User already has a live subscription"
    db_session.expire_all()
    fresh = db_session.get(models.User, user.id)
    assert fresh.trial_activated_at is None
    assert fresh.trial_expires_at is None
    assert fresh.balance_kopecks == 0
    assert _ledger(db_session, f"trial:{user.id}") == []
    assert [a["reason"] for a in _audits(db_session, user.id, "trial_activate_rejected")] == [
        "live"
    ]


def test_webapp_no_trial_plan_is_503(client, db_session: Session) -> None:
    user = make_user(db_session, telegram_id="w9-noplan")
    resp = _activate(client, user)
    assert resp.status_code == 503
    rejected = _audits(db_session, user.id, "trial_activate_rejected")
    assert [(a["reason"], a["source"]) for a in rejected] == [("no_trial_plan", "webapp")]


# ── 3. Дни для текстов: /register, /me, /referral/code ───────────────


def test_register_returns_trial_days(client, db_session: Session) -> None:
    _plan(db_session)
    owner = make_user(db_session, telegram_id="reg-owner")
    db_session.add(
        models.ReferralCode(owner_id=owner.id, code="REGREF", bonus_days=3, reward_days=10)
    )
    db_session.commit()

    plain = client.post("/api/users/register", json={"telegram_id": "reg-plain"})
    assert plain.status_code == 200, plain.text
    assert plain.json()["trial_days"] == 3
    assert plain.json()["trial_referral_days"] == 0

    # Реферал привязывается тем же запросом — дни считаются уже с ним.
    invited = client.post(
        "/api/users/register",
        json={"telegram_id": "reg-invited", "referral_code": "REGREF"},
    )
    assert invited.status_code == 200, invited.text
    assert invited.json()["trial_days"] == 3
    assert invited.json()["trial_referral_days"] == 3


def test_me_returns_trial_days(client, db_session: Session) -> None:
    _plan(db_session)
    plain = make_user(db_session, telegram_id="me-plain")
    invited = make_user(db_session, telegram_id="me-invited")
    _invite(db_session, invited, bonus_days=5)

    b1 = client.get("/api/webapp/me", headers=_auth(plain.id)).json()["balance"]
    assert (b1["trial_days"], b1["trial_referral_days"]) == (3, 0)
    assert b1["trial_amount_kopecks"] == 1500
    assert b1["trial_bonus_only"] is False
    assert b1["trial_autoactivate_allowed"] is False

    b2 = client.get("/api/webapp/me", headers=_auth(invited.id)).json()["balance"]
    assert (b2["trial_days"], b2["trial_referral_days"]) == (3, 5)


def test_referral_code_returns_invitee_total_days(client, db_session: Session) -> None:
    user = make_user(db_session, telegram_id="ref-owner")

    resp = client.post(
        "/api/referral/code", json={"telegram_id": user.telegram_id}, headers=_ADMIN
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["invitee_total_days"] == 6

    code = (
        db_session.query(models.ReferralCode)
        .filter(models.ReferralCode.owner_id == user.id)
        .one()
    )
    code.bonus_days = 7
    db_session.commit()
    again = client.post(
        "/api/referral/code", json={"telegram_id": user.telegram_id}, headers=_ADMIN
    )
    assert again.json()["invitee_total_days"] == 10
