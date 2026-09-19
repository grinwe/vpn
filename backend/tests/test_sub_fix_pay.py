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

    def __init__(self, name: str | None = None):
        self.calls = 0
        if name:
            self.name = name

    def create_invoice(self, *, invoice_id, amount, currency, description=None, return_url=None):
        self.calls += 1
        # Имя провайдера в external_id/pay_url: у каждого способа (карта / СБП)
        # своё пространство счетов, как у настоящих эквайреров.
        return ProviderInvoice(
            external_id=f"ext-{self.name}-{invoice_id}-{self.calls}",
            pay_url=f"https://pay.example/{self.name}/{invoice_id}/{self.calls}",
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
    for pay in ("1", "sbp"):
        resp = client.post(f"/api/sub/{device.sub_token}?fix=1&n={nonce}&pay={pay}")
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


@pytest.mark.parametrize(
    ("pay", "expected"),
    [("1", "lava_top"), ("sbp", "lava_top_sbp")],
    ids=["card", "sbp"],
)
def test_page_pins_the_card_provider(client, db_session, paid_env, monkeypatch, pay, expected):
    """Провайдер пинится явно, свой для каждого способа. Без этого
    get_provider берёт общую ротацию, чей прод-дефолт — cryptobot: страница
    «оплатить без Telegram» выдала бы криптосчёт человеку, у которого нет ни
    Telegram, ни кошелька. ``pay=1`` — карта (lava_top), ``pay=sbp`` — СБП
    (lava_top_sbp): с 2026-09-19 это два разных имени одной интеграции."""
    monkeypatch.delenv("SUB_FIX_PROVIDER", raising=False)
    monkeypatch.delenv("SUB_FIX_SBP_PROVIDER", raising=False)
    seen = {}

    def _spy(name=None):
        seen["name"] = name
        return _FakeProvider(name)

    monkeypatch.setattr("app.services.payments.checkout.get_provider", _spy)
    _sub, device, _fake = paid_env
    nonce = sub_fix.make_nonce(device.sub_token)
    resp = client.post(
        f"/api/sub/{device.sub_token}?fix=1&n={nonce}&pay={pay}", follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    assert seen["name"] == expected


def test_page_provider_env_overrides_are_per_method(client, db_session, paid_env, monkeypatch):
    """SUB_FIX_PROVIDER пинит карту, SUB_FIX_SBP_PROVIDER — СБП, друг на
    друга не влияют (иначе переключение карточного эквайрера утащило бы за
    собой и СБП)."""
    monkeypatch.setenv("SUB_FIX_PROVIDER", "card_alt")
    monkeypatch.setenv("SUB_FIX_SBP_PROVIDER", "sbp_alt")
    seen = []

    def _spy(name=None):
        seen.append(name)
        return _FakeProvider(name)

    monkeypatch.setattr("app.services.payments.checkout.get_provider", _spy)
    _sub, device, _fake = paid_env
    nonce = sub_fix.make_nonce(device.sub_token)
    client.post(f"/api/sub/{device.sub_token}?fix=1&n={nonce}&pay=1", follow_redirects=False)
    client.post(f"/api/sub/{device.sub_token}?fix=1&n={nonce}&pay=sbp", follow_redirects=False)
    assert seen == ["card_alt", "sbp_alt"]

    # Пустое значение = дефолт, а не провайдер с пустым именем.
    monkeypatch.setenv("SUB_FIX_PROVIDER", "  ")
    monkeypatch.setenv("SUB_FIX_SBP_PROVIDER", "")
    assert sub_fix.pay_provider("card") == "lava_top"
    assert sub_fix.pay_provider("sbp") == "lava_top_sbp"
    assert sub_fix.pay_provider() == "lava_top"


def test_start_screen_offers_sbp_and_card_with_price(client, db_session, paid_env):
    """На стартовом экране ДВЕ кнопки продления — СБП и карта — и на каждой
    цена со сроком. Единой «Карта РФ / СБП» больше нет: lava закрыл карту у
    агрегатора PAY2ME (2026-09-19), у каждого способа свой эквайрер."""
    sub, device, _fake = paid_env
    sub.extra_device_slots = 1  # 10 + 100 = 110 ₽
    db_session.commit()

    page = client.get(f"/api/sub/{device.sub_token}?fix=1", headers=HTML)
    assert page.status_code == 200
    assert "Продлить по СБП на 30 дн. за 110 ₽" in page.text
    assert "Продлить картой на 30 дн. за 110 ₽" in page.text
    # Каждая кнопка — своя форма со своим query-параметром способа.
    assert "&pay=sbp" in page.text
    assert "&pay=1" in page.text
    assert page.text.count("<form") >= 3  # починка + СБП + карта
    # Общая (не по способу) кнопка без указания способа не рисуется.
    assert "Продлить на 30 дн." not in page.text


def test_sbp_and_card_share_invoice_but_own_payment_rows(client, db_session, paid_env, monkeypatch):
    """Счёт на продление один (дедуп по подписке), а Payment-строка у каждого
    способа своя: человек нажал СБП, передумал, нажал карту — счета не
    плодятся, но у карточного эквайрера свой external_id, и вебхук/сверка
    по семейству lava найдут ту строку, что реально оплачена."""
    monkeypatch.delenv("SUB_FIX_PROVIDER", raising=False)
    monkeypatch.delenv("SUB_FIX_SBP_PROVIDER", raising=False)
    monkeypatch.setattr(
        "app.services.payments.checkout.get_provider",
        lambda name=None: _FakeProvider(name),
    )
    sub, device, _fake = paid_env
    nonce = sub_fix.make_nonce(device.sub_token)
    base = f"/api/sub/{device.sub_token}?fix=1&n={nonce}"

    first = client.post(f"{base}&pay=sbp", follow_redirects=False)
    second = client.post(f"{base}&pay=1", follow_redirects=False)
    assert first.status_code == second.status_code == 303
    # Разные провайдеры → разные платёжные ссылки.
    assert first.headers["location"] != second.headers["location"]

    invoices = db_session.query(models.Invoice).filter_by(subscription_id=sub.id).all()
    assert len(invoices) == 1
    providers = sorted(
        p.provider
        for p in db_session.query(models.Payment).filter_by(invoice_id=invoices[0].id)
    )
    assert providers == ["lava_top", "lava_top_sbp"]


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
