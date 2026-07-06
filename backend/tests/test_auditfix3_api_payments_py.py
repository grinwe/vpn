"""Аудит-фикс #117 в ``app/api/payments.py``.

Вебхук ``paid`` теперь помечает оплаченной ИМЕННО ту Payment-строку,
которую реально оплатили, а не последнюю по id. При двойном checkout по
одному счёту создаётся несколько Payment-строк с разными provider
external_id; раньше paid всегда получала последняя, из-за чего сверка с
провайдером по external_id расходилась.

Матчинг идёт по provider invoice id, извлечённому из ``event.raw``
(``event.external_id`` — это НАШ внутренний invoice id, а не id провайдера),
с фолбэком на «последнюю pending».
"""
from __future__ import annotations

import json

from app import models
from app.services.payments.base import ProviderInvoice, WebhookEvent

from .factories import make_user


class _CryptoLikeProvider:
    """Фейк с cryptobot-подобным raw: provider invoice id лежит в
    ``raw["payload"]["invoice_id"]``, а ``external_id`` — наш invoice id."""

    def __init__(self, name: str = "cryptobot"):
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
        prov_id = payload.get("provider_invoice_id")
        raw = {"payload": {"invoice_id": prov_id, "payload": str(payload["invoice_id"])}}
        return WebhookEvent(
            external_id=str(payload["invoice_id"]),
            status=payload.get("status", "paid"),
            amount=payload.get("amount"),
            currency=payload.get("currency"),
            raw=raw,
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


def _make_payment(db, invoice, *, external_id, provider="cryptobot"):
    p = models.Payment(
        subscription_id=None,
        invoice_id=invoice.id,
        amount=invoice.amount,
        currency=invoice.currency,
        status=models.PaymentStatus.pending,
        provider=provider,
        external_id=external_id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_webhook_marks_the_actually_paid_payment(client, db_session, monkeypatch):
    """Из двух pending-Payment по одному счёту paid получает та, чей
    external_id совпал с provider invoice id из события."""
    fake = _CryptoLikeProvider("cryptobot")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=150.0, currency="RUB")

    # Первый checkout — оплаченная ссылка (external_id="AAA"), затем второй
    # checkout той же кнопкой (external_id="BBB", id больше).
    paid_p = _make_payment(db_session, invoice, external_id="AAA")
    other_p = _make_payment(db_session, invoice, external_id="BBB")
    assert other_p.id > paid_p.id

    resp = client.post(
        "/api/payments/webhook/cryptobot",
        json={
            "invoice_id": invoice.id,
            "status": "paid",
            "provider_invoice_id": "AAA",
        },
    )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.paid
    # Оплачена именно первая строка, вторая осталась pending.
    assert paid_p.status == models.PaymentStatus.paid
    assert other_p.status == models.PaymentStatus.pending


def test_webhook_falls_back_to_latest_pending(client, db_session, monkeypatch):
    """Если provider invoice id из события не совпал ни с одной строкой,
    берём последнюю pending (прежнее поведение)."""
    fake = _CryptoLikeProvider("cryptobot")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=150.0, currency="RUB")

    first_p = _make_payment(db_session, invoice, external_id="AAA")
    latest_p = _make_payment(db_session, invoice, external_id="BBB")

    resp = client.post(
        "/api/payments/webhook/cryptobot",
        json={
            "invoice_id": invoice.id,
            "status": "paid",
            "provider_invoice_id": "ZZZ",  # не совпадает ни с одной
        },
    )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.paid
    assert latest_p.status == models.PaymentStatus.paid
    assert first_p.status == models.PaymentStatus.pending
