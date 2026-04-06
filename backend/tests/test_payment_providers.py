"""Unit tests for payment provider drivers.

These are pure logic — no DB, no HTTP. Each provider's ``requests.Session``
is monkeypatched so the tests exercise request construction, response
parsing, and webhook verification (especially signature / secret paths)
without touching the network.

Webhook verification is the critical surface here: a bug that accepts a
bad signature in prod means attackers can mark any invoice paid. We
deliberately feed malformed payloads, wrong secrets, and off-by-one
header names at each verifier.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from app.services.payments.base import ProviderError


# ---------------------------------------------------------------------------
# Helpers — a minimal stand-in for requests.Response that providers inspect.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: dict[str, Any], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data)

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeSession:
    """Records the last call and returns a canned response."""

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.last_call: dict[str, Any] | None = None
        self.headers: dict[str, str] = {}
        self.auth: tuple[str, str] | None = None

    def post(self, url: str, json: dict | None = None, headers: dict | None = None, timeout: int | None = None):  # noqa: A002
        self.last_call = {
            "url": url,
            "json": json,
            "headers": headers or {},
            "timeout": timeout,
        }
        return self.response


# ===========================================================================
# Crypto Pay
# ===========================================================================


def test_cryptobot_create_invoice_happy_path() -> None:
    from app.services.payments.cryptobot import CryptoBotProvider

    prov = CryptoBotProvider(token="tok")
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse(
            {
                "ok": True,
                "result": {
                    "invoice_id": "987",
                    "pay_url": "https://pay.crypt.bot/IV987",
                },
            }
        )
    )

    inv = prov.create_invoice(invoice_id=42, amount=9.99, currency="USD")
    assert inv.external_id == "987"
    assert inv.pay_url == "https://pay.crypt.bot/IV987"

    # USD → USDT mapping and payload round-tripping.
    body = prov._session.last_call["json"]  # type: ignore[attr-defined]
    assert body["asset"] == "USDT"
    assert body["payload"] == "42"
    assert body["amount"] == "9.99"


def test_cryptobot_verify_webhook_accepts_good_signature() -> None:
    from app.services.payments.cryptobot import CryptoBotProvider

    prov = CryptoBotProvider(token="tok")
    body = json.dumps(
        {
            "update_type": "invoice_paid",
            "payload": {"payload": "42", "amount": "9.99", "asset": "USDT"},
        }
    ).encode()
    secret = hashlib.sha256(b"tok").digest()
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()

    ev = prov.verify_webhook(body, {"crypto-pay-api-signature": sig})
    assert ev.status == "paid"
    assert ev.external_id == "42"
    assert ev.amount == 9.99


def test_cryptobot_verify_webhook_rejects_bad_signature() -> None:
    from app.services.payments.cryptobot import CryptoBotProvider

    prov = CryptoBotProvider(token="tok")
    body = b'{"update_type":"invoice_paid","payload":{"payload":"42"}}'
    with pytest.raises(ProviderError, match="Invalid"):
        prov.verify_webhook(body, {"crypto-pay-api-signature": "deadbeef"})


def test_cryptobot_verify_webhook_missing_header() -> None:
    from app.services.payments.cryptobot import CryptoBotProvider

    prov = CryptoBotProvider(token="tok")
    with pytest.raises(ProviderError, match="Missing"):
        prov.verify_webhook(b"{}", {})


# ===========================================================================
# YooKassa
# ===========================================================================


def test_yookassa_create_invoice_happy_path() -> None:
    from app.services.payments.yookassa import YooKassaProvider

    prov = YooKassaProvider(shop_id="SID", secret_key="SEC")
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse(
            {
                "id": "2c85d..",
                "status": "pending",
                "confirmation": {"confirmation_url": "https://yookassa/pay/xyz"},
            }
        )
    )

    inv = prov.create_invoice(
        invoice_id=7,
        amount=499.00,
        currency="RUB",
        return_url="https://t.me/mybot",
    )
    assert inv.external_id == "2c85d.."
    assert inv.pay_url == "https://yookassa/pay/xyz"

    body = prov._session.last_call["json"]  # type: ignore[attr-defined]
    assert body["amount"] == {"value": "499.00", "currency": "RUB"}
    assert body["metadata"] == {"invoice_id": "7"}
    assert body["confirmation"]["return_url"] == "https://t.me/mybot"
    # Idempotency header must be present so retries don't double-charge.
    assert prov._session.last_call["headers"]["Idempotence-Key"].startswith("inv-7-")  # type: ignore[attr-defined]


def test_yookassa_verify_webhook_paid_event() -> None:
    from app.services.payments.yookassa import YooKassaProvider

    prov = YooKassaProvider(shop_id="SID", secret_key="SEC")
    body = json.dumps(
        {
            "event": "payment.succeeded",
            "object": {
                "id": "pay_xyz",
                "status": "succeeded",
                "amount": {"value": "499.00", "currency": "RUB"},
                "metadata": {"invoice_id": "7"},
            },
        }
    ).encode()

    ev = prov.verify_webhook(body, {})  # no allowlist → no IP check
    assert ev.status == "paid"
    # metadata.invoice_id wins over object.id so the API route can look up
    # the Invoice row directly.
    assert ev.external_id == "7"
    assert ev.amount == 499.00
    assert ev.currency == "RUB"


def test_yookassa_verify_webhook_ip_allowlist_rejects_outsider() -> None:
    from app.services.payments.yookassa import YooKassaProvider

    prov = YooKassaProvider(
        shop_id="SID",
        secret_key="SEC",
        allowed_ips=["185.71.76.0/27"],
    )
    body = json.dumps(
        {
            "event": "payment.succeeded",
            "object": {
                "id": "pay_xyz",
                "amount": {"value": "1.00", "currency": "RUB"},
                "metadata": {"invoice_id": "1"},
            },
        }
    ).encode()

    with pytest.raises(ProviderError, match="not in allowlist"):
        prov.verify_webhook(body, {"x-real-ip": "1.2.3.4"})


def test_yookassa_verify_webhook_ip_allowlist_accepts_insider() -> None:
    from app.services.payments.yookassa import YooKassaProvider

    prov = YooKassaProvider(
        shop_id="SID",
        secret_key="SEC",
        allowed_ips=["185.71.76.0/27"],
    )
    body = json.dumps(
        {
            "event": "payment.succeeded",
            "object": {
                "id": "pay_xyz",
                "amount": {"value": "1.00", "currency": "RUB"},
                "metadata": {"invoice_id": "1"},
            },
        }
    ).encode()

    ev = prov.verify_webhook(body, {"x-real-ip": "185.71.76.5"})
    assert ev.status == "paid"


def test_yookassa_error_response_raises() -> None:
    from app.services.payments.yookassa import YooKassaProvider

    prov = YooKassaProvider(shop_id="SID", secret_key="SEC")
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse(
            {"type": "error", "code": "invalid_request", "description": "amount too low"},
            status_code=400,
        )
    )

    with pytest.raises(ProviderError, match="YooKassa error"):
        prov.create_invoice(invoice_id=1, amount=0.01, currency="RUB")


# ===========================================================================
# Telegram Stars
# ===========================================================================


def test_telegram_stars_create_invoice_rounds_up_to_integer() -> None:
    from app.services.payments.telegram_stars import TelegramStarsProvider

    prov = TelegramStarsProvider(bot_token="BOT:TOKEN", webhook_secret="shh")
    prov._session = _FakeSession(  # type: ignore[assignment]
        _FakeResponse({"ok": True, "result": "https://t.me/$abc"})
    )

    inv = prov.create_invoice(invoice_id=11, amount=0.5, currency="XTR")
    assert inv.pay_url == "https://t.me/$abc"
    # Stars are integer units → 0.5 rounds up to 1.
    assert inv.amount == 1.0
    # external_id uses our internal id since Telegram has no payment id
    # at creation time.
    assert inv.external_id == "11"

    body = prov._session.last_call["json"]  # type: ignore[attr-defined]
    assert body["currency"] == "XTR"
    assert body["prices"] == [{"label": body["title"], "amount": 1}]
    assert body["payload"] == "11"


def test_telegram_stars_verify_webhook_happy_path() -> None:
    from app.services.payments.telegram_stars import TelegramStarsProvider

    prov = TelegramStarsProvider(bot_token="BOT:TOKEN", webhook_secret="shh")
    body = json.dumps(
        {
            "invoice_payload": "11",
            "total_amount": 1,
            "currency": "XTR",
            "telegram_payment_charge_id": "xtr_charge_1",
        }
    ).encode()

    ev = prov.verify_webhook(body, {"x-telegram-stars-secret": "shh"})
    assert ev.status == "paid"
    assert ev.external_id == "11"
    assert ev.currency == "XTR"


def test_telegram_stars_verify_webhook_wrong_secret() -> None:
    from app.services.payments.telegram_stars import TelegramStarsProvider

    prov = TelegramStarsProvider(bot_token="BOT:TOKEN", webhook_secret="shh")
    body = b'{"invoice_payload":"11","currency":"XTR"}'
    with pytest.raises(ProviderError, match="Invalid X-Telegram-Stars-Secret"):
        prov.verify_webhook(body, {"x-telegram-stars-secret": "nope"})


def test_telegram_stars_verify_webhook_rejects_non_stars_currency() -> None:
    from app.services.payments.telegram_stars import TelegramStarsProvider

    prov = TelegramStarsProvider(bot_token="BOT:TOKEN", webhook_secret="shh")
    body = json.dumps({"invoice_payload": "11", "currency": "USD"}).encode()
    with pytest.raises(ProviderError, match="Unexpected currency"):
        prov.verify_webhook(body, {"x-telegram-stars-secret": "shh"})
