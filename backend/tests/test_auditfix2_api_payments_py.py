"""Аудит-фикс #111 в ``app/api/payments.py``.

Вебхук ``paid`` теперь сверяет ``event.amount``/``event.currency`` с
ожидаемой суммой счёта, прежде чем зачислять. Сравнение идёт в валюте
провайдера: счёт хранится в рублях, а провайдер присылает свою валюту
(XTR/USDT), поэтому ожидание считается тем же ``_convert_for_provider``,
что и при создании счёта.

  * недоплата → 409, счёт остаётся pending, админам уходит алёрт;
  * несовпадение валюты → 409;
  * точная сумма → зачисляем;
  * сумма не пришла (template-режим SBP) → зачисляем как раньше.
"""
from __future__ import annotations

import json

from app import models
from app.services.payments.base import ProviderInvoice, WebhookEvent

from .factories import make_user


class _AmountProvider:
    """Фейк-провайдер, чей вебхук возвращает сумму и валюту платежа."""

    def __init__(self, name: str = "sbp:acme"):
        self.name = name

    def create_invoice(self, *, invoice_id, amount, currency, description=None, return_url=None):
        return ProviderInvoice(
            external_id=str(invoice_id),
            pay_url="https://pay/$fake",
            amount=amount,
            currency=currency,
        )

    def verify_webhook(self, body: bytes, headers: dict) -> WebhookEvent:
        payload = json.loads(body.decode("utf-8"))
        return WebhookEvent(
            external_id=str(payload["invoice_id"]),
            status=payload.get("status", "paid"),
            amount=payload.get("amount"),
            currency=payload.get("currency"),
        )


def _make_invoice(db, user, *, amount=150.0, currency="RUB", kind="topup"):
    invoice = models.Invoice(
        user_id=user.id,
        amount=amount,
        currency=currency,
        kind=kind,
        status=models.InvoiceStatus.pending,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def test_webhook_underpaid_leaves_invoice_pending(client, db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "42")
    fake = _AmountProvider("sbp:acme")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=150.0, currency="RUB")

    resp = client.post(
        "/api/payments/webhook/sbp:acme",
        json={"invoice_id": invoice.id, "status": "paid", "amount": 100.0, "currency": "RUB"},
    )
    assert resp.status_code == 409, resp.text

    db_session.expire_all()
    # Счёт не зачислен, баланс не тронут.
    assert invoice.status == models.InvoiceStatus.pending
    assert user.balance_kopecks == 0
    # Админам ушёл алёрт о расхождении.
    alert = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_payment_amount_mismatch")
        .first()
    )
    assert alert is not None


def test_webhook_currency_mismatch_rejected(client, db_session, monkeypatch):
    fake = _AmountProvider("sbp:acme")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=150.0, currency="RUB")

    resp = client.post(
        "/api/payments/webhook/sbp:acme",
        json={"invoice_id": invoice.id, "status": "paid", "amount": 150.0, "currency": "USD"},
    )
    assert resp.status_code == 409, resp.text
    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.pending


def test_webhook_exact_amount_credits(client, db_session, monkeypatch):
    fake = _AmountProvider("sbp:acme")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=150.0, currency="RUB")

    resp = client.post(
        "/api/payments/webhook/sbp:acme",
        json={"invoice_id": invoice.id, "status": "paid", "amount": 150.0, "currency": "rub"},
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.paid
    # 150 ₽ → 15 000 копеек на баланс topup-счёта.
    assert user.balance_kopecks == 15_000


def test_webhook_stars_amount_checked_in_provider_currency(client, db_session, monkeypatch):
    # Счёт в рублях, провайдер — звёзды: ожидаемая сумма считается через
    # конвертер, поэтому «100 XTR» за 100-рублёвый счёт при курсе 0.67
    # (ожидается 67 ⭐) — это ПЕРЕплата и зачисляется, а не блокируется.
    monkeypatch.setenv("WEBAPP_STARS_PER_RUB", "0.67")
    fake = _AmountProvider("telegram_stars")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=100.0, currency="RUB")

    # Недоплата в звёздах: 40 ⭐ < ожидаемых 67 ⭐ → 409.
    resp = client.post(
        "/api/payments/webhook/telegram_stars",
        json={"invoice_id": invoice.id, "status": "paid", "amount": 40.0, "currency": "XTR"},
    )
    assert resp.status_code == 409, resp.text
    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.pending


def test_webhook_without_amount_still_credits(client, db_session, monkeypatch):
    # Template-режим SBP не фиксирует сумму — event.amount=None, сверить
    # нечего, поведение как до фикса (зачисляем).
    fake = _AmountProvider("sbp:acme")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=150.0, currency="RUB")

    resp = client.post(
        "/api/payments/webhook/sbp:acme",
        json={"invoice_id": invoice.id, "status": "paid"},
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.paid
