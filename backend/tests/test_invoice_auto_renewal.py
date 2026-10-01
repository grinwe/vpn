"""Авто-renewal: оплата «новой подписки» при уже живой подписке того же плана.

Инцидент 2026-08-21 (счёт #65): человек утром активировал триал (Solo),
днём оплатил через бота счёт с дефолтным ``action=new_subscription``.
Проведение падало в ``provision_subscription`` на «Device limit reached»
(вторая Solo при per-user лимите девайсов) — 500 ловили и вебхук lava
(ретраи каждую минуту), и reconcile-тик, и ручной mark paid в админке.
Деньги пришли — зачислить было некуда.

Два рубежа защиты, оба здесь и проверяются:
* ``create_invoice`` — счёт при живой подписке сразу создаётся как
  renewal (правду видят и админка, и вебхук);
* ``_mark_invoice_paid_core`` — страховка от гонки «счёт создан ДО
  активации подписки, оплачен ПОСЛЕ»: new_subscription-счёт проводится
  как продление существующей, а не роняет 500.
"""
from __future__ import annotations

from datetime import timedelta

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



def _setup(db):
    node = make_node(db)
    make_config(db, node)
    plan = make_plan(db)
    user = make_user(db)
    return node, plan, user


def test_create_invoice_switches_to_renewal_when_sub_alive(client, db_session):
    node, plan, user = _setup(db_session)
    sub = make_subscription(db_session, user, plan, node)

    resp = client.post(
        "/api/invoices",
        json={
            "plan_id": plan.id,
            "telegram_id": user.telegram_id,
            "currency": "RUB",
            "action": "new_subscription",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "renewal"
    assert data["subscription_id"] == sub.id


def test_create_invoice_keeps_new_subscription_without_alive_sub(client, db_session):
    """Без живой подписки поведение прежнее — счёт на новую подписку."""
    _node, plan, user = _setup(db_session)

    resp = client.post(
        "/api/invoices",
        json={
            "plan_id": plan.id,
            "telegram_id": user.telegram_id,
            "currency": "RUB",
            "action": "new_subscription",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "new_subscription"
    assert data["subscription_id"] is None


def test_mark_paid_falls_back_to_renewal_on_race(client, db_session):
    """Гонка: счёт new_subscription создан, подписка активировалась ПОСЛЕ
    (триал), оплата пришла последней. Проведение обязано продлить
    существующую подписку, а не падать на Device limit."""
    node, plan, user = _setup(db_session)

    resp = client.post(
        "/api/invoices",
        json={
            "plan_id": plan.id,
            "telegram_id": user.telegram_id,
            "currency": "RUB",
            "action": "new_subscription",
        },
    )
    assert resp.status_code == 200, resp.text
    invoice_id = resp.json()["id"]
    assert resp.json()["action"] == "new_subscription"

    # Подписка появляется ПОСЛЕ создания счёта — ровно гонка инцидента.
    sub = make_subscription(db_session, user, plan, node)
    expires_before = sub.expires_at

    paid = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert paid.status_code == 200, paid.text

    db_session.expire_all()
    invoice = db_session.get(models.Invoice, invoice_id)
    assert invoice.status == models.InvoiceStatus.paid
    assert invoice.action == models.InvoiceAction.renewal
    assert invoice.subscription_id == sub.id

    refreshed = db_session.get(models.Subscription, sub.id)
    assert refreshed.expires_at == expires_before + timedelta(days=plan.duration_days)
    # Второй подписки не появилось.
    subs = (
        db_session.query(models.Subscription)
        .filter(models.Subscription.user_id == user.id)
        .count()
    )
    assert subs == 1


def test_cross_plan_purchase_is_gated_for_clients(client, db_session):
    """C1 аудита 2026-08-21: покупка ДРУГОГО плана при живой подписке из
    бота создавала вторую параллельную подписку с двойным списанием.
    Клиентский путь (без admin-токена) режем 409; админский оставляем."""
    import os

    node, plan_a, user = _setup(db_session)
    plan_b = make_plan(db_session, name="other-plan")
    make_subscription(db_session, user, plan_a, node)

    body = {
        "plan_id": plan_b.id,
        "telegram_id": user.telegram_id,
        "currency": "RUB",
        "action": "new_subscription",
    }
    # conftest-клиент ходит с админ-токеном по умолчанию — клиентский
    # (ботовский) путь моделируем, снимая заголовок.
    resp = client.post(
        "/api/invoices", json=body, headers={"X-Admin-Token": ""}
    )
    assert resp.status_code == 409, resp.text

    resp = client.post(
        "/api/invoices",
        json=body,
        headers={"X-Admin-Token": os.environ["ADMIN_API_TOKEN"]},
    )
    assert resp.status_code == 200, resp.text


def test_topup_invoice_endpoint(client, db_session):
    """Паритет A: топап из бота — admin-token эндпоинт, сумма в рублях."""
    import os

    _node, _plan, user = _setup(db_session)
    headers = {"X-Admin-Token": os.environ["ADMIN_API_TOKEN"]}

    resp = client.post(
        "/api/invoices/topup",
        json={"telegram_id": user.telegram_id, "amount_kopecks": 20000},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["amount"] == 200.0
    assert data["currency"] == "RUB"

    # Без admin-токена — закрыто (conftest-клиент шлёт токен по
    # умолчанию, снимаем заголовок).
    resp = client.post(
        "/api/invoices/topup",
        json={"telegram_id": user.telegram_id, "amount_kopecks": 20000},
        headers={"X-Admin-Token": ""},
    )
    assert resp.status_code in (401, 403), resp.text

    # Меньше минимума — 400.
    resp = client.post(
        "/api/invoices/topup",
        json={"telegram_id": user.telegram_id, "amount_kopecks": 1},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


def test_paid_renewal_unfreezes_and_reprovisions(client, db_session):
    """Ревью 2026-08-21: подписка могла замёрзнуть между выставлением счёта
    и оплатой. Прежний код ставил active, но оставлял frozen_* поля
    (auto-unfreeze тик слеп к active, ручной unfreeze падает) и ноль живых
    девайсов. Оплата обязана чистить freeze-поля и реповижнить."""
    from datetime import datetime

    node, plan, user = _setup(db_session)
    sub = make_subscription(db_session, user, plan, node)

    resp = client.post(
        "/api/invoices",
        json={
            "plan_id": plan.id,
            "telegram_id": user.telegram_id,
            "currency": "RUB",
            "action": "new_subscription",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "renewal"
    invoice_id = resp.json()["id"]

    # Между счётом и оплатой подписка замёрзла: freeze ревокает девайсы
    # (здесь их и не было) и ставит frozen-поля.
    sub.status = models.SubscriptionStatus.frozen
    sub.frozen_at = datetime.utcnow()
    sub.frozen_until = datetime.utcnow() + timedelta(days=7)
    db_session.commit()

    paid = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert paid.status_code == 200, paid.text

    db_session.expire_all()
    refreshed = db_session.get(models.Subscription, sub.id)
    assert refreshed.status == models.SubscriptionStatus.active
    assert refreshed.frozen_at is None and refreshed.frozen_until is None, (
        "freeze-поля обязаны очиститься — иначе freeze-машина клинит"
    )
    live_devices = [
        d
        for d in refreshed.devices
        if d.status
        not in (models.DeviceStatus.revoked, models.DeviceStatus.disabled)
    ]
    assert live_devices, "оплата подписки без девайсов обязана реповижнить"
