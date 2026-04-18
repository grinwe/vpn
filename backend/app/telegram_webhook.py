"""Telegram Bot API native webhook receiver (audit #62).

Replaces the shared-secret bot-to-backend forwarding for Stars payments.
Telegram authenticates updates via ``X-Telegram-Bot-Api-Secret-Token``
(set once in ``setWebhook``), so the endpoint no longer relies on a
shared ``TELEGRAM_STARS_WEBHOOK_SECRET``.

Update routing
--------------
* ``pre_checkout_query`` (XTR) -- answer OK directly via Bot API.
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
        await asyncio.to_thread(
            _answer_pre_checkout_query,
            settings.bot_token,
            query_id,
            ok=True,
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
            logger.warning(
                "mark_invoice_paid failed for invoice %d: %s",
                invoice_id,
                exc.detail,
            )
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
