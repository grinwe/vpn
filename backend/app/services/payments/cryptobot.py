"""Crypto Pay API (``@CryptoBot``) payment provider.

API docs: https://help.crypt.bot/crypto-pay-api

We wrap the two endpoints we actually need (``createInvoice``) and verify
webhook signatures with HMAC-SHA256 as specified by the vendor. The token
must be kept outside of logs — we only ever pass it in the
``Crypto-Pay-API-Token`` header, never in query strings.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

import requests

from .base import ProviderError, ProviderInvoice, WebhookEvent

logger = logging.getLogger(__name__)

MAINNET_API = "https://pay.crypt.bot/api"
TESTNET_API = "https://testnet-pay.crypt.bot/api"

# Крипто-активы, которые Crypto Pay принимает в поле ``asset``. Всё остальное
# (RUB/USD/EUR/…) — фиат и задаётся парой ``currency_type=fiat`` + ``fiat``.
_CRYPTO_ASSETS = {
    "USDT",
    "TON",
    "BTC",
    "ETH",
    "LTC",
    "BNB",
    "TRX",
    "USDC",
    "JET",
    "GRAM",
    "DOGE",
    "SEND",
}


class CryptoBotProvider:
    name = "cryptobot"

    def __init__(self, token: str, *, testnet: bool = False) -> None:
        self._token = token
        self._api = TESTNET_API if testnet else MAINNET_API
        self._session = requests.Session()
        self._session.headers.update({"Crypto-Pay-API-Token": token})

    # ---------- createInvoice ----------

    def create_invoice(
        self,
        *,
        invoice_id: int,
        amount: float,
        currency: str,
        description: str | None = None,
        return_url: str | None = None,
    ) -> ProviderInvoice:
        # Crypto Pay различает крипто- и фиат-инвойсы: крипта задаётся полем
        # ``asset`` (USDT/TON/…), фиат — парой ``currency_type=fiat`` + ``fiat``
        # (RUB/USD/EUR/…). Наши счета почти все рублёвые, поэтому по умолчанию
        # неизвестный код трактуем как фиат, а исторический "USD" маппим в USDT.
        code = currency.upper()
        if code == "USD":
            code = "USDT"
        body: dict = {
            "amount": f"{amount:.2f}",
            "description": description or f"Order #{invoice_id}",
            # ``payload`` is returned verbatim in webhook events and lets us
            # look up the internal invoice without another round-trip.
            "payload": str(invoice_id),
            "allow_comments": False,
            "allow_anonymous": True,
        }
        if code in _CRYPTO_ASSETS:
            body["currency_type"] = "crypto"
            body["asset"] = code
        else:
            body["currency_type"] = "fiat"
            body["fiat"] = code
        if return_url:
            body["paid_btn_name"] = "openBot"
            body["paid_btn_url"] = return_url

        data = self._post("/createInvoice", body)
        result = data.get("result") or {}
        pay_url = result.get("pay_url") or result.get("bot_invoice_url")
        external_id = str(result.get("invoice_id") or result.get("hash") or "")
        if not pay_url or not external_id:
            raise ProviderError(f"Crypto Pay returned unexpected payload: {data}")
        return ProviderInvoice(
            external_id=external_id,
            pay_url=pay_url,
            amount=amount,
            currency=currency,
            raw=result,
        )

    # ---------- webhook ----------

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        signature = headers.get("crypto-pay-api-signature") or headers.get(
            "Crypto-Pay-Api-Signature"
        )
        if not signature:
            raise ProviderError("Missing Crypto-Pay-Api-Signature header")

        # Per spec: secret = sha256(token), then HMAC-SHA256 of the raw body.
        secret = hashlib.sha256(self._token.encode("utf-8")).digest()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ProviderError("Invalid Crypto-Pay webhook signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ProviderError("Malformed Crypto-Pay webhook body") from exc

        update_type = payload.get("update_type")
        invoice = payload.get("payload") or {}
        status_map = {
            "invoice_paid": "paid",
            "invoice_expired": "expired",
        }
        normalized = status_map.get(update_type, "other")

        # The invoice object uses ``payload`` (our own value) to round-trip
        # the internal invoice id that we set in create_invoice.
        external_id = str(invoice.get("payload") or invoice.get("invoice_id") or "")
        if not external_id:
            raise ProviderError("Crypto-Pay webhook has no payload/invoice_id")

        amount = None
        try:
            amount = float(invoice.get("amount")) if invoice.get("amount") else None
        except (TypeError, ValueError):
            amount = None
        currency = invoice.get("asset")

        return WebhookEvent(
            external_id=external_id,
            status=normalized,
            amount=amount,
            currency=currency,
            raw=payload,
        )

    # ---------- low level ----------

    def _post(self, path: str, body: dict) -> dict:
        try:
            resp = self._session.post(f"{self._api}{path}", json=body, timeout=15)
        except requests.RequestException as exc:
            raise ProviderError(f"Crypto Pay request failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(f"Crypto Pay returned non-JSON: {resp.text[:200]}")
        if not data.get("ok"):
            raise ProviderError(f"Crypto Pay error: {data}")
        return data
