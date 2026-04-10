"""Telegram Stars payment provider.

Telegram Stars (currency ``XTR``) are an in-platform payment method: the
user pays inside Telegram, there is no external pay-page, and no HMAC-signed
vendor webhook. The flow we use is:

1. Backend calls Bot API ``createInvoiceLink`` with ``currency=XTR`` and
   ``payload=<invoice_id>``; Telegram returns a ``t.me/$…`` link which we
   hand back as the "pay URL".
2. User taps the link → Telegram shows the native Stars checkout → the
   bot receives ``pre_checkout_query`` (answers ok) and then a
   ``successful_payment`` update.
3. The bot forwards the ``successful_payment`` payload to our own
   ``POST /api/payments/webhook/telegram_stars`` endpoint, protected by a
   shared secret header (``X-Telegram-Stars-Secret``).

The "webhook" is therefore *our* bot talking to *our* backend — authn is
a shared secret rather than a vendor signature. The provider abstraction
still fits cleanly: ``create_invoice`` returns a pay URL, ``verify_webhook``
validates + parses the forwarded payload.
"""
from __future__ import annotations

import hmac
import json
import logging
import os

import requests

from .base import ProviderError, ProviderInvoice, WebhookEvent

logger = logging.getLogger(__name__)

BOT_API_BASE = "https://api.telegram.org"


class TelegramStarsProvider:
    name = "telegram_stars"

    def __init__(self, bot_token: str, webhook_secret: str) -> None:
        self._bot_token = bot_token
        self._webhook_secret = webhook_secret
        self._session = requests.Session()

    # ---------- createInvoiceLink ----------

    def create_invoice(
        self,
        *,
        invoice_id: int,
        amount: float,
        currency: str,
        description: str | None = None,
        return_url: str | None = None,  # unused — Telegram handles UX in-app
    ) -> ProviderInvoice:
        # Stars are strictly integer units. Round up so we never undercharge
        # the user on fractional plans (0.5 Stars → 1 Star).
        stars = max(1, int(amount + 0.999))
        title = description or f"Order #{invoice_id}"
        # Title has a 32-char limit in Bot API; chop so the call doesn't 400.
        if len(title) > 32:
            title = title[:32]

        body = {
            "title": title,
            "description": description or f"Order #{invoice_id}",
            # ``payload`` round-trips verbatim in ``successful_payment`` →
            # we use it to recover the internal invoice id on the webhook.
            "payload": str(invoice_id),
            "currency": "XTR",
            "prices": [{"label": title, "amount": stars}],
        }

        data = self._post("/createInvoiceLink", body)
        pay_url = data.get("result")
        if not pay_url or not isinstance(pay_url, str):
            raise ProviderError(f"Telegram createInvoiceLink returned unexpected payload: {data}")
        # No server-side payment id is issued at creation — Stars only
        # mint one on successful_payment. Use our internal id as the
        # external id so the webhook round-trip finds the Payment row.
        return ProviderInvoice(
            external_id=str(invoice_id),
            pay_url=pay_url,
            amount=float(stars),
            currency="XTR",
            raw=data,
        )

    # ---------- webhook (bot → backend forward) ----------

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        provided = headers.get("x-telegram-stars-secret") or headers.get(
            "X-Telegram-Stars-Secret"
        )
        if not provided:
            raise ProviderError("Missing X-Telegram-Stars-Secret header")
        if not hmac.compare_digest(provided, self._webhook_secret):
            raise ProviderError("Invalid X-Telegram-Stars-Secret")

        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ProviderError("Malformed Telegram Stars webhook body") from exc

        # Accept either the bare ``successful_payment`` object or a full
        # update envelope with ``{"successful_payment": {...}}``. The bot
        # forwarder is ours, but being liberal here saves a foot-gun later.
        sp = payload.get("successful_payment") or payload
        invoice_payload = sp.get("invoice_payload")
        if not invoice_payload:
            raise ProviderError("Telegram Stars webhook has no invoice_payload")

        currency = sp.get("currency") or "XTR"
        if currency != "XTR":
            # Someone wired a non-Stars successful_payment to this route.
            raise ProviderError(f"Unexpected currency on Stars webhook: {currency}")

        total_amount = sp.get("total_amount")
        try:
            amount = float(total_amount) if total_amount is not None else None
        except (TypeError, ValueError):
            amount = None

        # successful_payment only arrives on success — there is no
        # ``canceled`` or ``expired`` variant to map here.
        return WebhookEvent(
            external_id=str(invoice_payload),
            status="paid",
            amount=amount,
            currency=currency,
            raw=payload,
        )

    # ---------- low level ----------

    def _post(self, method: str, body: dict) -> dict:
        url = f"{BOT_API_BASE}/bot{self._bot_token}{method}"
        try:
            resp = self._session.post(url, json=body, timeout=15)
        except requests.RequestException as exc:
            raise ProviderError(f"Telegram Bot API request failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(f"Telegram Bot API returned non-JSON: {resp.text[:200]}")
        if not data.get("ok"):
            raise ProviderError(f"Telegram Bot API error: {data}")
        return data


def _load_from_env() -> tuple[str, str]:
    # Accept both BOT_TOKEN (the canonical name used by the bot service +
    # docker-compose) and TELEGRAM_BOT_TOKEN (a legacy alias kept for back
    # compat). If neither is set, the provider can't talk to Bot API.
    token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    secret = os.getenv("TELEGRAM_STARS_WEBHOOK_SECRET")
    if not token:
        raise ProviderError(
            "BOT_TOKEN env var is required for telegram_stars provider"
        )
    if not secret:
        raise ProviderError(
            "TELEGRAM_STARS_WEBHOOK_SECRET env var is required for telegram_stars provider"
        )
    return token, secret
