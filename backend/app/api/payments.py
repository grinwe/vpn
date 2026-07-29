"""Payment endpoints: ``/api/payments``, ``/api/payments/webhook/{provider}`` and ``/api/invoices/{id}/checkout``.

The checkout route creates a provider-side invoice and returns the
pay_url. The webhook is HMAC-verified inside the provider driver (not
via the admin token, so FastAPI treats it as public — a bad signature
returns 401 from inside ``provider.verify_webhook``).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import optional_admin as optional_admin_token
from ..auth import require_admin
from ..rate_limit import limiter
from ..services.payments import ProviderError, get_provider
from ..services.payments.checkout import (
    ProviderApiError,
    checkout_pending_invoice,
    convert_for_provider,
)
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db, logger
from .invoices import _mark_invoice_paid_core

router = APIRouter()


def _provider_invoice_id_from_event(event) -> str | None:
    """Достать id счёта НА СТОРОНЕ провайдера из сырого события (аудит #117).

    Внимание: ``event.external_id`` у всех драйверов — это НАШ внутренний
    invoice id (round-trip через ``payload``), а в ``Payment.external_id``
    лежит id счёта, выданный провайдером. При двойном checkout по одному
    счёту создаётся несколько Payment-строк с разными provider external_id,
    поэтому по ``event.external_id`` нужную строку не отличить. Ищем
    provider invoice id в известных местах ``raw`` (best-effort): если не
    нашли — вызывающий откатывается на «последнюю pending».
    """
    raw = getattr(event, "raw", None)
    if not isinstance(raw, dict):
        return None
    # cryptobot: {"update_type": ..., "payload": {"invoice_id": <id провайдера>,
    #             "payload": "<наш invoice id>"}}
    # tribute:   {"name": "shop_order", "payload": {"uuid": <uuid заказа>, ...}}
    inner = raw.get("payload")
    if isinstance(inner, dict):
        pid = inner.get("invoice_id") or inner.get("uuid")
        if pid:
            return str(pid)
    # lava_top: {"eventType": ..., "contractId": <uuid контракта>, ...}
    pid = raw.get("contractId")
    if pid:
        return str(pid)
    return None


def _norm_currency(currency: str | None) -> str:
    """Нормализовать код валюты для сравнения (аудит #111).

    Приводим к верхнему регистру и схлопываем синоним RUR→RUB, чтобы
    сверка суммы вебхука не падала на косметическом различии кодов.
    """
    cur = (currency or "").upper()
    return "RUB" if cur == "RUR" else cur


# Конвертация переехала в services/payments/checkout.py вместе с единым
# чекаутом (2026-07-29): здесь остаётся алиас — его импортируют
# telegram_webhook и два аудит-теста.
_convert_for_provider = convert_for_provider


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
    # Ownership guard: бот передаёт telegram_id вызывающего, т.к. invoice_id
    # в новом меню оплаты приходит из подделываемой callback_data. Без
    # совпадения владельца — 403 (IDOR: чужой pending-счёт не чекаутится).
    #
    # Guard ОБЯЗАТЕЛЕН для не-админов: эндпоинт анонимный, и пока проверка
    # висела на `if req_tg`, она снималась простым отсутствием поля в теле —
    # то есть защищала только честного клиента (аудит 2026-07-25). Админ
    # (валидный X-Admin-Token) по-прежнему чекаутит любой счёт.
    req_tg = body.telegram_id if body else None
    if admin_token is None:
        if not req_tg:
            raise HTTPException(
                status_code=403,
                detail="telegram_id is required for non-admin checkout",
            )
        if invoice.user is None or invoice.user.telegram_id != req_tg:
            raise HTTPException(
                status_code=403, detail="Invoice does not belong to this user"
            )
    elif req_tg and (invoice.user is None or invoice.user.telegram_id != req_tg):
        raise HTTPException(status_code=403, detail="Invoice does not belong to this user")
    if invoice.status != models.InvoiceStatus.pending:
        raise HTTPException(status_code=400, detail="Invoice is not in pending state")

    # Вся денежная цепочка (конвертация #108, Payment(pending), реюз
    # pay_url + IntegrityError-дедуп #52) — в едином хелпере: копий было
    # три, страница починки без Telegram стала бы четвёртой.
    try:
        result = checkout_pending_invoice(
            db, invoice,
            provider_name=(body.provider if body else None) or None,
            return_url=(body.return_url if body else None),
        )
    except ProviderApiError as exc:
        raise HTTPException(status_code=502, detail=f"payment provider error: {exc}") from exc
    except ProviderError as exc:
        # Конфигурация (нет провайдера / нет курса) — 503, как и раньше.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return schemas.InvoiceCheckoutOut(
        invoice_id=invoice.id,
        provider=result.provider,
        external_id=result.external_id,
        pay_url=result.pay_url,
        amount=result.amount,
        currency=result.currency,
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
        # #111: сверяем сумму и валюту вебхука с ожидаемой суммой счёта,
        # прежде чем зачислять. Провайдер присылает сумму в СВОЕЙ валюте
        # (XTR/USDT), а счёт хранится в рублях, поэтому сравниваем не с
        # invoice.amount напрямую, а с тем же _convert_for_provider,
        # которым сумма считалась при создании счёта в checkout. Иначе
        # недоплата или ошибочно смэтченный платёж молча кредитуют баланс
        # на полную сумму.
        invoice = db.get(models.Invoice, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")

        if event.amount is None:
            # Сумму провайдер не прислал / не распарсили — это «сверить нечем».
            # Зачисляем (подпись валидна, счёт наш), но ГРОМКО: раньше эта ветка
            # была немой, и любой сбой парсинга суммы бесшумно снимал главную
            # проверку денежного пути (аудит 2026-07-25).
            from ..services.admin_notify import notify_admins

            logger.warning(
                "webhook %s invoice %d: сумма отсутствует — зачисляем БЕЗ сверки суммы",
                provider.name, invoice_id,
            )
            notify_admins(
                db,
                kind="payment_amount_unverified",
                text=(
                    f"⚠️ Вебхук {provider.name} по счёту #{invoice_id} пришёл без "
                    f"суммы — счёт зачислен БЕЗ сверки. Проверить вручную."
                ),
                dedup_key={"invoice_id": invoice_id},
                extra={"invoice_id": invoice_id, "provider": provider.name},
                autocommit=True,
            )
        else:
            try:
                expected_amount, expected_currency = _convert_for_provider(
                    float(invoice.amount), invoice.currency, provider.name
                )
            except ProviderError as exc:
                # Курс не задан (обычно он был на этапе checkout) — сверить
                # не можем. Подпись вебхука валидна и счёт существует,
                # поэтому зачисляем, но громко логируем для ручной сверки.
                logger.warning(
                    "webhook %s invoice %d: не удалось вычислить ожидаемую сумму для сверки: %s",
                    provider.name,
                    invoice_id,
                    exc,
                )
                expected_amount = None
                expected_currency = None

            if expected_amount is not None:
                # Допуск на копейки/дробное округление при конвертации.
                eps = 0.01
                underpaid = event.amount + eps < float(expected_amount)
                currency_ok = event.currency is None or _norm_currency(
                    event.currency
                ) == _norm_currency(expected_currency)
                if underpaid or not currency_ok:
                    from ..services.admin_notify import notify_admins

                    logger.warning(
                        "webhook %s invoice %d: сумма/валюта не совпали "
                        "(получено %s %s, ожидалось %s %s) — счёт НЕ зачислен",
                        provider.name,
                        invoice_id,
                        event.amount,
                        event.currency,
                        expected_amount,
                        expected_currency,
                    )
                    notify_admins(
                        db,
                        kind="payment_amount_mismatch",
                        text=(
                            f"⚠️ Вебхук {provider.name} по счёту #{invoice_id}: "
                            f"сумма/валюта не совпали. Получено "
                            f"{event.amount} {event.currency or '?'}, ожидалось "
                            f"{expected_amount} {expected_currency}. Счёт оставлен pending."
                        ),
                        dedup_key={"invoice_id": invoice_id},
                        extra={
                            "invoice_id": invoice_id,
                            "provider": provider.name,
                            "got_amount": event.amount,
                            "got_currency": event.currency,
                            "expected_amount": float(expected_amount),
                            "expected_currency": expected_currency,
                        },
                        autocommit=True,
                    )
                    raise HTTPException(
                        status_code=409, detail="webhook amount/currency mismatch"
                    )
                # Переплата не блокирует зачисление (клиент заплатил не
                # меньше), но фиксируем расхождение в логах для сверки.
                if event.amount - eps > float(expected_amount):
                    logger.warning(
                        "webhook %s invoice %d: переплата — получено %s, "
                        "ожидалось %s; зачисляем",
                        provider.name,
                        invoice_id,
                        event.amount,
                        expected_amount,
                    )

        # Lock the invoice row and mark a Payment as paid if we have one.
        # #117: при двойном checkout по одному счёту существует несколько
        # Payment-строк (каждая со своим provider external_id). Раньше брали
        # просто последнюю по id — и paid мог получить НЕ та строка, которую
        # реально оплатили, из-за чего сверка с провайдером по external_id
        # расходилась. Теперь выбираем аккуратно:
        #   1) среди pending-строк — ту, чей external_id совпал с provider
        #      invoice id из события (если его удаётся извлечь из raw);
        #   2) иначе — последнюю pending;
        #   3) иначе (ретрай уже обработанного вебхука, pending-строк нет) —
        #      последнюю любую, сохраняя прежнее поведение.
        base_q = db.query(models.Payment).filter(
            models.Payment.invoice_id == invoice_id,
            models.Payment.provider == provider.name,
        )
        pending_payments = (
            base_q.filter(models.Payment.status == models.PaymentStatus.pending)
            .order_by(models.Payment.id.desc())
            .all()
        )
        prov_ext_id = _provider_invoice_id_from_event(event)
        pending_payment = None
        if prov_ext_id:
            pending_payment = next(
                (p for p in pending_payments if p.external_id == prov_ext_id), None
            )
        if pending_payment is None:
            pending_payment = pending_payments[0] if pending_payments else None
        if pending_payment is None:
            pending_payment = base_q.order_by(models.Payment.id.desc()).first()
        payment_id = pending_payment.id if pending_payment else None

        # Детект двойной оплаты: счёт уже paid, но пришёл НОВЫЙ платёж
        # (свежая pending-строка — обычно другой способ из меню Stage 9b,
        # оплаченный вторым). _mark_invoice_paid_core молча зачтёт его без
        # повторного провижининга — деньги списаны дважды за один счёт,
        # поэтому зовём оператора на возврат. Ретрай того же вебхука сюда не
        # попадает: он матчит уже-paid строку (не pending) → case 3.
        if (
            invoice.status == models.InvoiceStatus.paid
            and pending_payment is not None
            and pending_payment.status == models.PaymentStatus.pending
        ):
            from ..services.admin_notify import notify_admins

            logger.warning(
                "webhook %s invoice %d: платёж по уже оплаченному счёту "
                "(payment #%s) — вероятна двойная оплата, нужен возврат",
                provider.name,
                invoice_id,
                payment_id,
            )
            notify_admins(
                db,
                kind="payment_double_paid",
                text=(
                    f"⚠️ Двойная оплата счёта #{invoice_id}: пришёл платёж "
                    f"{provider.name} по уже оплаченному счёту. Проверьте и "
                    f"верните лишнее."
                ),
                dedup_key={"invoice_id": invoice_id, "payment_id": payment_id},
                extra={
                    "invoice_id": invoice_id,
                    "provider": provider.name,
                    "payment_id": payment_id,
                },
                autocommit=True,
            )

        result = _mark_invoice_paid_core(
            db,
            invoice_id,
            actor=f"{provider.name}:webhook",
            actor_type=models.AuditActor.system,
            payment_id=payment_id,
        )
        return {"ok": True, "invoice_id": result.id, "status": result.status}

    return await asyncio.to_thread(_process_paid_event)
