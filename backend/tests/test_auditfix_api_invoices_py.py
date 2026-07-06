"""Аудит #186 — интеграционные тесты денежного конвейера «оплата → подписка».

Раньше ядро биллинга ``_mark_invoice_paid_core`` (invoices.py) и вебхук
``POST /api/payments/webhook/{provider}`` не выполнялись ни одним тестом:
провайдеры проверялись как чистые функции, а ядро мокалось. Здесь мы
бьём по реальным HTTP-эндпоинтам через TestClient и проверяем состояние
БД после:

* mark_paid: pending → paid, создание Subscription + ProvisioningTask;
* идемпотентность повторного mark_paid / повторной доставки вебхука
  (нет второй подписки, нет двойного продления);
* вебхук generic_sbp с настоящей HMAC-подписью тела;
* ветки ошибок: failed-инвойс → 400, несуществующий id → 404,
  topup с нулевой суммой → 400, битая подпись → 401;
* topup-ветка: баланс пользователя пополнен, реферер получил бонус
  ровно один раз.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from app import models
from app.services import balance as balance_svc

from .factories import make_config, make_node, make_plan, make_user

SBP_SLUG = "acme"
SBP_PROVIDER = f"sbp:{SBP_SLUG}"
SBP_SECRET = "test-sbp-secret-186"


# ── helpers ──────────────────────────────────────────────────────────

def _setup_provisionable(db):
    """Нода с включённым конфигом + план + пользователь — минимум,
    чтобы ``provision_subscription`` внутри mark_paid отработал
    (run_task_async замокан conftest-ом, но все строки в БД создаются)."""
    node = make_node(db)
    make_config(db, node)
    plan = make_plan(db)
    user = make_user(db)
    return node, plan, user


def _create_pending_invoice(client, plan, user, **overrides):
    """Создаём инвойс через реальный POST /api/invoices."""
    body = {
        "plan_id": plan.id,
        "telegram_id": user.telegram_id,
        "currency": "RUB",
        "action": "new_subscription",
    }
    body.update(overrides)
    resp = client.post("/api/invoices", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    return data["id"]


def _sbp_env(monkeypatch):
    """Конфигурируем generic_sbp-провайдер через env, как в проде."""
    norm = SBP_SLUG.upper()
    monkeypatch.setenv(f"SBP_{norm}_HMAC_SECRET", SBP_SECRET)
    monkeypatch.setenv(
        f"SBP_{norm}_PAY_URL_TEMPLATE",
        "https://pay.example/{invoice_id}",
    )


def _signed_webhook(client, payload: dict):
    """POST на вебхук с корректной HMAC-SHA256 подписью сырого тела."""
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(SBP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return client.post(
        f"/api/payments/webhook/{SBP_PROVIDER}",
        content=body,
        headers={
            "content-type": "application/json",
            "x-sbp-signature": signature,
        },
    )


# ── mark_paid: happy path + идемпотентность ─────────────────────────

def test_mark_paid_provisions_new_subscription(client, db_session):
    _, plan, user = _setup_provisionable(db_session)
    invoice_id = _create_pending_invoice(client, plan, user)

    resp = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "paid"
    assert data["subscription_id"] is not None

    db_session.expire_all()
    invoice = db_session.get(models.Invoice, invoice_id)
    assert invoice.status == models.InvoiceStatus.paid

    subs = db_session.query(models.Subscription).filter_by(user_id=user.id).all()
    assert len(subs) == 1
    assert subs[0].id == data["subscription_id"]
    assert subs[0].plan_id == plan.id

    # Оркестратор замокан, но ProvisioningTask-строка обязана появиться.
    tasks = db_session.query(models.ProvisioningTask).all()
    assert len(tasks) >= 1


def test_mark_paid_repeat_does_not_create_second_subscription(client, db_session):
    _, plan, user = _setup_provisionable(db_session)
    invoice_id = _create_pending_invoice(client, plan, user)

    first = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert first.status_code == 200
    second = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert second.status_code == 200
    assert second.json()["subscription_id"] == first.json()["subscription_id"]

    db_session.expire_all()
    count = db_session.query(models.Subscription).filter_by(user_id=user.id).count()
    assert count == 1


def test_mark_paid_renewal_extends_once(client, db_session):
    """Продление двигает expires_at ровно на duration_days и ровно один раз."""
    _, plan, user = _setup_provisionable(db_session)

    # Первая покупка через сам конвейер — получаем живую подписку.
    first_invoice = _create_pending_invoice(client, plan, user)
    resp = client.post(f"/api/invoices/{first_invoice}/mark_paid")
    assert resp.status_code == 200
    sub_id = resp.json()["subscription_id"]

    db_session.expire_all()
    sub = db_session.get(models.Subscription, sub_id)
    expires_before = sub.expires_at

    renewal_invoice = _create_pending_invoice(
        client, plan, user, action="renewal", subscription_id=sub_id
    )
    resp = client.post(f"/api/invoices/{renewal_invoice}/mark_paid")
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    sub = db_session.get(models.Subscription, sub_id)
    extended = sub.expires_at
    assert (extended - expires_before).days == plan.duration_days

    # Повторный mark_paid уже оплаченного renewal-инвойса не продлевает снова.
    resp = client.post(f"/api/invoices/{renewal_invoice}/mark_paid")
    assert resp.status_code == 200
    db_session.expire_all()
    sub = db_session.get(models.Subscription, sub_id)
    assert sub.expires_at == extended


# ── ветки ошибок mark_paid ───────────────────────────────────────────

def test_mark_paid_missing_invoice_404(client):
    resp = client.post("/api/invoices/999999/mark_paid")
    assert resp.status_code == 404


def test_mark_paid_failed_invoice_400(client, db_session):
    _, plan, user = _setup_provisionable(db_session)
    invoice_id = _create_pending_invoice(client, plan, user)
    resp = client.post(f"/api/invoices/{invoice_id}/cancel")
    assert resp.status_code == 200

    resp = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert resp.status_code == 400

    db_session.expire_all()
    assert db_session.query(models.Subscription).count() == 0


def test_mark_paid_topup_with_zero_amount_400(client, db_session):
    user = make_user(db_session)
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=None,
        amount=0,
        currency="RUB",
        kind="topup",
    )
    db_session.add(invoice)
    db_session.commit()

    resp = client.post(f"/api/invoices/{invoice.id}/mark_paid")
    assert resp.status_code == 400

    db_session.expire_all()
    assert db_session.get(models.User, user.id).balance_kopecks == 0
    assert db_session.get(models.Invoice, invoice.id).status == models.InvoiceStatus.pending


# ── вебхук платёжного провайдера ─────────────────────────────────────

def test_webhook_paid_marks_invoice_and_payment(client, db_session, monkeypatch):
    _sbp_env(monkeypatch)
    _, plan, user = _setup_provisionable(db_session)
    invoice_id = _create_pending_invoice(client, plan, user)

    # Payment-строка, которую в проде пишет /checkout — вебхук должен
    # сопоставить её по (invoice_id, provider) и перевести в paid.
    payment = models.Payment(
        invoice_id=invoice_id,
        amount=plan.price,
        currency="RUB",
        status=models.PaymentStatus.pending,
        provider=SBP_PROVIDER,
        external_id=str(invoice_id),
    )
    db_session.add(payment)
    db_session.commit()
    payment_id = payment.id

    resp = _signed_webhook(client, {"invoice_id": invoice_id, "status": "paid"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paid"

    db_session.expire_all()
    assert db_session.get(models.Invoice, invoice_id).status == models.InvoiceStatus.paid
    assert db_session.get(models.Payment, payment_id).status == models.PaymentStatus.paid
    assert db_session.query(models.Subscription).filter_by(user_id=user.id).count() == 1


def test_webhook_repeat_delivery_is_idempotent(client, db_session, monkeypatch):
    _sbp_env(monkeypatch)
    _, plan, user = _setup_provisionable(db_session)
    invoice_id = _create_pending_invoice(client, plan, user)

    first = _signed_webhook(client, {"invoice_id": invoice_id, "status": "paid"})
    assert first.status_code == 200

    db_session.expire_all()
    sub = db_session.query(models.Subscription).filter_by(user_id=user.id).one()
    expires_before = sub.expires_at

    # Провайдер прислал тот же вебхук ещё раз (ретрай доставки).
    second = _signed_webhook(client, {"invoice_id": invoice_id, "status": "paid"})
    assert second.status_code == 200

    db_session.expire_all()
    subs = db_session.query(models.Subscription).filter_by(user_id=user.id).all()
    assert len(subs) == 1
    assert subs[0].expires_at == expires_before


def test_webhook_bad_signature_rejected(client, db_session, monkeypatch):
    _sbp_env(monkeypatch)
    _, plan, user = _setup_provisionable(db_session)
    invoice_id = _create_pending_invoice(client, plan, user)

    body = json.dumps({"invoice_id": invoice_id, "status": "paid"}).encode("utf-8")
    resp = client.post(
        f"/api/payments/webhook/{SBP_PROVIDER}",
        content=body,
        headers={
            "content-type": "application/json",
            "x-sbp-signature": "0" * 64,
        },
    )
    assert resp.status_code == 401

    db_session.expire_all()
    assert db_session.get(models.Invoice, invoice_id).status == models.InvoiceStatus.pending
    assert db_session.query(models.Subscription).count() == 0


def test_webhook_unknown_invoice_404(client, monkeypatch):
    _sbp_env(monkeypatch)
    resp = _signed_webhook(client, {"invoice_id": 999999, "status": "paid"})
    assert resp.status_code == 404


def test_webhook_non_paid_status_no_action(client, db_session, monkeypatch):
    _sbp_env(monkeypatch)
    _, plan, user = _setup_provisionable(db_session)
    invoice_id = _create_pending_invoice(client, plan, user)

    resp = _signed_webhook(client, {"invoice_id": invoice_id, "status": "expired"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"

    db_session.expire_all()
    assert db_session.get(models.Invoice, invoice_id).status == models.InvoiceStatus.pending
    assert db_session.query(models.Subscription).count() == 0


# ── topup-ветка: баланс + реферальный бонус ──────────────────────────

def _make_topup_invoice(db, user, amount) -> int:
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=None,
        amount=amount,
        currency="RUB",
        kind="topup",
    )
    db.add(invoice)
    db.commit()
    return invoice.id


def test_topup_credits_balance_and_referral_bonus_once(client, db_session):
    referrer = make_user(db_session, telegram_id="tg-ref")
    user = make_user(db_session, telegram_id="tg-buyer")
    user.referred_by_id = referrer.id
    db_session.add(user)
    db_session.commit()

    invoice_id = _make_topup_invoice(db_session, user, 100)

    resp = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paid"
    assert resp.json()["kind"] == "topup"

    db_session.expire_all()
    assert db_session.get(models.User, user.id).balance_kopecks == 100 * 100
    # Реферер получил бонус за первый реальный topup реферала.
    assert (
        db_session.get(models.User, referrer.id).balance_kopecks
        == balance_svc.REFERRAL_BONUS_KOPECKS
    )

    # Повторный mark_paid уже оплаченного topup-инвойса не докручивает баланс.
    resp = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.User, user.id).balance_kopecks == 100 * 100

    # Второй topup того же пользователя: баланс растёт, бонус НЕ повторяется.
    second_invoice = _make_topup_invoice(db_session, user, 50)
    resp = client.post(f"/api/invoices/{second_invoice}/mark_paid")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.User, user.id).balance_kopecks == 150 * 100
    assert (
        db_session.get(models.User, referrer.id).balance_kopecks
        == balance_svc.REFERRAL_BONUS_KOPECKS
    )
