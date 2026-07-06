"""Payment endpoints: ``/api/payments``, ``/api/payments/webhook/{provider}`` and ``/api/invoices/{id}/checkout``.

The checkout route creates a provider-side invoice and returns the
pay_url. The webhook is HMAC-verified inside the provider driver (not
via the admin token, so FastAPI treats it as public — a bad signature
returns 401 from inside ``provider.verify_webhook``).
"""
from __future__ import annotations

import asyncio
import math
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import optional_admin as optional_admin_token
from ..auth import require_admin
from ..rate_limit import limiter
from ..services.payments import ProviderError, get_provider
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db, logger
from .invoices import _mark_invoice_paid_core

router = APIRouter()


def _convert_for_provider(amount: float, currency: str, provider_name: str) -> tuple[float, str]:
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
        # Ленивый импорт: app.api_webapp сам импортирует app.api, поэтому
        # импорт на уровне модуля дал бы цикл. Курс — единый
        # WEBAPP_STARS_PER_RUB, тот же, что в WebApp-чекауте.
        from ..api_webapp import _rub_to_stars

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


@router.post("/payments")
def create_payment(
    body: schemas.PaymentCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    sub = db.get(models.Subscription, body.subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    payment = models.Payment(
        subscription_id=sub.id,
        amount=body.amount,
        currency=body.currency,
        status=models.PaymentStatus(body.status),
        provider=body.provider,
        external_id=body.external_id,
    )
    db.add(payment)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Payment with provider={body.provider} external_id={body.external_id} already exists",
        )
    db.refresh(payment)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "payment_created", "payment", payment.id, actor_type=actor_type)
    return {"id": payment.id, "status": payment.status.value}


@router.post("/invoices/{invoice_id}/checkout", response_model=schemas.InvoiceCheckoutOut)
def checkout_invoice(
    invoice_id: int,
    body: schemas.InvoiceCheckoutRequest | None = None,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin_token),
):
    """Create a payment-provider invoice and return the pay URL.

    The bot calls this after the user picks a plan; we persist a
    ``Payment(status=pending, provider=..., external_id=...)`` so that the
    webhook handler can correlate the callback back to our invoice.
    """
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status != models.InvoiceStatus.pending:
        raise HTTPException(status_code=400, detail="Invoice is not in pending state")

    provider_name = (body.provider if body else None) or None
    try:
        provider = get_provider(provider_name)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # #108: счёт хранится в RUB — провайдеру отправляем сумму в его
    # валюте (звёзды/USDT), иначе рубли трактуются 1:1.
    try:
        pay_amount, pay_currency = _convert_for_provider(
            float(invoice.amount), invoice.currency, provider.name
        )
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        provider_invoice = provider.create_invoice(
            invoice_id=invoice.id,
            amount=pay_amount,
            currency=pay_currency,
            description=f"Order #{invoice.id}",
            return_url=(body.return_url if body else None),
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"payment provider error: {exc}") from exc

    # Persist a Payment row — this is what the webhook will look up.
    # subscription_id may be NULL for new_subscription invoices; the link
    # back to the checkout is via invoice_id.
    payment = models.Payment(
        subscription_id=invoice.subscription_id,
        invoice_id=invoice.id,
        amount=invoice.amount,
        currency=invoice.currency,
        status=models.PaymentStatus.pending,
        provider=provider.name,
        external_id=provider_invoice.external_id,
    )
    db.add(payment)
    try:
        db.commit()
    except IntegrityError:
        # #52 — UNIQUE(provider, external_id) fired: the same provider
        # invoice was already recorded (double-click, retry). Roll back
        # and return the existing pay_url — idempotent from the caller's
        # perspective.
        db.rollback()
        logger.info(
            "Duplicate checkout for invoice %d provider %s external_id %s",
            invoice.id,
            provider.name,
            provider_invoice.external_id,
        )

    return schemas.InvoiceCheckoutOut(
        invoice_id=invoice.id,
        provider=provider.name,
        external_id=provider_invoice.external_id,
        pay_url=provider_invoice.pay_url,
        amount=provider_invoice.amount,
        currency=provider_invoice.currency,
    )


@router.post("/payments/webhook/{provider_name}")
@limiter.limit("30/minute")
async def payment_webhook(
    provider_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Handle asynchronous callbacks from payment providers.

    Auth is HMAC-signature based (verified inside the provider driver), not
    via the admin token — so this endpoint is intentionally unauthenticated
    from FastAPI's point of view. A missing or invalid signature returns
    401. A successful ``paid`` event runs the same invoice→subscription
    pipeline as the admin ``mark_paid`` route.
    """
    try:
        provider = get_provider(provider_name)
    except ProviderError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        event = provider.verify_webhook(body, headers)
    except ProviderError as exc:
        logger.warning("webhook verification failed for %s: %s", provider_name, exc)
        raise HTTPException(status_code=401, detail="invalid webhook") from exc

    if event.status != "paid":
        # Record the event but don't flip the invoice — expired / other
        # statuses are not actionable for us beyond logging.
        logger.info("webhook event %s for invoice %s — no action", event.status, event.external_id)
        return {"ok": True, "status": event.status}

    try:
        invoice_id = int(event.external_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="webhook payload is not an invoice id") from exc

    # #199: вся работа с БД здесь синхронная (with_for_update на инвойс,
    # choose_node, warm-pool), а эндпоинт — async: блокирующие вызовы
    # вставали бы прямо в event loop и морозили ВСЕ запросы процесса
    # (включая /api/sub/{token}), пока ждётся row-lock. Уводим их в
    # threadpool через asyncio.to_thread.
    def _process_paid_event() -> dict:
        # Lock the invoice row and mark a Payment as paid if we have one.
        pending_payment = (
            db.query(models.Payment)
            .filter(
                models.Payment.invoice_id == invoice_id,
                models.Payment.provider == provider.name,
            )
            .order_by(models.Payment.id.desc())
            .first()
        )
        payment_id = pending_payment.id if pending_payment else None

        result = _mark_invoice_paid_core(
            db,
            invoice_id,
            actor=f"{provider.name}:webhook",
            actor_type=models.AuditActor.system,
            payment_id=payment_id,
        )
        return {"ok": True, "invoice_id": result.id, "status": result.status}

    return await asyncio.to_thread(_process_paid_event)
