"""Бот-путь подарка: /api/trial/activate_full и ownership auto_renew.

Ревью 2026-08-21: одношаговый /api/trial/activate давал юзеру только
бонус на баланс, а потратить его в боте нечем — человек застревал на
деньгах без VPN (та же воронка-ловушка из анализа 2026-07). activate_full
делает оба шага веб-флоу одним вызовом. Плюс IDOR: auto_renew без
сверки владельца выключался подделанным callback_data.
"""
from __future__ import annotations

import os

import pytest

from app import models
from app.services import provisioning_throttle

from .factories import make_config, make_node, make_plan, make_subscription, make_user


@pytest.fixture(autouse=True)
def _reset_cold_throttle():
    """Cold-path троттл — глобальный in-memory bucket на процесс: наши
    провижининги съедали бюджет соседних файлов (порядок-зависимые
    падения вебхук-тестов)."""
    provisioning_throttle.reset_for_tests()
    yield
    provisioning_throttle.reset_for_tests()


_ADMIN = {"X-Admin-Token": os.environ.get("ADMIN_API_TOKEN", "")}


def test_activate_full_grants_bonus_and_subscription(client, db_session):
    node = make_node(db_session)
    make_config(db_session, node)
    make_plan(db_session)  # видимый 30-дневный — и триал-база, и цель
    user = make_user(db_session)

    resp = client.post(
        "/api/trial/activate_full",
        json={"telegram_id": user.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 200, resp.text
    sub_id = resp.json()["subscription_id"]

    db_session.expire_all()
    sub = db_session.get(models.Subscription, sub_id)
    assert sub is not None and sub.user_id == user.id
    assert sub.status == models.SubscriptionStatus.active

    # Повторный тап: подписка уже живая — 409, второй не создаётся.
    resp = client.post(
        "/api/trial/activate_full",
        json={"telegram_id": user.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 409, resp.text
    subs = (
        db_session.query(models.Subscription)
        .filter(models.Subscription.user_id == user.id)
        .count()
    )
    assert subs == 1


def test_activate_full_finishes_stuck_bonus(client, db_session):
    """Юзер активировал бонус в ЛК и застрял (подписки нет) — бот-тап
    обязан ДОДЕЛАТЬ второй шаг, а не отвечать «уже активирован»."""
    node = make_node(db_session)
    make_config(db_session, node)
    make_plan(db_session)
    user = make_user(db_session)

    first = client.post(
        "/api/trial/activate",
        json={"telegram_id": user.telegram_id},
        headers=_ADMIN,
    )
    assert first.status_code == 200, first.text

    resp = client.post(
        "/api/trial/activate_full",
        json={"telegram_id": user.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 200, resp.text


def test_trial_activate_requires_admin_token(client, db_session):
    """Ревью 2026-08-21: без токена можно было сжечь чужой триал."""
    user = make_user(db_session)
    for path in ("/api/trial/activate", "/api/trial/activate_full"):
        resp = client.post(
            path,
            json={"telegram_id": user.telegram_id},
            headers={"X-Admin-Token": ""},
        )
        assert resp.status_code in (401, 403), (path, resp.text)


def test_auto_renew_checks_owner(client, db_session):
    """IDOR: подделанный callback_data не должен трогать чужую подписку."""
    plan = make_plan(db_session)
    node = make_node(db_session)
    make_config(db_session, node)
    owner = make_user(db_session, telegram_id="owner-tg")
    stranger = make_user(db_session, telegram_id="stranger-tg")
    sub = make_subscription(db_session, owner, plan, node)

    url = f"/api/subscriptions/{sub.id}/auto_renew"
    # Чужой telegram_id — 404, состояние не меняется.
    resp = client.post(
        url,
        json={"auto_renew": False, "telegram_id": stranger.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 404, resp.text

    # Владелец — 200.
    resp = client.post(
        url,
        json={"auto_renew": False, "telegram_id": owner.telegram_id},
        headers=_ADMIN,
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(models.Subscription, sub.id).auto_renew is False

    # Без telegram_id (админка) — прежнее поведение.
    resp = client.post(url, json={"auto_renew": True}, headers=_ADMIN)
    assert resp.status_code == 200, resp.text
