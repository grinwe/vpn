"""Д0 — денежные фиксы перед эпиком «починка и оплата без Telegram».

Три дефекта из ревью 2026-07-29:
1. 1₽-дыра: POST /api/invoices доступен без админ-токена и принимал
   клиентский amount как есть; _mark_invoice_paid_core сумму не сверяет,
   вебхук сверяет платёж с тем же подконтрольным клиенту числом — renewal
   на 1 ₽ продлевал подписку целиком.
2. Слоты терялись: баланс-путь берёт доплату extra_device_slots, а все
   invoice-пути выставляли голый plan.price — два пути продления брали
   разные деньги за один период.
3. «Повторный тап → тот же pay_url» был невозможен: pay_url нигде не
   персистился, каждый повтор плодил новый счёт у провайдера.
"""
from __future__ import annotations

import json

from app import models
from app.services.payments.base import ProviderInvoice, WebhookEvent
from app.services.payments.checkout import checkout_pending_invoice

from .factories import make_node, make_plan, make_subscription, make_user


class _FakeProvider:
    def __init__(self, name: str = "lava_top"):
        self.name = name
        self.create_calls = 0

    def create_invoice(self, *, invoice_id, amount, currency, description=None, return_url=None):
        self.create_calls += 1
        return ProviderInvoice(
            external_id=f"ext-{self.name}-{invoice_id}-{self.create_calls}",
            pay_url=f"https://pay.example/{self.name}/{invoice_id}/{self.create_calls}",
            amount=amount,
            currency=currency,
        )

    def verify_webhook(self, body: bytes, headers: dict) -> WebhookEvent:  # pragma: no cover
        payload = json.loads(body.decode("utf-8"))
        return WebhookEvent(external_id=str(payload["invoice_id"]), status="paid")


def _renewal_payload(user, plan, sub, **extra):
    return {
        "user_id": user.id,
        "plan_id": plan.id,
        "subscription_id": sub.id,
        "action": "renewal",
        **extra,
    }


def _setup_sub(db, *, slots=0):
    node = make_node(db)
    user = make_user(db)
    plan = make_plan(db, traffic_limit_mb=None)
    sub = make_subscription(db, user, plan, node)
    if slots:
        sub.extra_device_slots = slots
        db.commit()
    return user, plan, sub


def test_client_amount_requires_admin_token(client, db_session):
    """Дыра №1: без админ-токена amount из тела — 403, счёт не создаётся."""
    user, plan, sub = _setup_sub(db_session)

    client.headers.pop("X-Admin-Token", None)
    resp = client.post(
        "/api/invoices",
        json=_renewal_payload(user, plan, sub, amount=1.0),
    )
    assert resp.status_code == 403, resp.text
    assert db_session.query(models.Invoice).count() == 0


def test_renewal_amount_is_server_computed_with_slots(client, db_session):
    """Дыра №2: цена renewal считается сервером и включает слоты.

    Фабричный план: price=10 ₽, 30 дней (1 месяц). 2 слота × 100 ₽/мес →
    итог 210 ₽ — ровно то, что списал бы баланс-путь за тот же период.
    """
    user, plan, sub = _setup_sub(db_session, slots=2)

    resp = client.post("/api/invoices", json=_renewal_payload(user, plan, sub))
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount"] == 210.0

    # Без слотов — голый план.
    user2 = make_user(db_session, telegram_id="tg-2")
    sub2 = make_subscription(db_session, user2, plan, make_node(db_session, name="n2"))
    resp = client.post("/api/invoices", json=_renewal_payload(user2, plan, sub2))
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount"] == 10.0


def test_admin_can_still_override_amount(client, db_session):
    """Админский override остаётся: ручные скидки/спеццены — осознанное
    действие под токеном, а не дыра."""
    user, plan, sub = _setup_sub(db_session)
    resp = client.post("/api/invoices", json=_renewal_payload(user, plan, sub, amount=5.0))
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount"] == 5.0


def test_checkout_reuses_pending_pay_url(db_session, monkeypatch):
    """Дыра №3: повторный чекаут возвращает ТОТ ЖЕ pay_url и не ходит к
    провайдеру второй раз — в кабинете не плодятся счета-сироты."""
    fake = _FakeProvider()
    monkeypatch.setattr(
        "app.services.payments.checkout.get_provider", lambda name=None: fake
    )
    user, plan, sub = _setup_sub(db_session)
    invoice = models.Invoice(
        user_id=user.id, plan_id=plan.id, subscription_id=sub.id,
        amount=10.0, currency="RUB", action=models.InvoiceAction.renewal,
    )
    db_session.add(invoice)
    db_session.commit()

    first = checkout_pending_invoice(db_session, invoice)
    second = checkout_pending_invoice(db_session, invoice)

    assert fake.create_calls == 1
    assert first.pay_url == second.pay_url
    assert second.reused is True

    payment = db_session.query(models.Payment).filter_by(invoice_id=invoice.id).one()
    assert payment.pay_url == first.pay_url


def test_checkout_reuse_is_per_provider(db_session, monkeypatch):
    """Смена провайдера — новый счёт: pending-Payment другого провайдера
    не реюзается."""
    fake = _FakeProvider("lava_top")
    fake_sbp = _FakeProvider("generic_sbp")
    monkeypatch.setattr(
        "app.services.payments.checkout.get_provider",
        lambda name=None: fake_sbp if name == "generic_sbp" else fake,
    )
    user, plan, sub = _setup_sub(db_session)
    invoice = models.Invoice(
        user_id=user.id, plan_id=plan.id, subscription_id=sub.id,
        amount=10.0, currency="RUB", action=models.InvoiceAction.renewal,
    )
    db_session.add(invoice)
    db_session.commit()

    first = checkout_pending_invoice(db_session, invoice, provider_name="lava_top")
    other = checkout_pending_invoice(db_session, invoice, provider_name="generic_sbp")
    assert other.provider == "generic_sbp"
    assert other.pay_url != first.pay_url
