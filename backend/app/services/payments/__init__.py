"""Payment provider abstraction.

Providers: CryptoBotProvider (Crypto Pay API), TelegramStarsProvider
(in-bot XTR payments), GenericSBPProvider (SBP via configurable HTTP API).
Adding a new provider is a matter of writing a class that implements
:class:`PaymentProvider` and registering it in :func:`get_provider`.
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
