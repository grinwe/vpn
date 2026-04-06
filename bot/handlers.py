import asyncio
import logging

import aiohttp
from aiogram import F, Router, types
from aiogram.filters import Command

from .config import (
    ADMIN_API_TOKEN,
    ADMIN_IDS,
    BACKEND_URL,
    PAYMENT_PROVIDER,
    TELEGRAM_STARS_WEBHOOK_SECRET,
)
from .keyboards import start_keyboard

router = Router()
logger = logging.getLogger(__name__)

# A single shared aiohttp session is cheaper than opening one per request and
# lets us keep-alive connections to the backend. The session is created
# lazily on first use from inside the event loop so that importing this
# module does not require a running loop.
_SESSION: aiohttp.ClientSession | None = None
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
_RETRIES = 2


async def get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(timeout=_HTTP_TIMEOUT)
    return _SESSION


async def close_session() -> None:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _format_plan_button(plan: dict) -> str:
    price = plan.get("price", "?")
    duration = plan.get("duration_days", "?")
    return f"{plan['name']} — {price}$ / {duration}d"


async def _fetch_json(method: str, url: str, **kwargs):
    """Call backend with retries and a shared session.

    Returns ``(status, payload)``. Status ``0`` means network error after all
    retries — caller should surface a user-friendly error.
    """
    session = await get_session()
    last_exc: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            async with session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                try:
                    payload = await resp.json()
                except Exception:  # noqa: BLE001
                    payload = {"message": text}
                # Retry on 5xx; 4xx is a client error, don't retry.
                if resp.status >= 500 and attempt < _RETRIES:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                return resp.status, payload
        except aiohttp.ClientError as exc:
            last_exc = exc
            if attempt < _RETRIES:
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
    logger.warning("backend request failed: %s %s: %s", method, url, last_exc)
    return 0, {"message": f"backend unreachable: {last_exc}"}


def _admin_headers(actor_id: int) -> dict[str, str]:
    headers: dict[str, str] = {}
    if ADMIN_API_TOKEN:
        headers["X-Admin-Token"] = ADMIN_API_TOKEN
    headers["X-Admin-Actor"] = str(actor_id)
    return headers


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Это VPN-сервис с ShadowTLS+SS и VLESS Reality. "
        "Нажми 'Купить VPN' или вызови /plans, чтобы выбрать тариф.",
        reply_markup=start_keyboard(),
    )


@router.message(F.text == "Купить VPN")
@router.message(Command("plans"))
async def list_plans(message: types.Message):
    try:
        status, data = await _fetch_json("GET", f"{BACKEND_URL}/api/plans")
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
        return

    if status != 200:
        await message.answer("Не удалось получить список тарифов. Попробуйте позже.")
        return

    plans = data or []
    if not plans:
        await message.answer("Тарифы пока не настроены. Попробуйте позже.")
        return

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=_format_plan_button(plan), callback_data=f"plan:{plan['id']}"
                )
            ]
            for plan in plans
        ]
    )
    await message.answer("Выберите тариф:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("plan:"))
async def create_invoice(callback_query: types.CallbackQuery):
    plan_id = int(callback_query.data.split(":", maxsplit=1)[1])
    payload = {"telegram_id": str(callback_query.from_user.id), "plan_id": plan_id}

    try:
        status, invoice = await _fetch_json(
            "POST", f"{BACKEND_URL}/api/invoices", json=payload
        )
    except aiohttp.ClientError:
        await callback_query.answer("Бэкенд недоступен", show_alert=True)
        return

    if status != 200:
        await callback_query.answer("Ошибка при создании счета", show_alert=True)
        return

    # Ask the backend to mint a provider invoice. This returns a pay_url
    # we can attach to an inline button — for Telegram Stars that's a
    # t.me/$... link, for CryptoBot a pay.crypt.bot link, etc. If the
    # provider call fails we still tell the user the invoice id so an
    # admin can mark it paid manually.
    try:
        co_status, checkout = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/invoices/{invoice['id']}/checkout",
            json={"provider": PAYMENT_PROVIDER},
        )
    except aiohttp.ClientError:
        co_status, checkout = 0, None

    if co_status != 200 or not checkout:
        await callback_query.message.answer(
            f"Счет #{invoice['id']} создан, но платёжный провайдер "
            "временно недоступен. Админ отметит оплату вручную."
        )
        await callback_query.answer()
        return

    pay_url = checkout.get("pay_url")
    pay_button = types.InlineKeyboardButton(text="Оплатить", url=pay_url)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[pay_button]])

    await callback_query.message.answer(
        f"Счёт #{invoice['id']} на {invoice.get('amount')} {invoice.get('currency','')}.\n"
        "Нажмите кнопку ниже для оплаты. Конфиг придёт автоматически после подтверждения.",
        reply_markup=keyboard,
    )
    await callback_query.answer()


# ---------------------------------------------------------------------------
# Telegram Stars payment flow.
#
# Stars are processed *inside* Telegram — there is no vendor webhook back
# to our backend. Instead we rely on the two updates the bot receives:
#
#   1. pre_checkout_query — must be answered with ok=True within 10s.
#   2. successful_payment — final confirmation the user paid in Stars.
#
# The bot forwards (2) to our backend's
# ``/api/payments/webhook/telegram_stars`` endpoint, authenticated with a
# shared secret. That endpoint runs the normal invoice→subscription
# pipeline, so the user ends up with credentials delivered automatically.
# ---------------------------------------------------------------------------


@router.pre_checkout_query()
async def _stars_pre_checkout(query: types.PreCheckoutQuery) -> None:
    # The Bot API requires answering within 10s. We always ok unless the
    # currency is wrong (which would mean a misconfigured client) — real
    # duplicate/expired checks happen server-side on the webhook.
    if query.currency != "XTR":
        await query.answer(ok=False, error_message="Unsupported currency")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def _stars_successful_payment(message: types.Message) -> None:
    sp = message.successful_payment
    if not sp or sp.currency != "XTR":
        return

    if not TELEGRAM_STARS_WEBHOOK_SECRET:
        logger.error(
            "successful_payment received but TELEGRAM_STARS_WEBHOOK_SECRET is not set; "
            "cannot forward to backend — invoice %s will stay pending",
            sp.invoice_payload,
        )
        await message.answer(
            "Оплата получена, но бот не настроен для автоподтверждения. "
            "Свяжитесь с админом."
        )
        return

    forward = {
        "invoice_payload": sp.invoice_payload,
        "total_amount": sp.total_amount,
        "currency": sp.currency,
        "telegram_payment_charge_id": sp.telegram_payment_charge_id,
    }

    try:
        status, _ = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/payments/webhook/telegram_stars",
            json=forward,
            headers={"X-Telegram-Stars-Secret": TELEGRAM_STARS_WEBHOOK_SECRET},
        )
    except aiohttp.ClientError:
        status = 0

    if status != 200:
        logger.error(
            "failed to forward Stars payment to backend (status=%s, invoice=%s)",
            status,
            sp.invoice_payload,
        )
        await message.answer(
            "Оплата получена, но бэкенд не подтвердил её. Админ разберётся."
        )
        return

    await message.answer(
        "Оплата получена! Конфиг будет выдан через минуту, команда /status покажет статус."
    )


@router.message(Command("status"))
@router.message(F.text == "Статус")
async def status(message: types.Message):
    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/users/by_telegram/{message.from_user.id}",
            headers=_admin_headers(message.from_user.id),
        )
        if status_code != 200:
            await message.answer("Подписок не найдено")
            return
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
        return

    lines = ["Твои подписки:"]
    for sub in data:
        lines.append(
            f"План: {sub['plan_name']}\n"
            f"Сервер: {sub.get('node') or sub.get('server', 'n/a')} ({sub.get('region', '??')})\n"
            f"Истекает: {sub['expires_at']}\nСтатус: {sub['status']}"
        )
    await message.answer("\n\n".join(lines))


@router.message(Command("invoices"))
async def list_pending_invoices(message: types.Message):
    if not _is_admin(message.from_user.id):
        await message.answer("Эта команда только для админов.")
        return

    try:
        status, invoices = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/invoices",
            params={"status": "pending"},
            headers=_admin_headers(message.from_user.id),
        )
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
        return

    if status != 200:
        await message.answer("Не удалось загрузить счета.")
        return

    if not invoices:
        await message.answer("Нет ожидающих оплаты счетов.")
        return

    keyboard_rows = []
    text_lines = ["Последние ожидающие счета:"]
    for inv in invoices:
        text_lines.append(
            f"#{inv['id']} — план: {inv['plan_name']}, пользователь: {inv['user_telegram_id']}, сумма: {inv['amount']} {inv['currency']}"
        )
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f"Отметить как оплаченный #{inv['id']}",
                    callback_data=f"invoice_paid:{inv['id']}",
                )
            ]
        )

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    await message.answer("\n".join(text_lines), reply_markup=keyboard)


@router.callback_query(F.data.startswith("invoice_paid:"))
async def mark_invoice_paid(callback_query: types.CallbackQuery):
    if not _is_admin(callback_query.from_user.id):
        await callback_query.answer("Недостаточно прав", show_alert=True)
        return

    invoice_id = callback_query.data.split(":", maxsplit=1)[1]

    try:
        status, invoice = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/invoices/{invoice_id}/mark_paid",
            headers=_admin_headers(callback_query.from_user.id),
        )
    except aiohttp.ClientError:
        await callback_query.answer("Бэкенд недоступен", show_alert=True)
        return

    if status == 404:
        await callback_query.answer("Счет не найден", show_alert=True)
        return
    if status != 200:
        await callback_query.answer("Не удалось отметить счет", show_alert=True)
        return

    await callback_query.answer("Счет отмечен как оплаченный")
    await callback_query.message.answer(f"Счет #{invoice['id']} отмечен как оплаченный")

    user_id = invoice.get("user_telegram_id")
    credentials = invoice.get("credentials", [])
    if user_id and credentials:
        configs_text = ["Твои конфиги:"]
        for cred in credentials:
            configs_text.append(f"{cred['proto']}:\n{cred['config_text']}")
        await callback_query.message.bot.send_message(
            chat_id=user_id, text="\n\n".join(configs_text)
        )
    elif user_id:
        await callback_query.message.bot.send_message(
            chat_id=user_id,
            text="Счет оплачен, но конфиги пока не готовы. Свяжитесь с поддержкой.",
        )
