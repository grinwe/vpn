"""YooKassa payment provider.

API docs: https://yookassa.ru/developers/api

YooKassa uses HTTP Basic auth (``shop_id`` : ``secret_key``) and requires an
``Idempotence-Key`` on every mutating call so retries don't create duplicate
payments. Webhooks are *not* HMAC-signed — YooKassa instead asks integrators
to restrict the webhook endpoint to their published IP ranges. We enforce
that at the edge (nginx / reverse proxy), and optionally here via
``YOOKASSA_ALLOWED_IPS`` as a belt-and-braces check.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import uuid

import requests

from .base import ProviderError, ProviderInvoice, WebhookEvent

logger = logging.getLogger(__name__)

API_BASE = "https://api.yookassa.ru/v3"


class YooKassaProvider:
    name = "yookassa"

    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        *,
        allowed_ips: list[str] | None = None,
    ) -> None:
        self._shop_id = shop_id
        self._secret_key = secret_key
        self._session = requests.Session()
        self._session.auth = (shop_id, secret_key)
        # Parse once; empty list means "skip the IP check".
        self._allowed_networks = [
            ipaddress.ip_network(cidr, strict=False) for cidr in (allowed_ips or [])
        ]

    # ---------- createPayment ----------

    def create_invoice(
        self,
        *,
        invoice_id: int,
        amount: float,
        currency: str,
        description: str | None = None,
        return_url: str | None = None,
    ) -> ProviderInvoice:
        body = {
            "amount": {
                # YooKassa is strict: value must be a decimal string with
                # exactly 2 fraction digits for RUB.
                "value": f"{amount:.2f}",
                "currency": currency.upper(),
            },
            "capture": True,
            "description": description or f"VPN invoice #{invoice_id}",
            # ``metadata`` round-trips back on the webhook so we can find the
            # original Invoice row without guessing from the external id.
            "metadata": {"invoice_id": str(invoice_id)},
            "confirmation": {
                "type": "redirect",
                # YooKassa rejects the call if return_url is missing for the
                # redirect flow; fall back to a neutral URL so integration
                # tests / CLI probes don't need to thread a bot link through.
                "return_url": return_url or "https://t.me",
            },
        }

        # Idempotence key scoped to the internal invoice id so a retry of
        # the same invoice re-uses the same YooKassa payment instead of
        # opening a second one.
        idem_key = f"inv-{invoice_id}-{uuid.uuid4().hex[:8]}"
        data = self._post("/payments", body, idempotence_key=idem_key)

        external_id = str(data.get("id") or "")
        confirmation = data.get("confirmation") or {}
        pay_url = confirmation.get("confirmation_url")
        if not external_id or not pay_url:
            raise ProviderError(f"YooKassa returned unexpected payload: {data}")
        return ProviderInvoice(
            external_id=external_id,
            pay_url=pay_url,
            amount=amount,
            currency=currency,
            raw=data,
        )

    # ---------- webhook ----------

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        # YooKassa doesn't sign webhooks — authentication relies on the
        # caller's IP being in the published allowlist. We accept the IP
        # through a trusted header (``X-Real-IP`` / ``X-Forwarded-For``
        # set by our own reverse proxy) and only enforce the check when the
        # integrator has configured an allowlist.
        if self._allowed_networks:
            remote = (
                headers.get("x-real-ip")
                or headers.get("X-Real-IP")
                or (headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            )
            if not remote:
                raise ProviderError("YooKassa webhook: no client IP in headers")
            try:
                remote_ip = ipaddress.ip_address(remote)
            except ValueError as exc:
                raise ProviderError(f"YooKassa webhook: bad client IP {remote!r}") from exc
            if not any(remote_ip in net for net in self._allowed_networks):
                raise ProviderError(f"YooKassa webhook: IP {remote} not in allowlist")

        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ProviderError("Malformed YooKassa webhook body") from exc

        event = payload.get("event")
        obj = payload.get("object") or {}
        status_map = {
            "payment.succeeded": "paid",
            "payment.canceled": "failed",
            "payment.waiting_for_capture": "other",
        }
        normalized = status_map.get(event, "other")

        # Prefer metadata.invoice_id (our own) so the API layer can look the
        # Invoice row up directly; fall back to the YooKassa payment id.
        metadata = obj.get("metadata") or {}
        external_id = str(metadata.get("invoice_id") or obj.get("id") or "")
        if not external_id:
            raise ProviderError("YooKassa webhook has no metadata.invoice_id/id")

        amount_obj = obj.get("amount") or {}
        amount: float | None
        try:
            amount = float(amount_obj["value"]) if "value" in amount_obj else None
        except (TypeError, ValueError):
            amount = None
        currency = amount_obj.get("currency")

        return WebhookEvent(
            external_id=external_id,
            status=normalized,
            amount=amount,
            currency=currency,
            raw=payload,
        )

    # ---------- low level ----------

    def _post(self, path: str, body: dict, *, idempotence_key: str) -> dict:
        try:
            resp = self._session.post(
                f"{API_BASE}{path}",
                json=body,
                headers={"Idempotence-Key": idempotence_key},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"YooKassa request failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(f"YooKassa returned non-JSON: {resp.text[:200]}")
        # YooKassa returns 4xx with an error object — surface it verbatim
        # so the operator can see ``type``/``code``/``description``.
        if resp.status_code >= 400 or data.get("type") == "error":
            raise ProviderError(f"YooKassa error ({resp.status_code}): {data}")
        return data


def _load_allowed_ips() -> list[str]:
    raw = os.getenv("YOOKASSA_ALLOWED_IPS", "").strip()
    if not raw:
        return []
    return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
