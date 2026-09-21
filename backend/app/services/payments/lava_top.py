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
import time

import requests

from .base import ProviderError, ProviderInvoice, WebhookEvent

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://gate.lava.top"

_SUPPORTED_CURRENCIES = ("RUB", "USD", "EUR")


def _coerce_amount(value) -> float | None:
    """Число или строка (``"100.00"``) → float, иначе ``None``.

    Платформы охотно меняют тип поля между релизами; строгий
    ``isinstance(value, (int, float))`` превращал такую смену в тихий
    ``amount=None``, а он на fail-open-сверке означал бы зачисление счёта без
    проверки суммы (аудит 2026-07-25). ``bool`` отсекаем явно — он подкласс int.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").strip())
        except ValueError:
            return None
    return None


def _sale_amount(item: dict) -> tuple[float | None, str | None]:
    """Достать (сумма, валюта) продажи из строки ``GET /api/v2/invoices``.

    Форма ответа платформой не зафиксирована и отличается от ответа create
    (``amountTotal``). Пробуем известные варианты по очереди:
    ``receipt.amount`` (фискальный чек — приезжает АСИНХРОННО, у свежей
    продажи его может ещё не быть), ``amountTotal`` (число или объект),
    ``amount`` верхнего уровня. Ни один не подошёл → ``(None, None)``, и
    вызывающий обязан трактовать это как «сверить нечем», а не «сверка ок».
    """
    receipt = item.get("receipt") if isinstance(item.get("receipt"), dict) else {}
    candidates: list[tuple[object, object]] = [
        (receipt.get("amount"), receipt.get("currency")),
    ]
    total = item.get("amountTotal")
    if isinstance(total, dict):
        candidates.append((total.get("amount"), total.get("currency")))
    else:
        candidates.append((total, item.get("currency")))
    candidates.append((item.get("amount"), item.get("currency")))

    for raw_amount, raw_currency in candidates:
        amount = _coerce_amount(raw_amount)
        if amount is not None:
            currency = raw_currency if isinstance(raw_currency, str) else None
            return amount, currency
    return None, None


# Одна интеграция lava — два имени провайдера в нашей системе. Имя живёт в
# ``Payment.provider`` и в меню выбора способа оплаты:
#   * ``lava_top``     — карта РФ (эквайрер SMART_GLOCAL, paymentMethod CARD);
#   * ``lava_top_sbp`` — СБП (агрегатор PAY2ME, paymentMethod SBP).
# До 2026-09-19 была одна кнопка «Карта РФ / СБП» через PAY2ME без
# paymentMethod — агрегатор сам давал выбрать на своей странице. Потом lava
# закрыл карту у PAY2ME, а без метода PAY2ME берёт карту по умолчанию →
# каждый счёт падал с 400 «Restricted payment method type». Вебхук и сверка
# по API-ключу общие для обоих имён (LAVA_FAMILY).
LAVA_FAMILY = ("lava_top", "lava_top_sbp")


class LavaTopProvider:
    name = "lava_top"
    family = LAVA_FAMILY

    def __init__(
        self,
        *,
        api_key: str,
        offer_id: str,
        webhook_secret: str,
        email_domain: str,
        api_base: str | None = None,
        payment_provider: str | None = None,
        payment_method: str | None = None,
        name: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._offer_id = offer_id
        self._webhook_secret = webhook_secret
        self._email_domain = email_domain
        self._api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        # paymentProvider у lava выбирает эквайрера (SMART_GLOCAL — карта,
        # PAY2ME — СБП), paymentMethod — способ (CARD / SBP). Пусто → не шлём
        # (дефолт lava: SMART_GLOCAL + карта).
        self._payment_provider = (payment_provider or "").strip().upper() or None
        self._payment_method = (payment_method or "").strip().upper() or None
        if name:
            self.name = name
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
            # utm_content — round-trip нашего invoice_id (вебхук/сверка матчат
            # по нему). utm_source — честный канал продажи: платформа ждёт в
            # нём источник трафика (анкета службы безопасности, 2026-09-21);
            # return_url приходит только со страницы по саб-токену.
            "clientUtm": {
                "utm_content": str(invoice_id),
                "utm_source": "sub_page" if return_url else "telegram_bot",
            },
        }
        if self._payment_provider:
            body["paymentProvider"] = self._payment_provider
        if self._payment_method:
            # Способ шлём ЯВНО: у PAY2ME без paymentMethod дефолт — карта, а
            # она у него закрыта (2026-09-19, «Restricted payment method type»).
            body["paymentMethod"] = self._payment_method
        resp, data = self._post_invoice(body)
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

    # Секундные провалы балансировщика lava («no available server» текстом
    # вместо JSON, 5xx, обрыв соединения) превращались в «Не удалось создать
    # счёт» у человека — хотя следующий тап через пару секунд проходил
    # (2026-09-19: три тапа подряд — 502, 502, 200). Один повтор с паузой
    # закрывает ровно этот класс; 4xx не повторяем — это не транзиент.
    _CREATE_ATTEMPTS = 2
    _RETRY_DELAY_SEC = 1.5

    def _post_invoice(self, body: dict) -> tuple["requests.Response", dict]:
        """POST /api/v3/invoice с одним повтором на транзиентный сбой."""
        last_error: ProviderError | None = None
        for attempt in range(1, self._CREATE_ATTEMPTS + 1):
            try:
                resp = self._session.post(
                    f"{self._api_base}/api/v3/invoice",
                    json=body,
                    headers={"X-Api-Key": self._api_key},
                    timeout=15,
                )
            except requests.RequestException as exc:
                last_error = ProviderError(f"lava_top: create failed: {exc}")
            else:
                try:
                    data = resp.json()
                except ValueError:
                    last_error = ProviderError(
                        f"lava_top: non-JSON response: {resp.text[:200]}"
                    )
                else:
                    if resp.status_code < 500:
                        return resp, data
                    last_error = ProviderError(
                        f"lava_top: HTTP {resp.status_code}: {data}"
                    )
            if attempt < self._CREATE_ATTEMPTS:
                logger.warning(
                    "lava_top: create attempt %s failed (%s) — retrying",
                    attempt, last_error,
                )
                time.sleep(self._RETRY_DELAY_SEC)
        assert last_error is not None
        raise last_error

    # ---------- reconcile (webhook-independent) ----------

    def list_recent_invoices(self) -> list[dict]:
        """GET /api/v2/invoices — недавние продажи для авто-сверки.

        Вебхуки lava негарантированы (до 20 ретраев по докам; в проде
        наблюдалось, что POST не приходит вовсе). Этот метод отдаёт список
        продаж, чтобы воркер мог зачислить pending-счета, чей платёж у lava
        уже COMPLETED. Нормализует к списку dict'ов:
        ``{invoice_id: int|None, amount: float|None, currency, contract_id, completed: bool}``.
        ``invoice_id`` берётся из ``clientUtm.utm_content`` (наш round-trip).
        """
        try:
            resp = self._session.get(
                f"{self._api_base}/api/v2/invoices",
                headers={"X-Api-Key": self._api_key},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"lava_top: list invoices failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError(f"lava_top: non-JSON invoices response: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"lava_top: invoices HTTP {resp.status_code}: {data}")

        out: list[dict] = []
        for item in (data.get("items") or []):
            if not isinstance(item, dict):
                continue
            utm = item.get("clientUtm")
            raw_id = utm.get("utm_content") if isinstance(utm, dict) else None
            invoice_id = int(raw_id) if raw_id and str(raw_id).isdigit() else None
            amount, currency = _sale_amount(item)
            if amount is None:
                # Сумму не нашли ни в одной известной форме — НЕ прячем это:
                # воркер обязан отказаться зачислять (fail-closed), иначе смена
                # формы ответа lava молча снимала бы единственную проверку суммы
                # на основном денежном пути (аудит 2026-07-25).
                logger.warning(
                    "lava_top: не удалось распарсить сумму продажи %s (invoice_id=%s); "
                    "поля: %s", item.get("id"), invoice_id, sorted(item),
                )
            out.append(
                {
                    "invoice_id": invoice_id,
                    "amount": amount,
                    "currency": currency,
                    "contract_id": item.get("id"),
                    "offer_id": (item.get("offer") or {}).get("id")
                    if isinstance(item.get("offer"), dict)
                    else item.get("offerId"),
                    "completed": str(item.get("status") or "").upper() == "COMPLETED",
                }
            )
        return out

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


def load_lava_top_env(kind: str = "card") -> dict:
    """Read LAVA_TOP_* env vars and return constructor kwargs.

    ``kind`` — какое из двух имён поднимаем: ``card`` (``lava_top``:
    эквайрер ``LAVA_TOP_CARD_PROVIDER``, дефолт SMART_GLOCAL, метод CARD) или
    ``sbp`` (``lava_top_sbp``: ``LAVA_TOP_SBP_PROVIDER``, дефолт PAY2ME, метод
    SBP). Ключ, offerId, секрет вебхука и домен общие.

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
    if kind == "sbp":
        method = {
            "payment_provider": os.getenv("LAVA_TOP_SBP_PROVIDER", "PAY2ME"),
            "payment_method": "SBP",
            "name": "lava_top_sbp",
        }
    else:
        # Пустой LAVA_TOP_CARD_PROVIDER = дефолт lava (SMART_GLOCAL). Старый
        # LAVA_TOP_PAYMENT_PROVIDER (=PAY2ME в проде) сюда намеренно НЕ
        # читаем — с ним карта и сломалась.
        method = {
            "payment_provider": os.getenv("LAVA_TOP_CARD_PROVIDER", "SMART_GLOCAL"),
            "payment_method": "CARD",
            "name": "lava_top",
        }
    return {
        **values,
        "api_base": os.getenv("LAVA_TOP_API_BASE"),
        **method,
    }
