"""Payment provider contracts."""
from __future__ import annotations

import os
import random
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


def list_available_providers() -> list[str]:
    """Return the configured rotation pool.

    Stage 9a: ``PAYMENT_PROVIDERS`` is a comma-separated list, falling
    back to ``PAYMENT_PROVIDER`` (single-value, legacy) and finally to
    ``cryptobot``. Names are normalized to lower-case and de-duplicated
    in declaration order so a stale entry can't shadow a fresh one.
    """
    raw = os.getenv("PAYMENT_PROVIDERS")
    if raw:
        names = [chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()]
    else:
        names = [(os.getenv("PAYMENT_PROVIDER") or "cryptobot").lower()]
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def pick_provider_name(rng: random.Random | None = None) -> str:
    """Pick one provider name from the rotation pool.

    Stage 9a: random selection. The pool is read from env every call so
    operators can flip a provider on/off without restarting the API.
    Persisted on ``Invoice.provider`` / ``Payment.provider`` so the
    webhook still routes to the right verifier.
    """
    pool = list_available_providers()
    if not pool:
        raise ProviderError("No payment providers configured")
    if len(pool) == 1:
        return pool[0]
    return (rng or random).choice(pool)


def get_provider(name: str | None = None) -> PaymentProvider:
    """Instantiate a provider by name.

    If ``name`` is None or empty, picks one from the rotation pool
    (Stage 9a).
    """
    if not name:
        name = pick_provider_name()
    name = name.lower()
    # Stage 9c: generic_sbp instances are addressed as ``sbp:<slug>``
    # so a single backend can fan out to multiple aggregators.
    if name.startswith("sbp:") or name == "generic_sbp":
        from .generic_sbp import GenericSBPProvider, load_sbp_instance

        slug = name.split(":", 1)[1] if ":" in name else ""
        cfg = load_sbp_instance(slug)
        return GenericSBPProvider(**cfg)

    if name == "cryptobot":
        from .cryptobot import CryptoBotProvider

        token = os.getenv("CRYPTOBOT_TOKEN")
        if not token:
            raise ProviderError("CRYPTOBOT_TOKEN env var is required for cryptobot provider")
        return CryptoBotProvider(token=token)

    if name in ("telegram_stars", "stars"):
        from .telegram_stars import TelegramStarsProvider, _load_from_env

        token, secret = _load_from_env()
        return TelegramStarsProvider(bot_token=token, webhook_secret=secret)

    # Stage 9b: карточные/СБП провайдеры (docs/PLAN_LAVA_TOP.md).
    if name == "lava_top":
        from .lava_top import LavaTopProvider, load_lava_top_env

        return LavaTopProvider(**load_lava_top_env())

    if name == "tribute":
        from .tribute import TributeProvider, load_tribute_env

        return TributeProvider(**load_tribute_env())

    raise ProviderError(f"Unknown payment provider: {name}")
