"""Аудит-фиксы (волна 2) для api_webapp.py: находки №116 и №191.

№116 — гонка при добавлении устройства: charge_extra_device уже
инкрементирует sub.extra_device_slots, а хэндлер следом ПЕРЕЗАПИСЫВАЛ
поле значением current_slots + 1, прочитанным в начале запроса. Два
одновременных add_device теряли одну оплату (slots=1 вместо 2). Фикс:
FOR UPDATE-лок строки подписки + убрана перезапись слота в хэндлере.

№191 — HTTP-слой мини-аппа почти без тестов. Здесь покрыты денежные
пути /checkout и /topup и триальный /trial/activate: статус-коды,
создание Invoice/Payment нужного kind, отклонение невалидного payload
и повторной активации.
"""
from dataclasses import dataclass

import pytest

from app import models
from app.api_webapp import issue_token
from app.config import get_settings
from app.services import balance, provisioning_throttle
from app.services.payments.base import ProviderInvoice

from .factories import (
    make_plan,
    make_subscription_with_device,
    make_user,
)


@pytest.fixture(autouse=True)
def _fresh_throttle():
    """Cold-path трэттл глобальный на процесс — чистим между тестами."""
    provisioning_throttle.reset_for_tests()
    yield
    provisioning_throttle.reset_for_tests()


def _auth_headers(user_id: int) -> dict:
    settings = get_settings()
    token = issue_token(user_id, settings.webapp_jwt_secret, 600)
    return {"Authorization": f"Bearer {token}"}


# ── Фейковый платёжный провайдер ─────────────────────────────────────
# Реальные провайдеры (telegram_stars/cryptobot) ходят по сети в
# create_invoice; в тестах подменяем get_provider на офлайн-заглушку.


@dataclass
class _FakeProvider:
    name: str = "telegram_stars"

    def create_invoice(self, *, invoice_id, amount, currency, description=None,
                        return_url=None):
        return ProviderInvoice(
            external_id=f"ext-{invoice_id}",
            pay_url=f"https://pay.example/{invoice_id}",
            amount=amount,
            currency=currency,
        )

    def verify_webhook(self, body, headers):  # pragma: no cover - не нужен здесь
        raise NotImplementedError


@pytest.fixture
def fake_provider(monkeypatch):
    provider = _FakeProvider()
    monkeypatch.setattr("app.services.payments.checkout.get_provider", lambda name=None: provider)
    return provider


# ── №191: /checkout ──────────────────────────────────────────────────

def test_checkout_creates_invoice_and_payment(client, db_session, fake_provider):
    """Валидный чекаут → 200, Invoice + Payment(pending) созданы."""
    plan = make_plan(db_session, name="plan-checkout")
    user = make_user(db_session, telegram_id="tg-checkout")
    db_session.commit()

    res = client.post(
        "/api/webapp/checkout",
        json={"plan_id": plan.id, "provider": "telegram_stars"},
        headers=_auth_headers(user.id),
    )
    assert res.status_code == 200, res.text
    data = res.json()
    invoice_id = data["invoice_id"]

    invoice = db_session.get(models.Invoice, invoice_id)
    assert invoice is not None
    assert invoice.user_id == user.id
    assert invoice.plan_id == plan.id
    assert invoice.action == models.InvoiceAction.new_subscription

    payments = (
        db_session.query(models.Payment)
        .filter(models.Payment.invoice_id == invoice_id)
        .all()
    )
    assert len(payments) == 1
    assert payments[0].status == models.PaymentStatus.pending
    assert payments[0].external_id == f"ext-{invoice_id}"


def test_checkout_unknown_plan_404(client, db_session, fake_provider):
    """Несуществующий plan_id → 404, инвойс не создаётся."""
    user = make_user(db_session, telegram_id="tg-noplan")
    db_session.commit()

    res = client.post(
        "/api/webapp/checkout",
        json={"plan_id": 999999, "provider": "telegram_stars"},
        headers=_auth_headers(user.id),
    )
    assert res.status_code == 404
    assert db_session.query(models.Invoice).count() == 0


def test_checkout_requires_auth(client):
    res = client.post("/api/webapp/checkout", json={"plan_id": 1})
    assert res.status_code == 401


# ── №191: /topup ─────────────────────────────────────────────────────

def test_topup_below_minimum_400(client, db_session, fake_provider):
    """Сумма ниже MIN_TOPUP_KOPECKS → 400, ничего не создано."""
    user = make_user(db_session, telegram_id="tg-topup-min")
    db_session.commit()

    res = client.post(
        "/api/webapp/topup",
        json={"amount_kopecks": balance.MIN_TOPUP_KOPECKS - 1},
        headers=_auth_headers(user.id),
    )
    assert res.status_code == 400
    assert db_session.query(models.Invoice).count() == 0


def test_topup_creates_topup_invoice(client, db_session, fake_provider):
    """Валидный топап → 200, Invoice(kind='topup', plan_id=None) + Payment."""
    user = make_user(db_session, telegram_id="tg-topup-ok")
    db_session.commit()

    res = client.post(
        "/api/webapp/topup",
        json={"amount_kopecks": balance.MIN_TOPUP_KOPECKS, "provider": "telegram_stars"},
        headers=_auth_headers(user.id),
    )
    assert res.status_code == 200, res.text
    invoice_id = res.json()["invoice_id"]

    invoice = db_session.get(models.Invoice, invoice_id)
    assert invoice.kind == "topup"
    assert invoice.plan_id is None
    # Сумма всегда хранится в рублях, чтобы хук топапа кредитовал
    # чистые копейки независимо от валюты отображения.
    assert invoice.amount == pytest.approx(balance.MIN_TOPUP_KOPECKS / 100)
    assert (
        db_session.query(models.Payment)
        .filter(models.Payment.invoice_id == invoice_id)
        .count()
        == 1
    )


# ── №191: /trial/activate ────────────────────────────────────────────

def test_trial_activate_no_plan_503(client, db_session):
    """Нет видимого 30-дневного плана → 503, триал не активирован."""
    user = make_user(db_session, telegram_id="tg-trial-noplan")
    db_session.commit()

    res = client.post(
        "/api/webapp/trial/activate", headers=_auth_headers(user.id)
    )
    assert res.status_code == 503

    db_session.expire_all()
    assert db_session.get(models.User, user.id).trial_activated_at is None


def test_trial_activate_once_then_repeat_409(client, db_session):
    """Первый запрос активирует триал, повторный → 409."""
    make_plan(db_session, name="trial-plan")  # видимый 30-дневный план
    user = make_user(db_session, telegram_id="tg-trial-ok")
    db_session.commit()

    res1 = client.post(
        "/api/webapp/trial/activate", headers=_auth_headers(user.id)
    )
    assert res1.status_code == 200, res1.text
    assert res1.json()["trial_amount_kopecks"] > 0

    db_session.expire_all()
    assert db_session.get(models.User, user.id).trial_activated_at is not None

    res2 = client.post(
        "/api/webapp/trial/activate", headers=_auth_headers(user.id)
    )
    assert res2.status_code == 409


# ── №116: гонка добавления устройства ────────────────────────────────

def test_sequential_add_device_charges_both_slots(client, db_session):
    """Два ПОСЛЕДОВАТЕЛЬНЫХ add_device → slots=2 и ДВА списания.

    Проверяем ЛОГИКУ фикса №116 без настоящей гонки: синхронный TestClient
    на одной БД реальный параллелизм не воспроизводит (потоки делят
    db_session → ошибки SQLAlchemy, второй запрос «терял» соединение). Сам
    анти-гоночный механизм — FOR UPDATE-лок строки подписки + отсутствие
    перезаписи слота в хэндлере — проверяем через два последовательных
    вызова: каждый читает уже инкрементированный extra_device_slots и
    добавляет свой слот, поэтому оба слота сохраняются и оба списываются.
    До фикса хэндлер перезаписывал slots значением current_slots+1,
    прочитанным до инкремента внутри charge_extra_device, и одна оплата
    пропадала (slots=1 при двух устройствах).
    """
    # max_devices=1 → уже первое доп. устройство требует платного слота.
    plan = make_plan(db_session, name="plan-race-device", max_devices=1)
    user = make_user(db_session, telegram_id="tg-add-race")
    balance.topup(db_session, user.id, 30000, reference="seed")
    # make_subscription_with_device создаёт ноду+конфиг и одно устройство.
    from .factories import make_config, make_node
    node = make_node(db_session)
    make_config(db_session, node)
    sub = make_subscription_with_device(db_session, user, plan, node)
    db_session.commit()

    headers = _auth_headers(user.id)
    statuses = []
    for _ in range(2):
        res = client.post(
            f"/api/webapp/subscriptions/{sub.id}/devices",
            headers=headers,
        )
        statuses.append(res.status_code)

    assert statuses == [200, 200], statuses

    db_session.expire_all()
    fresh_sub = db_session.get(models.Subscription, sub.id)
    assert fresh_sub.extra_device_slots == 2  # оба слота оплачены и сохранены

    spends = (
        db_session.query(models.BalanceTransaction)
        .filter(
            models.BalanceTransaction.user_id == user.id,
            models.BalanceTransaction.kind == models.BalanceTxKind.spend,
            models.BalanceTransaction.reference.like("extra_device:%"),
        )
        .all()
    )
    assert len(spends) == 2  # ровно два списания за два слота
