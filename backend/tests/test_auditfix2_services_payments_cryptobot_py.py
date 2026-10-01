"""Тесты формы тела запроса CryptoBotProvider.create_invoice.

Проверяем находку #115: рублёвые (и прочие фиат) счета должны уходить в
Crypto Pay как ``currency_type=fiat`` + ``fiat=<код>`` без поля ``asset``,
а крипто-инвойсы — как ``currency_type=crypto`` + ``asset``.
"""
from __future__ import annotations

from app.services.payments.cryptobot import CryptoBotProvider


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _make_provider(captured: dict):
    provider = CryptoBotProvider(token="dummy-token", testnet=True)

    def fake_post(url, json=None, timeout=None):  # noqa: A002 - имитируем requests
        captured["url"] = url
        captured["body"] = json
        return _FakeResp(
            {
                "ok": True,
                "result": {
                    "invoice_id": 777,
                    "pay_url": "https://t.me/CryptoBot?invoice=777",
                },
            }
        )

    provider._session.post = fake_post  # type: ignore[assignment]
    return provider


def test_rub_invoice_uses_fiat_currency_type():
    captured: dict = {}
    provider = _make_provider(captured)

    inv = provider.create_invoice(invoice_id=42, amount=199.0, currency="RUB")

    body = captured["body"]
    assert body["currency_type"] == "fiat"
    assert body["fiat"] == "RUB"
    assert "asset" not in body
    assert body["amount"] == "199.00"
    assert body["payload"] == "42"
    assert inv.external_id == "777"


def test_usdt_invoice_uses_crypto_asset():
    captured: dict = {}
    provider = _make_provider(captured)

    provider.create_invoice(invoice_id=43, amount=5.0, currency="USDT")

    body = captured["body"]
    assert body["currency_type"] == "crypto"
    assert body["asset"] == "USDT"
    assert "fiat" not in body


def test_usd_maps_to_usdt_crypto():
    captured: dict = {}
    provider = _make_provider(captured)

    provider.create_invoice(invoice_id=44, amount=5.0, currency="USD")

    body = captured["body"]
    assert body["currency_type"] == "crypto"
    assert body["asset"] == "USDT"
