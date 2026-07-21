"""Lava.top payment provider (Stage 9b — карты РФ / СБП).

Публичное API lava.top (LAVALANE LTD, gate.lava.top, OpenAPI v1.22.0).
Референс интеграции: docs/PLAN_LAVA_TOP.md.

Особенности платформы, продиктовавшие дизайн:

* Динамическая сумма работает только у продукта с включённым в кабинете
  режимом «Цена по запросу через API» — его ``offerId`` задаётся в env.
* В инвойсе нет поля metadata/orderId: наш ``invoice_id`` round-trip'ится
  через ``clientUtm.utm_content`` (платформа возвращает его в вебхуке
  verbatim), а ``id`` контракта из ответа сохраняется как provider
  external_id (для матчинга Payment-строки при двойном checkout, #117).
* Email покупателя обязателен — генерим синтетический
  ``inv{invoice_id}@{LAVA_TOP_EMAIL_DOMAIN}`` (per-invoice адрес, чтобы
  антифрод платформы не сцеплял всех покупателей в одного «клиента»).
* Вебхук НЕ подписывается HMAC: платформа шлёт наш статический секрет
  (настраивается в кабинете, тип авторизации «API key») в заголовке
  ``X-Api-Key``. Сверка суммы/валюты происходит выше, в
  ``payment_webhook`` — драйвер отвечает только за аутентичность.
"""
from __future__ import annotations

import hmac
import json
import logging
import os

import requests

from .base import ProviderError, ProviderInvoice, WebhookEvent

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://gate.lava.top"

_SUPPORTED_CURRENCIES = ("RUB", "USD", "EUR")


class LavaTopProvider:
    name = "lava_top"

    def __init__(
        self,
        *,
        api_key: str,
        offer_id: str,
        webhook_secret: str,
        email_domain: str,
        api_base: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._offer_id = offer_id
        self._webhook_secret = webhook_secret
        self._email_domain = email_domain
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
        cur = (currency or "").upper()
        if cur == "RUR":
            cur = "RUB"
        if cur not in _SUPPORTED_CURRENCIES:
            raise ProviderError(f"lava_top: unsupported currency {currency!r}")
        # description/return_url платформой не поддерживаются — назначение
        # платежа задаётся названием продукта в кабинете (нейтральным,
        # Stage 9d), редиректа после оплаты у виджета нет.
        body = {
            "email": f"inv{invoice_id}@{self._email_domain}",
            "offerId": self._offer_id,
            "currency": cur,
            # Лимиты платформы: 50–1 000 000 ₽ (5–10 000 $/€) — суммы вне
            # диапазона отклонит сам API, отдельно не дублируем.
            "amount": round(float(amount), 2),
            "clientUtm": {"utm_content": str(invoice_id)},
        }
        try:
            resp = self._session.post(
                f"{self._api_base}/api/v3/invoice",
                json=body,
                headers={"X-Api-Key": self._api_key},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"lava_top: create failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(f"lava_top: non-JSON response: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"lava_top: HTTP {resp.status_code}: {data}")
        contract_id = data.get("id")
        pay_url = data.get("paymentUrl")
        if not contract_id or not pay_url:
            raise ProviderError(f"lava_top: response missing id/paymentUrl: {data}")
        return ProviderInvoice(
            external_id=str(contract_id),
            pay_url=pay_url,
            amount=amount,
            currency=cur,
            raw=data,
        )

    # ---------- webhook ----------

    def verify_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        provided = None
        for k, v in headers.items():
            if k.lower() == "x-api-key":
                provided = v
                break
        if not provided or not hmac.compare_digest(
            self._webhook_secret.encode("utf-8"), provided.encode("utf-8")
        ):
            raise ProviderError("lava_top: bad or missing X-Api-Key webhook secret")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("lava_top: malformed JSON body") from exc
        if not isinstance(payload, dict):
            # Валидный JSON, но не объект ([] / "ping" / 42) — контракт
            # ждёт ProviderError (→ 401), а не AttributeError (→ 500).
            raise ProviderError("lava_top: webhook body is not a JSON object")

        event_type = str(payload.get("eventType") or "")
        status_raw = str(payload.get("status") or "").lower()
        # Успех — только payment.success с финальным статусом контракта.
        # subscription-active включён на случай, если оффер в кабинете
        # переключат на подписочный тип: первый платёж подписки приходит
        # тем же payment.success, но со статусом subscription-active.
        if event_type == "payment.success" and status_raw in ("completed", "subscription-active"):
            normalized = "paid"
        elif event_type in ("payment.failed", "subscription.recurring.payment.failed"):
            normalized = "failed"
        else:
            normalized = "other"

        utm = payload.get("clientUtm")
        invoice_id = utm.get("utm_content") if isinstance(utm, dict) else None
        # Наш invoice_id — строка из цифр. Отсутствие ИЛИ нечисловое
        # значение (напр. clientUtm из чужой покупки по UTM-ссылке в том же
        # аккаунте: секрет вебхука аккаунт-wide) — не наш платёж.
        # payment_webhook делает int(external_id) только для status=="paid",
        # поэтому ACK'аем как "other"/"0": иначе int("summer_promo") → 400 и
        # платформа ретраит до бесконечности (до 20 попыток).
        if not invoice_id or not str(invoice_id).isdigit():
            logger.warning(
                "lava_top: webhook without our numeric invoice id "
                "(eventType=%s, contractId=%s, utm_content=%r) — ignored",
                event_type,
                payload.get("contractId"),
                invoice_id,
            )
            normalized = "other"
            invoice_id = "0"

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


def load_lava_top_env() -> dict:
    """Read LAVA_TOP_* env vars and return constructor kwargs.

    Raises ``ProviderError`` listing the missing vars (pattern of
    ``load_sbp_instance``) — checkout с недонастроенным провайдером
    должен падать сразу и внятно, а не на первом вебхуке.
    """
    values = {
        "api_key": os.getenv("LAVA_TOP_API_KEY"),
        "offer_id": os.getenv("LAVA_TOP_OFFER_ID"),
        "webhook_secret": os.getenv("LAVA_TOP_WEBHOOK_SECRET"),
        "email_domain": os.getenv("LAVA_TOP_EMAIL_DOMAIN"),
    }
    missing = [f"LAVA_TOP_{k.upper()}" for k, v in values.items() if not v]
    if missing:
        raise ProviderError(
            f"lava_top provider requires env vars: {', '.join(missing)}"
        )
    return {**values, "api_base": os.getenv("LAVA_TOP_API_BASE")}
