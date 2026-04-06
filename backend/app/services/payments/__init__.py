"""Payment provider abstraction.

Today the only real provider is :class:`CryptoBotProvider`, which speaks the
Crypto Pay API used by the @CryptoBot Telegram bot. Adding YooKassa /
Telegram Stars / Stripe later is a matter of writing a new class that
implements :class:`PaymentProvider` and registering it in :data:`PROVIDERS`.
"""
from __future__ import annotations

from .base import (
    PaymentProvider,
    ProviderInvoice,
    ProviderError,
    WebhookEvent,
    get_provider,
)

__all__ = [
    "PaymentProvider",
    "ProviderInvoice",
    "ProviderError",
    "WebhookEvent",
    "get_provider",
]
