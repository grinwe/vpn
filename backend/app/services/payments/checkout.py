"""Единый чекаут счёта у платёжного провайдера.

До 2026-07-29 цепочка «create_invoice у провайдера → Payment(pending) →
pay_url» жила в ТРЁХ копиях (api/payments.py checkout_invoice,
api_webapp.py webapp_checkout и webapp_topup), причём конвертация валют
(#108) и IntegrityError-дедуп (#52) были только в первой. Страница починки
без Telegram стала бы четвёртой копией — вместо этого все ходят сюда.

Идемпотентность двухуровневая:
1. pending-Payment этого счёта у этого провайдера с сохранённым ``pay_url``
   → возвращаем его, к провайдеру НЕ ходим (повторный тап = тот же URL,
   в кабинете провайдера не плодятся счета-сироты);
2. гонка двойного тапа, проскочившая мимо (1), ловится
   UNIQUE(provider, external_id) → откат и реюз существующей строки.

Кидает ``ProviderError`` — HTTP-маппинг делает вызывающий слой.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ... import models
from . import ProviderError, get_provider

logger = logging.getLogger("app.services.payments.checkout")


# Что видит человек, когда провайдер не смог выписать счёт. Сырой текст
# провайдера («HTTP 400: Restricted payment method type», «non-JSON response:
# no available server») раньше уходил в диалог кабинета как есть — юзеру он
# бесполезен, а админ его не видел вовсе: карта лежала три недели, пока
# владелец не прислал скриншот (2026-09-19). Теперь наоборот: человеку —
# понятная фраза, админу — пуш с сырой ошибкой.
PROVIDER_UNAVAILABLE_MESSAGE = (
    "Платёжный сервис временно недоступен. Попробуй через минуту или "
    "другой способ оплаты."
)


def report_provider_failure(
    db: Session,
    exc: Exception,
    *,
    provider_name: str | None,
    invoice_id: int | None,
) -> str:
    """Залогировать и запушить админам сбой создания счёта; вернуть текст
    для пользователя.

    Дедуп пуша — час на провайдера: один секундный провал балансировщика =
    один пуш, а не по пушу на каждый тап всех пользователей.
    """
    from ..admin_notify import notify_admins

    name = provider_name or "?"
    logger.warning(
        "checkout: provider %s failed for invoice %s: %s", name, invoice_id, exc
    )
    try:
        notify_admins(
            db,
            kind="payment_provider",
            text=(
                f"⚠️ Платёжный провайдер {name} не смог выписать счёт"
                f"{f' #{invoice_id}' if invoice_id else ''}: {str(exc)[:300]}"
            ),
            dedup_key={"provider": name},
            extra={"provider": name, "invoice_id": invoice_id, "error": str(exc)[:500]},
            window_sec=3600,
            autocommit=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("checkout: admin alert for provider %s failed", name)
    return PROVIDER_UNAVAILABLE_MESSAGE


class ProviderApiError(ProviderError):
    """Ошибка ПОХОДА к провайдеру (сеть/HTTP-ответ).

    Отличается от конфигурационных ProviderError (нет провайдера, нет курса
    конвертации): вызывающие мапят конфиг в 503, поход — в 502, и это
    закреплено аудит-тестами.
    """


def normalize_currency(currency: str | None) -> str:
    """RUR→RUB и верхний регистр — косметика кодов не должна ломать сверку."""
    cur = (currency or "").upper()
    return "RUB" if cur == "RUR" else cur


def convert_for_provider(
    amount: float, currency: str, provider_name: str
) -> tuple[float, str]:
    """Привести сумму счёта к валюте платёжного провайдера (аудит #108).

    Бот создаёт счета в рублях, но провайдеры ждут свою валюту:
    telegram_stars — звёзды (XTR), cryptobot — криптоактив (USDT).
    Без конвертации рубли уходили бы 1:1 как звёзды (переплата ~в 1.5
    раза) или как несуществующий ассет ``RUB`` в Crypto Pay. SBP и уже
    сконвертированные счета (XTR/USDT) проходят без изменений.
    """
    cur = (currency or "").upper()
    if cur not in ("RUB", "RUR"):
        return amount, currency

    if provider_name in ("telegram_stars", "stars"):
        # Ленивый импорт: api_webapp тянет api-слой, поэтому импорт на
        # уровне модуля дал бы цикл. Курс — единый WEBAPP_STARS_PER_RUB.
        from ...api_webapp import _rub_to_stars

        return float(_rub_to_stars(amount)), "XTR"

    if provider_name == "cryptobot":
        # Курс задаётся оператором; без него создавать USDT-счёт на
        # рублёвую сумму нельзя — это прямая ошибка в деньгах.
        try:
            rate = float(os.getenv("CRYPTOBOT_RUB_PER_USDT", "0"))
        except (TypeError, ValueError):
            rate = 0.0
        if rate <= 0:
            raise ProviderError(
                "cryptobot: счёт в RUB требует курс CRYPTOBOT_RUB_PER_USDT "
                "(рублей за 1 USDT) — переменная не задана"
            )
        # Округляем вверх до цента, чтобы не недополучить на дробях.
        usdt = math.ceil(amount / rate * 100) / 100
        return max(0.01, usdt), "USDT"

    # generic_sbp и прочие рублёвые провайдеры — без конвертации.
    return amount, currency


@dataclass(frozen=True)
class CheckoutResult:
    provider: str
    external_id: str | None
    pay_url: str | None
    amount: float
    currency: str
    reused: bool = False


def checkout_pending_invoice(
    db: Session,
    invoice: models.Invoice,
    *,
    provider_name: str | None = None,
    return_url: str | None = None,
    pay_amount: float | None = None,
    pay_currency: str | None = None,
) -> CheckoutResult:
    """Выписать (или реюзнуть) счёт у провайдера для pending-инвойса.

    Владельца и статус инвойса проверяет ВЫЗЫВАЮЩИЙ — здесь только деньги
    и идемпотентность. Не коммитит ничего, кроме Payment-строки.

    ``pay_amount``/``pay_currency`` — явный override суммы для похода к
    провайдеру (topup: Invoice.amount по контракту в рублях, чтобы хук
    начислил чистые копейки, а провайдеру при Stars нужна сумма в XTR —
    из такой строки конвертация не выводится). Без override сумма берётся
    из инвойса и конвертируется по правилам #108.
    """
    provider = get_provider(provider_name)

    reusable = (
        db.query(models.Payment)
        .filter(
            models.Payment.invoice_id == invoice.id,
            models.Payment.provider == provider.name,
            models.Payment.status == models.PaymentStatus.pending,
            models.Payment.pay_url.isnot(None),
        )
        .order_by(models.Payment.id.desc())
        .first()
    )
    if reusable is not None:
        return CheckoutResult(
            provider=provider.name,
            external_id=reusable.external_id,
            pay_url=reusable.pay_url,
            amount=float(reusable.amount),
            currency=reusable.currency,
            reused=True,
        )

    # #108: счёт хранится в RUB — провайдеру отправляем сумму в его валюте.
    if pay_amount is None or pay_currency is None:
        pay_amount, pay_currency = convert_for_provider(
            float(invoice.amount), invoice.currency, provider.name
        )

    try:
        provider_invoice = provider.create_invoice(
            invoice_id=invoice.id,
            amount=pay_amount,
            currency=pay_currency,
            description=f"Order #{invoice.id}",
            return_url=return_url,
        )
    except ProviderApiError:
        raise
    except ProviderError as exc:
        raise ProviderApiError(str(exc)) from exc

    payment = models.Payment(
        subscription_id=invoice.subscription_id,
        invoice_id=invoice.id,
        amount=invoice.amount,
        currency=invoice.currency,
        status=models.PaymentStatus.pending,
        provider=provider.name,
        external_id=provider_invoice.external_id,
        pay_url=provider_invoice.pay_url,
    )
    db.add(payment)
    try:
        db.commit()
    except IntegrityError:
        # #52 — UNIQUE(provider, external_id): тот же провайдерский счёт уже
        # записан (двойной клик, ретрай). Откат и ответ тем же pay_url —
        # идемпотентно с точки зрения вызывающего.
        db.rollback()
        logger.info(
            "Duplicate checkout for invoice %d provider %s external_id %s",
            invoice.id, provider.name, provider_invoice.external_id,
        )

    return CheckoutResult(
        provider=provider.name,
        external_id=provider_invoice.external_id,
        pay_url=provider_invoice.pay_url,
        amount=provider_invoice.amount,
        currency=provider_invoice.currency,
    )
