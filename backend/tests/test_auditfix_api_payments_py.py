"""Аудит-фиксы #108 и #199 в ``app/api/payments.py``.

#108 — checkout конвертирует RUB-счёт в валюту провайдера:
  * telegram_stars: RUB → ⭐ по курсу WEBAPP_STARS_PER_RUB (как в WebApp);
  * cryptobot: RUB → USDT по CRYPTOBOT_RUB_PER_USDT, без курса — отказ 503;
  * SBP / уже сконвертированные счета проходят как есть.

#199 — вебхук остаётся async, но БД-часть уходит в threadpool
  (asyncio.to_thread); здесь проверяем, что рефакторинг не сломал
  happy-path и ветку non-paid.
"""
from __future__ import annotations

import json

import pytest

from app import models
from app.api.payments import _convert_for_provider
from app.services.payments.base import ProviderError, ProviderInvoice, WebhookEvent

from .factories import make_user

# ── #108: юнит-тесты конвертера ─────────────────────────────────────


def test_rub_to_stars_uses_webapp_rate(monkeypatch):
    monkeypatch.setenv("WEBAPP_STARS_PER_RUB", "0.67")
    amount, currency = _convert_for_provider(150.0, "RUB", "telegram_stars")
    # 150 ₽ * 0.67 = 100.5 → округление вверх → 101 ⭐, а не 150 ⭐ 1:1.
    assert amount == 101.0
    assert currency == "XTR"


def test_rub_to_usdt_uses_operator_rate(monkeypatch):
    monkeypatch.setenv("CRYPTOBOT_RUB_PER_USDT", "80")
    amount, currency = _convert_for_provider(150.0, "RUB", "cryptobot")
    # 150 / 80 = 1.875 → округление вверх до цента → 1.88 USDT.
    assert amount == 1.88
    assert currency == "USDT"


def test_rub_to_usdt_without_rate_is_refused(monkeypatch):
    monkeypatch.delenv("CRYPTOBOT_RUB_PER_USDT", raising=False)
    with pytest.raises(ProviderError, match="CRYPTOBOT_RUB_PER_USDT"):
        _convert_for_provider(150.0, "RUB", "cryptobot")


def test_non_rub_invoice_passes_through():
    # Счёт уже в звёздах (WebApp-путь) — не трогаем.
    assert _convert_for_provider(101.0, "XTR", "telegram_stars") == (101.0, "XTR")
    assert _convert_for_provider(2.5, "USDT", "cryptobot") == (2.5, "USDT")


def test_rub_sbp_passes_through():
    assert _convert_for_provider(150.0, "RUB", "sbp:acme") == (150.0, "RUB")


# ── общие фейки провайдера для эндпоинт-тестов ──────────────────────


class _FakeProvider:
    def __init__(self, name: str = "telegram_stars"):
        self.name = name
        self.create_calls: list[dict] = []

    def create_invoice(self, *, invoice_id, amount, currency, description=None, return_url=None):
        self.create_calls.append({"amount": amount, "currency": currency})
        return ProviderInvoice(
            external_id=str(invoice_id),
            pay_url="https://t.me/$fake",
            amount=amount,
            currency=currency,
        )

    def verify_webhook(self, body: bytes, headers: dict) -> WebhookEvent:
        payload = json.loads(body.decode("utf-8"))
        return WebhookEvent(
            external_id=str(payload["invoice_id"]),
            status=payload.get("status", "paid"),
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


# ── #108: checkout-эндпоинт шлёт провайдеру сконвертированную сумму ──


def test_checkout_converts_rub_to_stars(client, db_session, monkeypatch):
    monkeypatch.setenv("WEBAPP_STARS_PER_RUB", "0.67")
    fake = _FakeProvider("telegram_stars")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=150.0, currency="RUB")

    resp = client.post(
        f"/api/invoices/{invoice.id}/checkout",
        json={"provider": "telegram_stars"},
    )
    assert resp.status_code == 200, resp.text
    # Провайдер получил звёзды, а не рубли 1:1.
    assert fake.create_calls == [{"amount": 101.0, "currency": "XTR"}]
    data = resp.json()
    assert data["currency"] == "XTR"
    assert data["amount"] == 101.0
    # Payment-строка при этом хранится в валюте счёта (RUB).
    payment = (
        db_session.query(models.Payment)
        .filter(models.Payment.invoice_id == invoice.id)
        .one()
    )
    assert float(payment.amount) == 150.0
    assert payment.currency == "RUB"


def test_checkout_ownership_guard_rejects_foreign_telegram_id(client, db_session, monkeypatch):
    # Stage 9b IDOR-фикс: если передан telegram_id, счёт должен принадлежать
    # этому пользователю — иначе 403 и до провайдера не доходим.
    monkeypatch.setenv("WEBAPP_STARS_PER_RUB", "0.67")
    fake = _FakeProvider("telegram_stars")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    owner = make_user(db_session, telegram_id="owner-1")
    invoice = _make_invoice(db_session, owner, amount=150.0, currency="RUB")

    # Чужой telegram_id → 403.
    resp = client.post(
        f"/api/invoices/{invoice.id}/checkout",
        json={"provider": "telegram_stars", "telegram_id": "attacker-2"},
    )
    assert resp.status_code == 403, resp.text
    assert fake.create_calls == []

    # Свой telegram_id → проходит.
    resp = client.post(
        f"/api/invoices/{invoice.id}/checkout",
        json={"provider": "telegram_stars", "telegram_id": "owner-1"},
    )
    assert resp.status_code == 200, resp.text
    assert len(fake.create_calls) == 1

    # Без telegram_id — обратная совместимость (webapp/legacy): не блокируем.
    invoice2 = _make_invoice(db_session, owner, amount=150.0, currency="RUB")
    resp = client.post(
        f"/api/invoices/{invoice2.id}/checkout",
        json={"provider": "telegram_stars"},
    )
    assert resp.status_code == 200, resp.text


def test_checkout_cryptobot_without_rate_returns_503(client, db_session, monkeypatch):
    monkeypatch.delenv("CRYPTOBOT_RUB_PER_USDT", raising=False)
    fake = _FakeProvider("cryptobot")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=150.0, currency="RUB")

    resp = client.post(
        f"/api/invoices/{invoice.id}/checkout",
        json={"provider": "cryptobot"},
    )
    assert resp.status_code == 503
    assert "CRYPTOBOT_RUB_PER_USDT" in resp.json()["detail"]
    # До провайдера дойти не должны — счёт бы выставился в ~90x размере.
    assert fake.create_calls == []


# ── #199: вебхук после ухода БД-части в threadpool ──────────────────


def test_webhook_paid_flows_through_to_thread(client, db_session, monkeypatch):
    fake = _FakeProvider("telegram_stars")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, amount=100.0, currency="RUB", kind="topup")

    resp = client.post(
        "/api/payments/webhook/telegram_stars",
        json={"invoice_id": invoice.id, "status": "paid"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["invoice_id"] == invoice.id

    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.paid
    # topup-инвойс кредитует баланс: 100 ₽ → 10 000 копеек.
    assert user.balance_kopecks == 10_000


def test_webhook_non_paid_event_is_noop(client, db_session, monkeypatch):
    fake = _FakeProvider("telegram_stars")
    monkeypatch.setattr("app.api.payments.get_provider", lambda name=None: fake)

    user = make_user(db_session)
    invoice = _make_invoice(db_session, user, kind="topup")

    resp = client.post(
        "/api/payments/webhook/telegram_stars",
        json={"invoice_id": invoice.id, "status": "expired"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "expired"}
    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.pending
