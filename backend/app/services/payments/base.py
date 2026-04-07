"""Payment provider contracts."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(RuntimeError):
    """Raised when a payment provider operation fails."""


@dataclass
class ProviderInvoice:
    external_id: str
    pay_url: str
    amount: float
    currency: str
    raw: dict | None = field(default=None, repr=False)


@dataclass
class WebhookEvent:
    external_id: str
    # Normalized status: "paid" | "failed" | "expired" | "other"
    status: str
    amount: float | None = None
    currency: str | None = None
    raw: dict | None = field(default=None, repr=False)


class PaymentProvider(Protocol):
    name: str

    def create_invoice(
        self,
        *,
        invoice_id: int,
        amount: float,
        currency: str,
        description: str | None = None,
        return_url: str | None = None,
    ) -> ProviderInvoice: ...

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent: ...


def get_provider(name: str | None = None) -> PaymentProvider:
    """Instantiate a provider by name.

    Defaults to ``PAYMENT_PROVIDER`` env var, which defaults to ``cryptobot``.
    """
    name = (name or os.getenv("PAYMENT_PROVIDER") or "cryptobot").lower()
    if name == "cryptobot":
        from .cryptobot import CryptoBotProvider

        token = os.getenv("CRYPTOBOT_TOKEN")
        if not token:
            raise ProviderError("CRYPTOBOT_TOKEN env var is required for cryptobot provider")
        return CryptoBotProvider(token=token)

    if name == "yookassa":
        from .yookassa import YooKassaProvider, _load_allowed_ips

        shop_id = os.getenv("YOOKASSA_SHOP_ID")
        secret_key = os.getenv("YOOKASSA_SECRET_KEY")
        if not shop_id or not secret_key:
            raise ProviderError(
                "YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY env vars are required for yookassa provider"
            )
        return YooKassaProvider(
            shop_id=shop_id,
            secret_key=secret_key,
            allowed_ips=_load_allowed_ips(),
        )

    if name in ("telegram_stars", "stars"):
        from .telegram_stars import TelegramStarsProvider, _load_from_env

        token, secret = _load_from_env()
        return TelegramStarsProvider(bot_token=token, webhook_secret=secret)

    raise ProviderError(f"Unknown payment provider: {name}")
