"""Аудит-фиксы api_webapp.py: находки №106 и №16.

№106 — POST /api/webapp/subscriptions/activate падал с AttributeError
(500) для любого пользователя с действующей подпиской: вызывались
несуществующие balance_svc.prorated_sub_refund_kopecks и
balance_svc.refund_subscription_remainder. Теперь оба хелпера
реализованы в api_webapp.py (_prorated_sub_refund_kopecks /
_refund_subscription_remainder).

№16 — гонка двойной активации: два параллельных запроса проходили
read-then-act проверку existing_subs и оба списывали деньги. Теперь
webapp_activate/webapp_change_plan берут FOR UPDATE-блокировку на
строку users до проверки.
"""
import threading
from datetime import timedelta

from app import models
from app.api_webapp import _prorated_sub_refund_kopecks, issue_token
from app.config import get_settings
from app.services import balance, provisioning_throttle
from app.time_utils import utcnow

from .factories import make_config, make_node, make_plan, make_user, \
    make_subscription_with_device

import pytest


@pytest.fixture(autouse=True)
def _fresh_throttle():
    """Cold-path трэттл глобальный на процесс — чистим между тестами,
    чтобы активации соседних тестов не выедали окно."""
    provisioning_throttle.reset_for_tests()
    yield
    provisioning_throttle.reset_for_tests()


def _auth_headers(user_id: int) -> dict:
    settings = get_settings()
    token = issue_token(user_id, settings.webapp_jwt_secret, 600)
    return {"Authorization": f"Bearer {token}"}


def _setup_node(db):
    node = make_node(db)
    make_config(db, node)
    return node


# ── №106: смена плана поверх активной подписки ───────────────────────

def test_activate_with_existing_sub_refunds_and_switches(client, db_session):
    """Пользователь с активной подпиской покупает другой план: 200,
    рефанд остатка в леджере, старая подписка blocked (до фикса — 500)."""
    node = _setup_node(db_session)
    plan_a = make_plan(db_session, name="plan-a")
    plan_b = make_plan(db_session, name="plan-b")
    user = make_user(db_session, telegram_id="tg-switch")
    balance.topup(db_session, user.id, 5000, reference="seed")
    old_sub = make_subscription_with_device(db_session, user, plan_a, node)
    db_session.commit()

    res = client.post(
        "/api/webapp/subscriptions/activate",
        json={"plan_id": plan_b.id},
        headers=_auth_headers(user.id),
    )
    assert res.status_code == 200, res.text
    data = res.json()

    # Рефанд остатка: ~30 из 30 дней плана за 1000 коп. (ε времени теста).
    assert 990 <= data["refunded_from_previous_kopecks"] <= 1000
    refund_rows = (
        db_session.query(models.BalanceTransaction)
        .filter(
            models.BalanceTransaction.user_id == user.id,
            models.BalanceTransaction.kind == models.BalanceTxKind.refund,
        )
        .all()
    )
    assert len(refund_rows) == 1
    assert refund_rows[0].reference == f"switch:{data['subscription_id']}:{old_sub.id}"
    assert refund_rows[0].amount_kopecks == data["refunded_from_previous_kopecks"]

    # Старая подписка терминирована, новая жива — single-sub инвариант.
    db_session.expire_all()
    old = db_session.get(models.Subscription, old_sub.id)
    assert old.status == models.SubscriptionStatus.blocked
    new = db_session.get(models.Subscription, data["subscription_id"])
    assert new.plan_id == plan_b.id
    assert new.status == models.SubscriptionStatus.active

    # Баланс: 5000 + рефанд − 1000 (цена нового плана).
    assert data["balance_kopecks"] == 5000 + data["refunded_from_previous_kopecks"] - 1000


def test_activate_with_expired_remainder_refunds_zero(client, db_session):
    """expires_at в прошлом → рефанд 0 и ни одной refund-строки в леджере."""
    node = _setup_node(db_session)
    plan_a = make_plan(db_session, name="plan-a")
    plan_b = make_plan(db_session, name="plan-b")
    user = make_user(db_session, telegram_id="tg-expired")
    balance.topup(db_session, user.id, 2000, reference="seed")
    old_sub = make_subscription_with_device(db_session, user, plan_a, node)
    old_sub.expires_at = utcnow() - timedelta(days=1)
    db_session.commit()

    res = client.post(
        "/api/webapp/subscriptions/activate",
        json={"plan_id": plan_b.id},
        headers=_auth_headers(user.id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["refunded_from_previous_kopecks"] == 0
    refund_count = (
        db_session.query(models.BalanceTransaction)
        .filter(
            models.BalanceTransaction.user_id == user.id,
            models.BalanceTransaction.kind == models.BalanceTxKind.refund,
        )
        .count()
    )
    assert refund_count == 0


def test_activate_same_plan_still_400(client, db_session):
    """Повторный клик по своему же плану — 400, не дубль подписки."""
    node = _setup_node(db_session)
    plan_a = make_plan(db_session, name="plan-a")
    user = make_user(db_session, telegram_id="tg-same")
    balance.topup(db_session, user.id, 5000, reference="seed")
    make_subscription_with_device(db_session, user, plan_a, node)
    db_session.commit()

    res = client.post(
        "/api/webapp/subscriptions/activate",
        json={"plan_id": plan_a.id},
        headers=_auth_headers(user.id),
    )
    assert res.status_code == 400
    assert "Already on plan" in res.text


def test_prorated_refund_capped_by_plan_price(db_session):
    """«Удлинённый» expires_at (freeze) не даёт рефанд больше цены плана."""
    node = _setup_node(db_session)
    plan = make_plan(db_session, name="plan-cap")
    user = make_user(db_session, telegram_id="tg-cap")
    sub = make_subscription_with_device(db_session, user, plan, node)
    # 60 оставшихся дней на 30-дневном плане (заморозка сдвинула срок).
    sub.expires_at = utcnow() + timedelta(days=60)
    db_session.commit()

    refund = _prorated_sub_refund_kopecks(sub)
    assert refund == balance.plan_price_kopecks(plan)  # кап, не 2x


# ── №16: гонка двойной активации ─────────────────────────────────────

def test_concurrent_activation_single_charge(client, db_session):
    """Два параллельных activate одного плана: ровно один 200, второй
    400 «Already on plan»; одна подписка и одно списание."""
    _setup_node(db_session)
    plan = make_plan(db_session, name="plan-race")
    user = make_user(db_session, telegram_id="tg-race")
    balance.topup(db_session, user.id, 5000, reference="seed")
    db_session.commit()

    headers = _auth_headers(user.id)
    barrier = threading.Barrier(2)
    results = []

    def _hit():
        barrier.wait()
        res = client.post(
            "/api/webapp/subscriptions/activate",
            json={"plan_id": plan.id},
            headers=headers,
        )
        results.append(res.status_code)

    threads = [threading.Thread(target=_hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sorted(results) == [200, 400], results

    db_session.expire_all()
    subs = (
        db_session.query(models.Subscription)
        .filter(models.Subscription.user_id == user.id)
        .all()
    )
    assert len(subs) == 1  # вторая активация не создала дубль

    spends = (
        db_session.query(models.BalanceTransaction)
        .filter(
            models.BalanceTransaction.user_id == user.id,
            models.BalanceTransaction.kind == models.BalanceTxKind.spend,
        )
        .all()
    )
    assert len(spends) == 1  # деньги списаны ровно один раз
    fresh_user = db_session.get(models.User, user.id)
    assert fresh_user.balance_kopecks == 5000 - 1000
