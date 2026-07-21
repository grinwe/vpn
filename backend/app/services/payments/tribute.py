"""Tribute (tribute.tg) Shop API payment provider — карты РФ / СБП / Stars.

Референс интеграции: docs/PLAN_LAVA_TOP.md (раздел «Альтернативы»).
API: https://tribute.tg/api/v1, OpenAPI: /api/v1/openapi/shop/en.

Дизайн-факты платформы:

* ``POST /shop/orders`` принимает произвольную сумму **в копейках/центах**
  (int, smallest units) — конвертируем из рублёвого float на входе и
  обратно в вебхуке.
* ``customerId`` (свободная строка ≤256) возвращается в вебхуке —
  используем как round-trip нашего ``invoice_id``. ``uuid`` заказа из
  ответа сохраняется как provider external_id (матчинг Payment, #117).
* ``title``/``description`` обязательны — берём нейтральные строки из
  env, а не описание счёта (Stage 9d: назначение платежа на стороне
  агрегатора остаётся нейтральным).
* Подпись вебхука: заголовок ``trbt-signature`` = HMAC-SHA256 сырого
  тела, ключ — API key. Кодировка в доке не зафиксирована — принимаем
  hex и base64.
* Финальное подтверждение оплаты — ТОЛЬКО событие ``shop_order``
  (payload.status=paid). Промежуточный ``shop_order_payment_received``
  спека прямо запрещает считать оплатой — маппится в ``other``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os

import requests

from .base import ProviderError, ProviderInvoice, WebhookEvent

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://tribute.tg/api/v1"

_SUPPORTED_CURRENCIES = ("rub", "usd", "eur")

_FAILED_EVENTS = {
    "shop_order_payment_failed",
    "shop_order_cancelled",
    "shop_order_refunded",
}


class TributeProvider:
    name = "tribute"

    def __init__(
        self,
        *,
        api_key: str,
        order_title: str,
        order_description: str,
        api_base: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._order_title = order_title
        self._order_description = order_description
        self._api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
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
        cur = (currency or "").lower()
        if cur == "rur":
            cur = "rub"
        if cur not in _SUPPORTED_CURRENCIES:
            raise ProviderError(f"tribute: unsupported currency {currency!r}")
        body = {
            "amount": int(round(float(amount) * 100)),
            "currency": cur,
            "title": self._order_title,
            "description": self._order_description,
            "customerId": str(invoice_id),
        }
        if return_url:
            body["successUrl"] = return_url
        try:
            resp = self._session.post(
                f"{self._api_base}/shop/orders",
                json=body,
                headers={"Api-Key": self._api_key},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"tribute: create failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(f"tribute: non-JSON response: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"tribute: HTTP {resp.status_code}: {data}")
        order_uuid = data.get("uuid")
        # paymentUrl — оплата в браузере (вне Telegram-приложений: карты за
        # цифровые услуги внутри TG нарушают Stars-only правило Telegram);
        # webappPaymentUrl — фолбэк, если браузерная ссылка не выдана.
        pay_url = data.get("paymentUrl") or data.get("webappPaymentUrl")
        if not order_uuid or not pay_url:
            raise ProviderError(f"tribute: response missing uuid/paymentUrl: {data}")
        return ProviderInvoice(
            external_id=str(order_uuid),
            pay_url=pay_url,
            amount=amount,
            currency=cur,
            raw=data,
        )

    # ---------- webhook ----------

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        provided = None
        for k, v in headers.items():
            if k.lower() == "trbt-signature":
                provided = v
                break
        if not provided:
            raise ProviderError("tribute: missing trbt-signature header")
        digest = hmac.new(self._api_key.encode("utf-8"), body, hashlib.sha256)
        hex_ok = hmac.compare_digest(digest.hexdigest(), provided.strip().lower())
        b64_ok = hmac.compare_digest(
            base64.b64encode(digest.digest()).decode("ascii"), provided.strip()
        )
        if not (hex_ok or b64_ok):
            raise ProviderError("tribute: invalid trbt-signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("tribute: malformed JSON body") from exc
        if not isinstance(payload, dict):
            # Валидный JSON, но не объект — контракт ждёт ProviderError
            # (→ 401), а не AttributeError на payload.get (→ 500).
            raise ProviderError("tribute: webhook body is not a JSON object")

        event_name = str(payload.get("name") or "")
        inner = payload.get("payload")
        if not isinstance(inner, dict):
            inner = {}

        if event_name == "shop_order" and str(inner.get("status") or "").lower() == "paid":
            normalized = "paid"
        elif event_name in _FAILED_EVENTS:
            normalized = "failed"
        else:
            # В т.ч. shop_order_payment_received: фиат получен, но заказ ещё
            # не подтверждён — финал придёт отдельным shop_order.
            normalized = "other"

        customer_id = inner.get("customerId")
        # Наш customerId — строка из цифр (invoice_id). Отсутствие ИЛИ
        # нечисловое значение (заказ другой интеграции на том же API-ключе,
        # или ручной заказ в дашборде) — не наш платёж. ACK'аем как
        # "other"/"0": иначе payment_webhook падает на int("user-abc") → 400
        # и платформа ретраит (exponential backoff ~24ч).
        if not customer_id or not str(customer_id).isdigit():
            logger.warning(
                "tribute: webhook without our numeric customerId "
                "(name=%s, uuid=%s, customerId=%r) — ignored",
                event_name,
                inner.get("uuid"),
                customer_id,
            )
            normalized = "other"
            customer_id = "0"

        amount: float | None = None
        try:
            if inner.get("amount") is not None:
                # smallest units (копейки/центы) → мажорные единицы.
                amount = int(inner["amount"]) / 100
        except (TypeError, ValueError):
            amount = None

        return WebhookEvent(
            external_id=str(customer_id),
            status=normalized,
            amount=amount,
            currency=inner.get("currency"),
            raw=payload,
        )


def load_tribute_env() -> dict:
    """Read TRIBUTE_* env vars and return constructor kwargs.

    Только ключ обязателен; title/description имеют нейтральные дефолты.
    """
    api_key = os.getenv("TRIBUTE_API_KEY")
    if not api_key:
        raise ProviderError("tribute provider requires env var: TRIBUTE_API_KEY")
    return {
        "api_key": api_key,
        "order_title": os.getenv("TRIBUTE_ORDER_TITLE") or "Пополнение баланса",
        "order_description": os.getenv("TRIBUTE_ORDER_DESCRIPTION")
        or "Пополнение баланса личного кабинета",
        "api_base": os.getenv("TRIBUTE_API_BASE"),
    }
