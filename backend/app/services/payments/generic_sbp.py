"""Generic SBP / aggregator webhook adapter (Stage 9c).

A lot of small Russian SBP / card aggregators expose the same minimal
contract:

  * ``create_invoice`` → POST a JSON body with ``order_id`` + ``amount``
    and get back ``{pay_url, external_id}``;
  * webhook → JSON body, signed with HMAC-SHA256 over the *raw* body
    using a shared secret, signature in a header.

Rather than write a class per aggregator, this module ships *one*
provider whose behaviour is fully driven by env vars. Each instance is
addressed as ``sbp:<slug>`` (see ``base.get_provider``); the slug picks
which set of env vars to read.

Required env vars per slug ``X``:

    SBP_X_HMAC_SECRET           shared secret used for HMAC-SHA256
    SBP_X_PAY_URL_TEMPLATE      str.format template, may use {invoice_id} / {amount}
    SBP_X_DISPLAY_NAME          name persisted to Payment.provider (defaults to ``sbp:X``)

Optional:

    SBP_X_CREATE_URL            if set, POST {invoice_id, amount, currency} to it
                                and use the response's ``pay_url`` / ``external_id``
                                instead of formatting the template.
    SBP_X_AUTH_HEADER           extra header on the create call, format ``Name:Value``
    SBP_X_SIGNATURE_HEADER      header name carrying the HMAC-SHA256 hex digest
                                (default: ``x-sbp-signature``)
    SBP_X_PAID_STATUSES         comma-separated list of payload ``status`` values
                                that count as paid (default: ``paid,success,succeeded``)

The aggregator MUST round-trip the ``invoice_id`` we send back in its
webhook payload — that is the only field we trust to find the right
``Invoice`` row.

Why this design: Stage 9b (the actual aggregator hunt) is non-code, and
we explicitly don't know which aggregator will land. Pinning the shape
in env vars means a new aggregator only needs `.env` edits, no code.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

import requests

from .base import ProviderError, ProviderInvoice, WebhookEvent

logger = logging.getLogger(__name__)


class GenericSBPProvider:
    def __init__(
        self,
        *,
        slug: str,
        hmac_secret: str,
        pay_url_template: str,
        display_name: str,
        create_url: str | None = None,
        auth_header: tuple[str, str] | None = None,
        signature_header: str = "x-sbp-signature",
        paid_statuses: tuple[str, ...] = ("paid", "success", "succeeded"),
    ) -> None:
        self._slug = slug
        self._hmac_secret = hmac_secret.encode("utf-8")
        self._pay_url_template = pay_url_template
        self.name = display_name
        self._create_url = create_url
        self._auth_header = auth_header
        self._signature_header = signature_header.lower()
        self._paid_statuses = {s.lower() for s in paid_statuses}
        self._session = requests.Session()

    # ---------- create ----------

    def create_invoice(
        self,
        *,
        invoice_id: int,
        amount: float,
        currency: str,
        description: str | None = None,
        return_url: str | None = None,
    ) -> ProviderInvoice:
        if self._create_url:
            body = {
                "invoice_id": invoice_id,
                "amount": f"{amount:.2f}",
                "currency": currency.upper(),
                # description is intentionally NOT forwarded — we want
                # the aggregator's stored description to stay neutral
                # (Stage 9d). Aggregators that need a label set it on
                # their side.
            }
            headers = {}
            if self._auth_header:
                headers[self._auth_header[0]] = self._auth_header[1]
            try:
                resp = self._session.post(self._create_url, json=body, headers=headers, timeout=15)
            except requests.RequestException as exc:
                raise ProviderError(f"SBP[{self._slug}] create failed: {exc}") from exc
            try:
                data = resp.json()
            except ValueError:
                raise ProviderError(
                    f"SBP[{self._slug}] non-JSON response: {resp.text[:200]}"
                )
            if resp.status_code >= 400:
                raise ProviderError(f"SBP[{self._slug}] HTTP {resp.status_code}: {data}")
            pay_url = data.get("pay_url") or data.get("payUrl") or data.get("url")
            external_id = str(data.get("external_id") or data.get("id") or invoice_id)
            if not pay_url:
                raise ProviderError(f"SBP[{self._slug}] missing pay_url: {data}")
            return ProviderInvoice(
                external_id=external_id,
                pay_url=pay_url,
                amount=amount,
                currency=currency,
                raw=data,
            )

        # Template mode — pure str.format, no HTTP. Useful for
        # aggregators that hand out one-shot static URLs out-of-band.
        try:
            pay_url = self._pay_url_template.format(
                invoice_id=invoice_id,
                amount=f"{amount:.2f}",
                currency=currency.upper(),
            )
        except (KeyError, IndexError) as exc:
            raise ProviderError(
                f"SBP[{self._slug}] pay_url_template references unknown placeholder: {exc}"
            ) from exc
        return ProviderInvoice(
            external_id=str(invoice_id),
            pay_url=pay_url,
            amount=amount,
            currency=currency,
            raw=None,
        )

    # ---------- webhook ----------

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        # Header lookups must be case-insensitive — clients are sloppy.
        provided = None
        for k, v in headers.items():
            if k.lower() == self._signature_header:
                provided = v
                break
        if not provided:
            raise ProviderError(
                f"SBP[{self._slug}]: missing {self._signature_header} header"
            )
        expected = hmac.new(self._hmac_secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, provided.lower()):
            raise ProviderError(f"SBP[{self._slug}]: invalid HMAC signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ProviderError(f"SBP[{self._slug}]: malformed JSON body") from exc

        invoice_id = payload.get("invoice_id") or payload.get("order_id")
        if not invoice_id:
            raise ProviderError(f"SBP[{self._slug}]: webhook has no invoice_id")
        status_raw = str(payload.get("status") or "").lower()
        normalized = "paid" if status_raw in self._paid_statuses else (
            "failed" if status_raw in {"failed", "canceled", "cancelled", "declined"} else
            "expired" if status_raw in {"expired", "timeout"} else
            "other"
        )

        amount: float | None = None
        try:
            if payload.get("amount") is not None:
                amount = float(payload["amount"])
        except (TypeError, ValueError):
            amount = None

        return WebhookEvent(
            external_id=str(invoice_id),
            status=normalized,
            amount=amount,
            currency=payload.get("currency"),
            raw=payload,
        )


def load_sbp_instance(slug: str) -> dict:
    """Read env vars for a given SBP slug and return a kwargs dict.

    Raises ``ProviderError`` if the required vars are missing.
    """
    if not slug:
        raise ProviderError(
            "SBP provider requires a slug, e.g. ``sbp:robokassa``"
        )
    norm = slug.upper()
    secret = os.getenv(f"SBP_{norm}_HMAC_SECRET")
    template = os.getenv(f"SBP_{norm}_PAY_URL_TEMPLATE")
    create_url = os.getenv(f"SBP_{norm}_CREATE_URL")
    if not secret:
        raise ProviderError(
            f"SBP_{norm}_HMAC_SECRET env var is required for sbp:{slug}"
        )
    if not template and not create_url:
        raise ProviderError(
            f"sbp:{slug} needs either SBP_{norm}_PAY_URL_TEMPLATE or SBP_{norm}_CREATE_URL"
        )

    auth_raw = os.getenv(f"SBP_{norm}_AUTH_HEADER")
    auth_header: tuple[str, str] | None = None
    if auth_raw and ":" in auth_raw:
        k, _, v = auth_raw.partition(":")
        auth_header = (k.strip(), v.strip())

    statuses_raw = os.getenv(f"SBP_{norm}_PAID_STATUSES")
    paid_statuses = (
        tuple(s.strip().lower() for s in statuses_raw.split(",") if s.strip())
        if statuses_raw
        else ("paid", "success", "succeeded")
    )

    return {
        "slug": slug,
        "hmac_secret": secret,
        "pay_url_template": template or "",
        "display_name": os.getenv(f"SBP_{norm}_DISPLAY_NAME") or f"sbp:{slug}",
        "create_url": create_url,
        "auth_header": auth_header,
        "signature_header": os.getenv(f"SBP_{norm}_SIGNATURE_HEADER") or "x-sbp-signature",
        "paid_statuses": paid_statuses,
    }
