"""Telegram Bot API native webhook receiver (audit #62).

Replaces the shared-secret bot-to-backend forwarding for Stars payments.
Telegram authenticates updates via ``X-Telegram-Bot-Api-Secret-Token``
(set once in ``setWebhook``), so the endpoint no longer relies on a
shared ``TELEGRAM_STARS_WEBHOOK_SECRET``.

Update routing
--------------
* ``pre_checkout_query`` (XTR) -- validate the invoice (exists, still
  pending, Stars amount matches) and answer via Bot API (audit #2).
  Must respond within 10 s or Telegram cancels the payment.
* ``message.successful_payment`` (XTR) -- mark the invoice paid in the
  DB (same pipeline as the admin ``mark_paid`` route).
* Everything else -- forward the raw JSON to the bot's internal webhook
  so aiogram dispatches it normally.

Activation
----------
Set these env vars to enable webhook mode:

* ``TELEGRAM_WEBHOOK_SECRET_TOKEN`` -- random secret, stored in vault.
* ``TELEGRAM_WEBHOOK_URL``          -- public URL of this endpoint
  (e.g. ``https://grinwer.online/tg-webhook``).
* ``BOT_INTERNAL_WEBHOOK_URL``      -- internal URL where the bot
  listens for forwarded updates (e.g. ``http://bot:8081/webhook``).

The endpoint lives at ``/tg-webhook`` (outside ``/api``) to stay out
of the admin-token middleware scope.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging

import requests as _requests
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import models
from .api._common import get_db
from .api.invoices import _mark_invoice_paid_core
from .config import get_settings
from .rate_limit import limiter
from .services.admin_notify import notify_admins

logger = logging.getLogger(__name__)
router = APIRouter()

BOT_API_BASE = "https://api.telegram.org"


# ── Bot API helpers (synchronous — run via asyncio.to_thread) ────────


def _answer_pre_checkout_query(
    bot_token: str,
    query_id: str,
    *,
    ok: bool,
    error_message: str | None = None,
) -> None:
    body: dict = {"pre_checkout_query_id": query_id, "ok": ok}
    if error_message:
        body["error_message"] = error_message
    try:
        resp = _requests.post(
            f"{BOT_API_BASE}/bot{bot_token}/answerPreCheckoutQuery",
            json=body,
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                "answerPreCheckoutQuery failed: %s %s",
                resp.status_code,
                resp.text[:200],
            )
    except _requests.RequestException:
        logger.exception("answerPreCheckoutQuery network error")


def _forward_to_bot(body: bytes, bot_url: str) -> None:
    try:
        resp = _requests.post(
            bot_url,
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Bot forward returned %s", resp.status_code)
    except _requests.RequestException:
        logger.exception("Failed to forward update to bot at %s", bot_url)


# ── pre_checkout validation (аудит #2) ───────────────────────────────


def _expected_stars_amount(invoice: models.Invoice) -> int | None:
    """Ожидаемая сумма счёта в Stars — зеркалит путь его создания.

    * ``kind=topup``: ``Invoice.amount`` всегда хранится в рублях
      (``api_webapp.webapp_topup``), даже при ``currency="XTR"`` —
      конвертируем так же, как при выпуске ссылки (``_rub_to_stars``).
    * RUB-инвойсы (бот-флоу): конвертация повторяет
      ``api/payments._convert_for_provider`` → ``_rub_to_stars``.
    * XTR-инвойсы (webapp checkout): ``amount`` уже в звёздах, провайдер
      лишь округляет вверх (``TelegramStarsProvider.create_invoice``).

    Если курс ``WEBAPP_STARS_PER_RUB`` сменился после выпуска ссылки,
    старая ссылка отклонится на pre_checkout — это осознанно: цена в
    ней устарела, пользователь пересоздаёт заказ по актуальной.

    Возвращает ``None``, если сумму посчитать не удалось (битые данные)
    — тогда проверка суммы пропускается, остальные проверки остаются.
    """
    try:
        amount = float(invoice.amount or 0)
    except (TypeError, ValueError):
        return None
    currency = (invoice.currency or "").upper()
    if invoice.kind == "topup" or currency in ("RUB", "RUR"):
        # Ленивый импорт: api_webapp тянет за собой app.api — держим
        # его вне module-level, как в api/payments._convert_for_provider.
        from .api_webapp import _rub_to_stars

        return _rub_to_stars(amount)
    return max(1, int(amount + 0.999))


def _validate_pre_checkout(db: Session, pcq: dict) -> tuple[bool, str | None]:
    """Проверить инвойс перед подтверждением pre_checkout (аудит #2).

    Telegram списывает Stars сразу после ``ok=True``, поэтому
    подтверждаем только существующий pending-инвойс с совпадающей
    суммой. Иначе отвечаем ``ok=False`` — checkout отменяется и деньги
    пользователя не списываются «в никуда» (раньше failed/битый инвойс
    проходил pre_checkout, а successful_payment падал уже после оплаты).
    """
    try:
        invoice_id = int(pcq.get("invoice_payload"))
    except (TypeError, ValueError):
        logger.warning(
            "pre_checkout: non-int invoice_payload %r",
            pcq.get("invoice_payload"),
        )
        return False, "Счёт не найден — создайте заказ заново"

    invoice = db.get(models.Invoice, invoice_id)
    if invoice is None:
        logger.warning("pre_checkout: invoice %d not found", invoice_id)
        return False, "Счёт не найден — создайте заказ заново"
    if invoice.status == models.InvoiceStatus.paid:
        return False, "Счёт уже оплачен"
    if invoice.status != models.InvoiceStatus.pending:
        return False, "Счёт отменён — создайте заказ заново"

    expected = _expected_stars_amount(invoice)
    total = pcq.get("total_amount")
    if expected is not None and total != expected:
        logger.warning(
            "pre_checkout: amount mismatch for invoice %d: got %r, expected %d",
            invoice_id,
            total,
            expected,
        )
        return False, "Сумма счёта устарела — создайте заказ заново"
    return True, None


# ── Webhook endpoint ─────────────────────────────────────────────────


@router.post("/tg-webhook")
@limiter.limit("300/minute")
async def tg_webhook(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()

    # ── Auth: validate Telegram's secret_token header ──
    secret = settings.telegram_webhook_secret_token
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook not configured")

    provided = request.headers.get("x-telegram-bot-api-secret-token")
    if not provided or not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    try:
        update = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # ── pre_checkout_query -> answer directly via Bot API ──
    pcq = update.get("pre_checkout_query")
    if pcq:
        query_id = pcq.get("id")
        if not query_id:
            raise HTTPException(
                status_code=400, detail="Missing pre_checkout_query.id"
            )
        currency = pcq.get("currency")
        if currency != "XTR":
            await asyncio.to_thread(
                _answer_pre_checkout_query,
                settings.bot_token,
                query_id,
                ok=False,
                error_message="Unsupported currency",
            )
            return {"ok": True}
        # Аудит #2: подтверждаем только существующий pending-инвойс с
        # совпадающей суммой — иначе Telegram спишет Stars, а
        # successful_payment упрётся в 400 и деньги уйдут «в никуда».
        ok, error_message = _validate_pre_checkout(db, pcq)
        await asyncio.to_thread(
            _answer_pre_checkout_query,
            settings.bot_token,
            query_id,
            ok=ok,
            error_message=error_message,
        )
        return {"ok": True}

    # ── successful_payment -> mark invoice paid in DB ──
    msg = update.get("message") or {}
    sp = msg.get("successful_payment")
    if sp and sp.get("currency") == "XTR":
        invoice_payload = sp.get("invoice_payload")
        if not invoice_payload:
            logger.warning("successful_payment without invoice_payload")
            return {"ok": True}
        try:
            invoice_id = int(invoice_payload)
        except (ValueError, TypeError):
            logger.warning(
                "successful_payment non-int invoice_payload: %r",
                invoice_payload,
            )
            return {"ok": True}

        pending_payment = (
            db.query(models.Payment)
            .filter(
                models.Payment.invoice_id == invoice_id,
                models.Payment.provider == "telegram_stars",
            )
            .order_by(models.Payment.id.desc())
            .first()
        )
        payment_id = pending_payment.id if pending_payment else None

        try:
            _mark_invoice_paid_core(
                db,
                invoice_id,
                actor="telegram_stars:native_webhook",
                actor_type=models.AuditActor.system,
                payment_id=payment_id,
            )
        except HTTPException as exc:
            # Аудит #209: Stars уже списаны, а инвойс не оплачен — такое
            # нельзя глотать warning'ом: подписка не создана, повторного
            # апдейта от Telegram не будет. Логируем error, алертим
            # админов (dedup по invoice_id) и на инфраструктурных сбоях
            # (5xx, например упавший провижининг) отвечаем не-200, чтобы
            # Telegram ретраил вебхук.
            logger.error(
                "mark_invoice_paid failed for invoice %d: %s",
                invoice_id,
                exc.detail,
                exc_info=True,
            )
            # Сессия после сбоя может держать полусделанные изменения
            # (mark_paid коммитит поэтапно) — откатываем, чтобы алерт
            # не закоммитил их заодно.
            db.rollback()
            tg_user_id = (msg.get("from") or {}).get("id")
            try:
                notify_admins(
                    db,
                    kind="stars_payment_failed",
                    text=(
                        "⚠️ Оплата Telegram Stars не зачислена: "
                        f"инвойс #{invoice_id}, "
                        f"telegram_id={tg_user_id}, "
                        f"сумма={sp.get('total_amount')}⭐.\n"
                        f"Ошибка: {exc.detail}\n"
                        "Звёзды с пользователя списаны — нужен ручной "
                        "разбор (mark_paid или рефанд)."
                    ),
                    dedup_key={"invoice_id": invoice_id},
                    extra={
                        "telegram_id_user": tg_user_id,
                        "status_code": exc.status_code,
                    },
                    autocommit=True,
                )
            except Exception:  # noqa: BLE001 — алерт не должен ронять ветку
                logger.exception(
                    "notify_admins(stars_payment_failed) failed for invoice %d",
                    invoice_id,
                )
            if exc.status_code >= 500:
                # Восстановимый сбой — не-200 заставит Telegram
                # повторить апдейт; mark_paid идемпотентен для уже
                # оплаченных инвойсов, дубль безопасен.
                raise HTTPException(
                    status_code=500,
                    detail="Stars payment processing failed",
                ) from exc
        return {"ok": True}

    # ── Everything else -> forward to bot ──
    bot_url = settings.bot_internal_webhook_url
    if bot_url:
        await asyncio.to_thread(_forward_to_bot, body, bot_url)
    else:
        logger.debug(
            "No BOT_INTERNAL_WEBHOOK_URL — dropping non-payment update"
        )

    return {"ok": True}


# ── Startup: register webhook with Telegram ──────────────────────────


def register_webhook() -> None:
    """Call ``setWebhook`` on Telegram Bot API.

    Meant to be called once at backend startup (``@app.on_event``).
    Skips silently if the env vars are not configured (local dev / tests).
    """
    settings = get_settings()
    url = settings.telegram_webhook_url
    secret = settings.telegram_webhook_secret_token
    bot_token = settings.bot_token
    if not url or not secret or not bot_token:
        logger.info(
            "Telegram webhook env vars not set — skipping setWebhook"
        )
        return
    try:
        resp = _requests.post(
            f"{BOT_API_BASE}/bot{bot_token}/setWebhook",
            json={"url": url, "secret_token": secret},
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            logger.info("Telegram webhook registered: %s", url)
        else:
            logger.warning("setWebhook failed: %s", data)
    except Exception:
        logger.exception("Failed to register Telegram webhook")
