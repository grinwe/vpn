"""Оплата со страницы починки — продление без Telegram.

Ключевое: счёт создаётся серверной ценой (со слотами), повторный тап ведёт
на ТОТ ЖЕ pay_url (иначе человек не знает, какой из счетов оплачивать), а
чужой invoice_id в ``?paid=`` ничего о себе не рассказывает.
"""
from __future__ import annotations

import pytest

from app import models
from app.api import sub_fix
from app.services.payments.base import ProviderInvoice

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)

HTML = {"accept": "text/html"}


class _FakeProvider:
    name = "lava_top"

    def __init__(self):
        self.calls = 0

    def create_invoice(self, *, invoice_id, amount, currency, description=None, return_url=None):
        self.calls += 1
        return ProviderInvoice(
            external_id=f"ext-{invoice_id}-{self.calls}",
            pay_url=f"https://pay.example/{invoice_id}/{self.calls}",
            amount=amount,
            currency=currency,
        )

    def verify_webhook(self, body, headers):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.rate_limit import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def paid_env(db_session, monkeypatch):
    monkeypatch.setenv("SUB_FIX_PAGE", "1")
    monkeypatch.setenv("SUB_FIX_PAY", "1")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-for-nonce")
    fake = _FakeProvider()
    monkeypatch.setattr(
        "app.services.payments.checkout.get_provider", lambda name=None: fake
    )
    node = make_node(db_session, name="pay-node", host="203.0.113.220")
    make_config(db_session, node)
    plan = make_plan(db_session, name="pay-plan")
    user = make_user(db_session, telegram_id="pay-user")
    sub = make_subscription_with_device(db_session, user, plan, node)
    device = sub.devices[0]
    device.sub_token = "paytoken1234567890"
    db_session.commit()
    return sub, device, fake


def test_pay_creates_invoice_with_slots_and_redirects(client, db_session, paid_env):
    """Счёт выставляется серверной ценой со слотами и ведёт на оплату."""
    sub, device, _fake = paid_env
    sub.extra_device_slots = 2  # +200 ₽ к плану в 10 ₽
    db_session.commit()

    nonce = sub_fix.make_nonce(device.sub_token)
    resp = client.post(
        f"/api/sub/{device.sub_token}?fix=1&n={nonce}&pay=1", follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("https://pay.example/")

    invoice = (
        db_session.query(models.Invoice)
        .filter_by(subscription_id=sub.id, action=models.InvoiceAction.renewal)
        .one()
    )
    assert float(invoice.amount) == 210.0
    assert invoice.status == models.InvoiceStatus.pending


def test_second_tap_reuses_the_same_invoice_and_url(client, db_session, paid_env):
    """Повторный тап не плодит счета: человек иначе не знает, какой из них
    оплачивать, а в кабинете провайдера копятся сироты."""
    sub, device, fake = paid_env
    nonce = sub_fix.make_nonce(device.sub_token)
    url = f"/api/sub/{device.sub_token}?fix=1&n={nonce}&pay=1"

    first = client.post(url, follow_redirects=False)
    second = client.post(url, follow_redirects=False)

    assert first.headers["location"] == second.headers["location"]
    assert fake.calls == 1, "к провайдеру ходим один раз"
    assert (
        db_session.query(models.Invoice).filter_by(subscription_id=sub.id).count() == 1
    )


def test_pay_is_off_by_default(client, db_session, paid_env, monkeypatch):
    """Флаг SUB_FIX_PAY выключен → кнопки продления нет и счёт не создаётся."""
    sub, device, _fake = paid_env
    monkeypatch.setenv("SUB_FIX_PAY", "0")

    page = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert "Продлить" not in page.text

    nonce = sub_fix.make_nonce(device.sub_token)
    resp = client.post(f"/api/sub/{device.sub_token}?fix=1&n={nonce}&pay=1")
    assert resp.status_code == 200
    assert db_session.query(models.Invoice).filter_by(subscription_id=sub.id).count() == 0


def test_foreign_invoice_id_reveals_nothing(client, db_session, paid_env):
    """``?paid=<чужой id>`` не подтверждает и не раскрывает чужой счёт."""
    sub, device, _fake = paid_env
    other_user = make_user(db_session, telegram_id="pay-other")
    foreign = models.Invoice(
        user_id=other_user.id,
        plan_id=sub.plan_id,
        subscription_id=None,
        amount=999.0,
        currency="RUB",
        action=models.InvoiceAction.renewal,
        status=models.InvoiceStatus.paid,
    )
    db_session.add(foreign)
    db_session.commit()

    resp = client.get(
        f"/api/sub/{device.sub_token}?fix=1&paid={foreign.id}", headers=HTML
    )
    assert resp.status_code == 200
    assert "999" not in resp.text
    assert "Оплата получена" not in resp.text


def test_paid_invoice_shows_success(client, db_session, paid_env):
    """Свой оплаченный счёт → экран «оплата получена» с подсказкой про 🔄."""
    sub, device, _fake = paid_env
    invoice = models.Invoice(
        user_id=sub.user_id,
        plan_id=sub.plan_id,
        subscription_id=sub.id,
        amount=10.0,
        currency="RUB",
        action=models.InvoiceAction.renewal,
        status=models.InvoiceStatus.paid,
    )
    db_session.add(invoice)
    db_session.commit()

    resp = client.get(
        f"/api/sub/{device.sub_token}?fix=1&paid={invoice.id}", headers=HTML
    )
    assert "Оплата получена" in resp.text
    assert "🔄" in resp.text


def test_pending_invoice_page_autorefreshes(client, db_session, paid_env):
    """Подтверждение занимает 30–60 с (вебхуки lava не долетают, работает тик
    сверки) — страница обязана обновляться сама и честно об этом говорить."""
    sub, device, _fake = paid_env
    invoice = models.Invoice(
        user_id=sub.user_id,
        plan_id=sub.plan_id,
        subscription_id=sub.id,
        amount=10.0,
        currency="RUB",
        action=models.InvoiceAction.renewal,
        status=models.InvoiceStatus.pending,
    )
    db_session.add(invoice)
    db_session.commit()

    resp = client.get(
        f"/api/sub/{device.sub_token}?fix=1&paid={invoice.id}", headers=HTML
    )
    assert 'http-equiv="refresh"' in resp.text
    assert "второй раз" in resp.text


def test_page_pins_the_card_provider(client, db_session, paid_env, monkeypatch):
    """Провайдер пинится явно. Без этого get_provider берёт общую ротацию,
    чей прод-дефолт — cryptobot: страница «оплатить картой без Telegram»
    выдала бы криптосчёт человеку, у которого нет ни Telegram, ни кошелька."""
    seen = {}

    def _spy(name=None):
        seen["name"] = name
        return _FakeProvider()

    monkeypatch.setattr("app.services.payments.checkout.get_provider", _spy)
    _sub, device, _fake = paid_env
    nonce = sub_fix.make_nonce(device.sub_token)
    client.post(f"/api/sub/{device.sub_token}?fix=1&n={nonce}&pay=1", follow_redirects=False)
    assert seen["name"] == "lava_top"


def test_checkout_gets_return_url_back_to_the_page(client, paid_env, monkeypatch):
    """После оплаты человека возвращает на нашу страницу ожидания — иначе
    экран «проверяем оплату» недостижим, и человек остаётся на вкладке банка."""
    monkeypatch.setenv("SUB_LINK_BASE_URL", "https://grn-ssync.pro")
    seen = {}

    class _Spy(_FakeProvider):
        def create_invoice(self, *, invoice_id, amount, currency, description=None, return_url=None):
            seen["return_url"] = return_url
            return super().create_invoice(
                invoice_id=invoice_id, amount=amount, currency=currency
            )

    monkeypatch.setattr(
        "app.services.payments.checkout.get_provider", lambda name=None: _Spy()
    )
    _sub, device, _fake = paid_env
    nonce = sub_fix.make_nonce(device.sub_token)
    client.post(f"/api/sub/{device.sub_token}?fix=1&n={nonce}&pay=1", follow_redirects=False)
    assert seen["return_url"].startswith("https://grn-ssync.pro/")
    assert "paid=" in seen["return_url"]


def test_price_is_shown_before_payment(client, db_session, paid_env):
    """Сумму и срок человек видит ДО платёжного виджета: кнопка, которая
    молча ведёт на списание, — плохая кнопка."""
    sub, device, _fake = paid_env
    sub.extra_device_slots = 1  # 10 + 100 = 110 ₽
    db_session.commit()

    page = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert "110" in page.text
    assert "30 дн" in page.text


def test_price_with_slots_is_explained(client, db_session, paid_env):
    """Сумма выше тарифа расшифровывается: иначе человек видит цифру, не
    совпадающую с планом, и решает, что мы ошиблись (владелец так и
    отреагировал на живой странице)."""
    sub, device, _fake = paid_env
    sub.extra_device_slots = 1
    db_session.commit()

    page = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert "110" in page.text          # 10 ₽ план + 100 ₽ слот
    assert "10 ₽ тариф" in page.text
    assert "дополнительные устройства (1)" in page.text

    # Без слотов расшифровки нет — она была бы шумом.
    sub.extra_device_slots = 0
    db_session.commit()
    page = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert "тариф плюс" not in page.text
