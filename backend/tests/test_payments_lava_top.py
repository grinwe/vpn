"""Unit tests for the lava.top and Tribute payment drivers (Stage 9b).

Как и test_payment_providers.py — чистая логика без сети/БД:
``requests.Session`` подменяется, проверяются построение запроса,
парсинг ответа и верификация вебхука (секрет/подпись — критичная
поверхность: баг здесь = возможность пометить чужой счёт оплаченным).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest

from app.services.payments.base import ProviderError, get_provider


class _FakeResponse:
    def __init__(self, data: dict[str, Any], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data)

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.last_call: dict[str, Any] | None = None

    def post(self, url: str, json: dict | None = None, headers: dict | None = None, timeout: int | None = None):  # noqa: A002
        self.last_call = {
            "url": url,
            "json": json,
            "headers": headers or {},
            "timeout": timeout,
        }
        return self.response

    def get(self, url: str, headers: dict | None = None, timeout: int | None = None):
        self.last_call = {"url": url, "headers": headers or {}, "timeout": timeout}
        return self.response


def _lava(**overrides):
    from app.services.payments.lava_top import LavaTopProvider

    kwargs = {
        "api_key": "key",
        "offer_id": "836b9fc5-7ae9-4a27-9642-592bc44072b7",
        "webhook_secret": "hooksecret",
        "email_domain": "example.com",
    }
    kwargs.update(overrides)
    return LavaTopProvider(**kwargs)


def _tribute(**overrides):
    from app.services.payments.tribute import TributeProvider

    kwargs = {
        "api_key": "trbt-key",
        "order_title": "Пополнение баланса",
        "order_description": "Пополнение баланса личного кабинета",
    }
    kwargs.update(overrides)
    return TributeProvider(**kwargs)


# ===========================================================================
# lava.top — create
# ===========================================================================


def test_lava_top_create_invoice_happy_path() -> None:
    prov = _lava()
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse(
            {
                "id": "c0ffee00-1111-2222-3333-444455556666",
                "status": "new",
                "paymentUrl": "https://payment-widget-url",
            },
            status_code=201,
        )
    )

    inv = prov.create_invoice(invoice_id=42, amount=299.0, currency="RUB")
    assert inv.external_id == "c0ffee00-1111-2222-3333-444455556666"
    assert inv.pay_url == "https://payment-widget-url"

    call = prov._session.last_call  # type: ignore[attr-defined]
    assert call["url"].endswith("/api/v3/invoice")
    assert call["headers"]["X-Api-Key"] == "key"
    body = call["json"]
    assert body["email"] == "inv42@example.com"
    assert body["offerId"] == "836b9fc5-7ae9-4a27-9642-592bc44072b7"
    assert body["currency"] == "RUB"
    assert body["amount"] == 299.0
    # round-trip нашего invoice_id — единственный сквозной канал.
    assert body["clientUtm"] == {"utm_content": "42"}


def test_lava_top_create_invoice_rur_alias_and_bad_currency() -> None:
    prov = _lava()
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse({"id": "x", "paymentUrl": "https://p"}, status_code=201)
    )
    inv = prov.create_invoice(invoice_id=1, amount=100.0, currency="RUR")
    assert inv.currency == "RUB"

    with pytest.raises(ProviderError, match="unsupported currency"):
        prov.create_invoice(invoice_id=1, amount=100.0, currency="XTR")


def test_lava_top_create_invoice_http_error_and_missing_fields() -> None:
    prov = _lava()
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse({"error": "Amount is required"}, status_code=400)
    )
    with pytest.raises(ProviderError, match="HTTP 400"):
        prov.create_invoice(invoice_id=1, amount=100.0, currency="RUB")

    prov._session = _FakeSession(_FakeResponse({"status": "new"}, status_code=201))  # type: ignore[assignment]
    with pytest.raises(ProviderError, match="missing id/paymentUrl"):
        prov.create_invoice(invoice_id=1, amount=100.0, currency="RUB")


# ===========================================================================
# lava.top — reconcile (list_recent_invoices)
# ===========================================================================


def test_lava_top_list_recent_invoices_normalizes() -> None:
    prov = _lava()
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse(
            {
                "items": [
                    {
                        "id": "9ac65b2c-7fbc-4672-a27a-abd1b96fbaac",
                        "status": "COMPLETED",
                        "receipt": {"amount": 100.0, "currency": "RUB"},
                        "clientUtm": {"utm_content": "48"},
                    },
                    {
                        "id": "in-progress-1",
                        "status": "IN_PROGRESS",
                        "receipt": {"amount": 50.0, "currency": "RUB"},
                        "clientUtm": {"utm_content": "49"},
                    },
                    {
                        "id": "no-utm",
                        "status": "COMPLETED",
                        "receipt": {"amount": 200.0, "currency": "RUB"},
                        "clientUtm": None,
                    },
                ],
                "total": 3,
            }
        )
    )
    rows = prov.list_recent_invoices()
    assert prov._session.last_call["url"].endswith("/api/v2/invoices")  # type: ignore[attr-defined]
    assert prov._session.last_call["headers"]["X-Api-Key"] == "key"  # type: ignore[attr-defined]

    completed = rows[0]
    assert completed["invoice_id"] == 48
    assert completed["amount"] == 100.0
    assert completed["currency"] == "RUB"
    assert completed["contract_id"] == "9ac65b2c-7fbc-4672-a27a-abd1b96fbaac"
    assert completed["completed"] is True

    # Не-COMPLETED и без utm — попадают в список, но с флагами, по которым
    # тик их отфильтрует (completed=False / invoice_id=None).
    assert rows[1]["completed"] is False
    assert rows[2]["invoice_id"] is None


def test_lava_top_list_recent_invoices_http_error() -> None:
    prov = _lava()
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse({"error": "unauthorized"}, status_code=401)
    )
    with pytest.raises(ProviderError, match="invoices HTTP 401"):
        prov.list_recent_invoices()


# ===========================================================================
# lava.top — webhook
# ===========================================================================


def _lava_webhook_body(**overrides) -> bytes:
    payload = {
        "eventType": "payment.success",
        "contractId": "c0ffee00-1111-2222-3333-444455556666",
        "status": "completed",
        "amount": 299.0,
        "currency": "RUB",
        "clientUtm": {"utm_content": "42"},
        "buyer": {"email": "inv42@example.com"},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_lava_top_webhook_happy_path() -> None:
    prov = _lava()
    ev = prov.verify_webhook(_lava_webhook_body(), {"X-Api-Key": "hooksecret"})
    assert ev.status == "paid"
    assert ev.external_id == "42"
    assert ev.amount == 299.0
    assert ev.currency == "RUB"
    # provider contract id доступен вебхук-хендлеру через raw (#117).
    assert ev.raw["contractId"] == "c0ffee00-1111-2222-3333-444455556666"


def test_lava_top_webhook_rejects_wrong_or_missing_secret() -> None:
    prov = _lava()
    with pytest.raises(ProviderError, match="X-Api-Key"):
        prov.verify_webhook(_lava_webhook_body(), {"X-Api-Key": "nope"})
    with pytest.raises(ProviderError, match="X-Api-Key"):
        prov.verify_webhook(_lava_webhook_body(), {})


def test_lava_top_webhook_failed_and_intermediate_events() -> None:
    prov = _lava()
    ev = prov.verify_webhook(
        _lava_webhook_body(eventType="payment.failed", status="failed"),
        {"x-api-key": "hooksecret"},
    )
    assert ev.status == "failed"

    # payment.success с нефинальным статусом контракта — НЕ оплата.
    ev = prov.verify_webhook(
        _lava_webhook_body(status="in-progress"), {"x-api-key": "hooksecret"}
    )
    assert ev.status == "other"


def test_lava_top_webhook_without_utm_is_ignored_not_rejected() -> None:
    # Покупка не из нашего backend'а: доставку подтверждаем (иначе
    # платформа ретраит до бесконечности), но оплатой не считаем.
    prov = _lava()
    ev = prov.verify_webhook(
        _lava_webhook_body(clientUtm=None), {"x-api-key": "hooksecret"}
    )
    assert ev.status == "other"
    assert ev.external_id == "0"


def test_lava_top_webhook_non_numeric_utm_is_ignored() -> None:
    # Нечисловой utm_content (чужая покупка по UTM-ссылке в том же
    # аккаунте) НЕ должен уходить как paid: иначе payment_webhook падает
    # на int("summer_promo") → 400 → бесконечные ретраи.
    prov = _lava()
    ev = prov.verify_webhook(
        _lava_webhook_body(clientUtm={"utm_content": "summer_promo"}),
        {"x-api-key": "hooksecret"},
    )
    assert ev.status == "other"
    assert ev.external_id == "0"


def test_lava_top_webhook_malformed_json() -> None:
    prov = _lava()
    with pytest.raises(ProviderError, match="malformed"):
        prov.verify_webhook(b"not-json", {"x-api-key": "hooksecret"})


def test_lava_top_webhook_non_object_body_raises_not_crashes() -> None:
    # Валидный JSON, но не объект → ProviderError (→401), не AttributeError (→500).
    prov = _lava()
    for raw in (b"[]", b'"ping"', b"42"):
        with pytest.raises(ProviderError, match="not a JSON object"):
            prov.verify_webhook(raw, {"x-api-key": "hooksecret"})


# ===========================================================================
# Tribute — create
# ===========================================================================


def test_tribute_create_invoice_happy_path() -> None:
    prov = _tribute()
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse(
            {
                "uuid": "550e8400-e29b-41d4-a716-446655440000",
                "status": "pending",
                "paymentUrl": "https://tribute.tg/pay/xyz",
                "webappPaymentUrl": "https://t.me/tribute/app?startapp=xyz",
            }
        )
    )

    inv = prov.create_invoice(invoice_id=42, amount=299.0, currency="RUB")
    assert inv.external_id == "550e8400-e29b-41d4-a716-446655440000"
    # Браузерная ссылка приоритетнее webapp (Stars-only правило Telegram).
    assert inv.pay_url == "https://tribute.tg/pay/xyz"

    call = prov._session.last_call  # type: ignore[attr-defined]
    assert call["url"].endswith("/shop/orders")
    assert call["headers"]["Api-Key"] == "trbt-key"
    body = call["json"]
    # Сумма в НАИМЕНЬШИХ единицах (копейки), int.
    assert body["amount"] == 29900
    assert body["currency"] == "rub"
    assert body["customerId"] == "42"
    assert body["title"] == "Пополнение баланса"


def test_tribute_create_invoice_falls_back_to_webapp_url() -> None:
    prov = _tribute()
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse(
            {
                "uuid": "u-1",
                "paymentUrl": None,
                "webappPaymentUrl": "https://t.me/tribute/app?startapp=xyz",
            }
        )
    )
    inv = prov.create_invoice(invoice_id=1, amount=100.0, currency="rub")
    assert inv.pay_url == "https://t.me/tribute/app?startapp=xyz"


def test_tribute_create_invoice_bad_currency_and_http_error() -> None:
    prov = _tribute()
    with pytest.raises(ProviderError, match="unsupported currency"):
        prov.create_invoice(invoice_id=1, amount=100.0, currency="XTR")

    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse({"code": "error_not_found", "message": "shop not found"}, status_code=404)
    )
    with pytest.raises(ProviderError, match="HTTP 404"):
        prov.create_invoice(invoice_id=1, amount=100.0, currency="RUB")


# ===========================================================================
# Tribute — webhook
# ===========================================================================


def _tribute_webhook_body(name: str = "shop_order", **payload_overrides) -> bytes:
    payload = {
        "uuid": "550e8400-e29b-41d4-a716-446655440000",
        "shopId": 1,
        "amount": 29900,
        "currency": "rub",
        "fee": 2990,
        "status": "paid",
        "customerId": "42",
        "isRecurrent": False,
    }
    payload.update(payload_overrides)
    return json.dumps(
        {
            "name": name,
            "created_at": "2026-07-21T01:15:58.33246Z",
            "sent_at": "2026-07-21T01:15:58.54Z",
            "payload": payload,
        }
    ).encode()


def _trbt_sig(body: bytes, key: str = "trbt-key") -> str:
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def test_tribute_webhook_happy_path_hex_signature() -> None:
    prov = _tribute()
    body = _tribute_webhook_body()
    ev = prov.verify_webhook(body, {"trbt-signature": _trbt_sig(body)})
    assert ev.status == "paid"
    assert ev.external_id == "42"
    # Копейки → рубли для сверки суммы в payment_webhook.
    assert ev.amount == 299.0
    assert ev.currency == "rub"
    assert ev.raw["payload"]["uuid"] == "550e8400-e29b-41d4-a716-446655440000"


def test_tribute_webhook_accepts_base64_signature() -> None:
    prov = _tribute()
    body = _tribute_webhook_body()
    sig = base64.b64encode(
        hmac.new(b"trbt-key", body, hashlib.sha256).digest()
    ).decode()
    ev = prov.verify_webhook(body, {"TRBT-Signature": sig})
    assert ev.status == "paid"


def test_tribute_webhook_rejects_bad_or_missing_signature() -> None:
    prov = _tribute()
    body = _tribute_webhook_body()
    with pytest.raises(ProviderError, match="invalid trbt-signature"):
        prov.verify_webhook(body, {"trbt-signature": "deadbeef"})
    with pytest.raises(ProviderError, match="missing trbt-signature"):
        prov.verify_webhook(body, {})


def test_tribute_webhook_intermediate_event_is_not_paid() -> None:
    # shop_order_payment_received — фиат получен, но финал придёт
    # отдельным shop_order; спека прямо запрещает считать это оплатой.
    prov = _tribute()
    body = _tribute_webhook_body(name="shop_order_payment_received")
    ev = prov.verify_webhook(body, {"trbt-signature": _trbt_sig(body)})
    assert ev.status == "other"


def test_tribute_webhook_failed_events() -> None:
    prov = _tribute()
    body = _tribute_webhook_body(name="shop_order_payment_failed", status="failed")
    ev = prov.verify_webhook(body, {"trbt-signature": _trbt_sig(body)})
    assert ev.status == "failed"


def test_tribute_webhook_without_customer_id_is_ignored() -> None:
    prov = _tribute()
    body = _tribute_webhook_body(customerId=None)
    ev = prov.verify_webhook(body, {"trbt-signature": _trbt_sig(body)})
    assert ev.status == "other"
    assert ev.external_id == "0"


def test_tribute_webhook_non_numeric_customer_id_is_ignored() -> None:
    # Заказ другой интеграции на том же API-ключе: нечисловой customerId
    # не должен уходить как paid (иначе int() → 400 → ретраи).
    prov = _tribute()
    body = _tribute_webhook_body(customerId="user-abc")
    ev = prov.verify_webhook(body, {"trbt-signature": _trbt_sig(body)})
    assert ev.status == "other"
    assert ev.external_id == "0"


def test_tribute_webhook_non_object_body_raises_not_crashes() -> None:
    prov = _tribute()
    for raw in (b"[]", b'"ping"', b"42"):
        sig = _trbt_sig(raw)
        with pytest.raises(ProviderError, match="not a JSON object"):
            prov.verify_webhook(raw, {"trbt-signature": sig})


# ===========================================================================
# get_provider dispatch + env loading
# ===========================================================================


def test_get_provider_lava_top_requires_env(monkeypatch) -> None:
    for var in (
        "LAVA_TOP_API_KEY",
        "LAVA_TOP_OFFER_ID",
        "LAVA_TOP_WEBHOOK_SECRET",
        "LAVA_TOP_EMAIL_DOMAIN",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ProviderError, match="LAVA_TOP_API_KEY"):
        get_provider("lava_top")

    monkeypatch.setenv("LAVA_TOP_API_KEY", "k")
    monkeypatch.setenv("LAVA_TOP_OFFER_ID", "o")
    monkeypatch.setenv("LAVA_TOP_WEBHOOK_SECRET", "s")
    monkeypatch.setenv("LAVA_TOP_EMAIL_DOMAIN", "d.example")
    prov = get_provider("lava_top")
    assert prov.name == "lava_top"


def test_get_provider_tribute_requires_env(monkeypatch) -> None:
    monkeypatch.delenv("TRIBUTE_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="TRIBUTE_API_KEY"):
        get_provider("tribute")

    monkeypatch.setenv("TRIBUTE_API_KEY", "k")
    prov = get_provider("tribute")
    assert prov.name == "tribute"
    # Дефолтные нейтральные тексты заказа.
    assert prov._order_title == "Пополнение баланса"


def test_provider_invoice_id_extraction_for_new_drivers() -> None:
    # Матчинг Payment-строки по provider invoice id (#117) должен
    # понимать raw обоих новых драйверов.
    from app.api.payments import _provider_invoice_id_from_event
    from app.services.payments.base import WebhookEvent

    ev = WebhookEvent(external_id="42", status="paid", raw={"contractId": "abc-uuid"})
    assert _provider_invoice_id_from_event(ev) == "abc-uuid"

    ev = WebhookEvent(
        external_id="42", status="paid", raw={"name": "shop_order", "payload": {"uuid": "u-1"}}
    )
    assert _provider_invoice_id_from_event(ev) == "u-1"
