"""Аудит-фикс #117 в ``app/api/payments.py``.

Вебхук ``paid`` теперь помечает оплаченной ИМЕННО ту Payment-строку,
которую реально оплатили, а не последнюю по id. При двойном checkout по
одному счёту создаётся несколько Payment-строк с разными provider
external_id; раньше paid всегда получала последняя, из-за чего сверка с
провайдером по external_id расходилась.

Матчинг идёт по provider invoice id, извлечённому из ``event.raw``
(``event.external_id`` — это НАШ внутренний invoice id, а не id провайдера).
Фолбэк на «последнюю pending» — только когда id провайдера из события
извлечь не удалось: при известном id без совпадения чужую строку не трогаем
(ревью 2026-09-19 — с двумя именами lava на одном счёте ретрай помечал paid
неоплаченную строку соседнего способа).
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


def test_webhook_unmatched_provider_id_marks_no_row(client, db_session, monkeypatch):
    """Provider invoice id из события известен, но не совпал ни с одной
    строкой: счёт зачисляем (деньги пришли, подпись валидна), а pending-строки
    НЕ трогаем — у них другие контракты, и «последняя pending» была бы
    неоплаченной строкой другого способа (до 2026-09-19 брали именно её)."""
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
    assert latest_p.status == models.PaymentStatus.pending
    assert first_p.status == models.PaymentStatus.pending


def test_webhook_falls_back_to_latest_pending_without_provider_id(client, db_session, monkeypatch):
    """Provider invoice id из события извлечь не удалось — различить строки
    нечем, берём последнюю pending (прежнее поведение)."""
    fake = _CryptoLikeProvider("cryptobot")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=150.0, currency="RUB")

    first_p = _make_payment(db_session, invoice, external_id="AAA")
    latest_p = _make_payment(db_session, invoice, external_id="BBB")

    resp = client.post(
        "/api/payments/webhook/cryptobot",
        json={"invoice_id": invoice.id, "status": "paid"},  # без provider_invoice_id
    )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.paid
    assert latest_p.status == models.PaymentStatus.paid
    assert first_p.status == models.PaymentStatus.pending
