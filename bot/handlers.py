import asyncio
import logging

import aiohttp
from aiogram import F, Router, types
from aiogram.filters import Command, CommandStart

from .config import (
    ADMIN_API_TOKEN,
    ADMIN_IDS,
    BACKEND_URL,
    PAYMENT_PROVIDER,
    SUB_LINK_BASE_URL,
    TELEGRAM_STARS_WEBHOOK_SECRET,
)
from .keyboards import start_keyboard, onboarding_keyboard

router = Router()
logger = logging.getLogger(__name__)

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
    name = plan.get("name", "")
    if duration >= 365:
        return f"{name} — {price}$ / год"
    elif duration >= 28:
        months = duration // 30
        return f"{name} — {price}$ / {months}мес"
    return f"{name} — {price}$ / {duration}д"


async def _fetch_json(method: str, url: str, **kwargs):
    """Call backend with retries and a shared session."""
    session = await get_session()
    last_exc: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            async with session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                try:
                    payload = await resp.json()
                except Exception:
                    payload = {"message": text}
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


# ── Onboarding instructions ──────────────────────────────────────────

ONBOARDING_INSTRUCTIONS = {
    "android": (
        "<b>Настройка на Android (v2rayNG):</b>\n\n"
        "1. Установите <b>v2rayNG</b> из Google Play или GitHub\n"
        "2. Скопируйте ссылку конфига (команда /config)\n"
        "3. Откройте v2rayNG → нажмите <b>+</b> → <b>Импорт из буфера</b>\n"
        "4. Нажмите кнопку ▶️ для подключения\n\n"
        "Альтернатива: <b>Hiddify</b> (Google Play) — автоимпорт по ссылке."
    ),
    "ios": (
        "<b>Настройка на iOS (Hiddify / Streisand):</b>\n\n"
        "1. Установите <b>Hiddify</b> или <b>Streisand</b> из App Store\n"
        "2. Скопируйте ссылку конфига (команда /config)\n"
        "3. Откройте приложение → <b>+</b> → <b>Добавить из буфера</b>\n"
        "4. Нажмите <b>Подключить</b>"
    ),
    "windows": (
        "<b>Настройка на Windows (Hiddify / Nekoray):</b>\n\n"
        "1. Скачайте <b>Hiddify</b> с hiddify.com или <b>Nekoray</b> с GitHub\n"
        "2. Скопируйте ссылку конфига (команда /config)\n"
        "3. В программе: <b>Добавить профиль из буфера</b>\n"
        "4. Активируйте системный прокси и подключитесь"
    ),
    "macos": (
        "<b>Настройка на macOS (Hiddify):</b>\n\n"
        "1. Скачайте <b>Hiddify</b> с hiddify.com\n"
        "2. Скопируйте ссылку конфига (команда /config)\n"
        "3. Добавьте профиль из буфера обмена\n"
        "4. Подключитесь"
    ),
}


# ── /start ──

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    # Check for referral deep link: /start ref_XXXXX
    args = message.text.split(maxsplit=1)
    referral_code = None
    if len(args) > 1 and args[1].startswith("ref_"):
        referral_code = args[1][4:]

    # Register user (and apply referral if present)
    register_payload = {"telegram_id": str(message.from_user.id)}
    if referral_code:
        register_payload["referral_code"] = referral_code

    try:
        await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/users/register",
            json=register_payload,
            headers=_admin_headers(message.from_user.id),
        )
    except Exception:
        pass  # Registration is best-effort; user will be created on first purchase

    await message.answer(
        "Привет! Это VPN-сервис.\n\n"
        "Доступные протоколы: VLESS Reality, VLESS+WS+CDN, ShadowTLS+SS, Hysteria2.\n\n"
        "Используй кнопки ниже или команды:\n"
        "/plans — выбрать тариф\n"
        "/config — получить конфиг\n"
        "/status — статус подписки\n"
        "/renew — продлить подписку\n"
        "/referral — реферальная ссылка",
        reply_markup=start_keyboard(),
    )


# ── /plans ──

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


# ── Stars payment flow ──

@router.pre_checkout_query()
async def _stars_pre_checkout(query: types.PreCheckoutQuery) -> None:
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
        logger.error("successful_payment received but TELEGRAM_STARS_WEBHOOK_SECRET is not set")
        await message.answer(
            "Оплата получена, но бот не настроен для автоподтверждения. Свяжитесь с админом."
        )
        return

    forward = {
        "invoice_payload": sp.invoice_payload,
        "total_amount": sp.total_amount,
        "currency": sp.currency,
        "telegram_payment_charge_id": sp.telegram_payment_charge_id,
    }

    try:
        status, result = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/payments/webhook/telegram_stars",
            json=forward,
            headers={"X-Telegram-Stars-Secret": TELEGRAM_STARS_WEBHOOK_SECRET},
        )
    except aiohttp.ClientError:
        status = 0
        result = {}

    if status != 200:
        logger.error("failed to forward Stars payment (status=%s)", status)
        await message.answer("Оплата получена, но бэкенд не подтвердил её. Админ разберётся.")
        return

    await message.answer(
        "✅ Оплата получена! Конфиг будет готов через минуту.\n"
        "Используй /config чтобы получить ссылку для подключения.",
        reply_markup=onboarding_keyboard(),
    )


# ── /config — получить конфиг подключения ──

@router.message(F.text == "Мой конфиг")
@router.message(Command("config"))
async def cmd_config(message: types.Message):
    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/users/by_telegram/{message.from_user.id}",
            headers=_admin_headers(message.from_user.id),
        )
        if status_code != 200:
            await message.answer("У тебя пока нет активных подписок. Используй /plans для покупки.")
            return
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
        return

    # Find the latest active subscription with active credentials
    active_sub = None
    for sub in data:
        if sub.get("status") == "active" and sub.get("credentials"):
            active_sub = sub
            break

    if not active_sub:
        # Check if there's a pending device
        for sub in data:
            if sub.get("devices"):
                for dev in sub["devices"]:
                    if dev.get("status") == "pending":
                        await message.answer(
                            "⏳ Твой конфиг ещё создаётся. Подожди минуту и попробуй снова."
                        )
                        return
        await message.answer("У тебя нет активных подписок с готовыми конфигами. Используй /plans.")
        return

    # Build config message with all credentials
    lines = ["🔐 <b>Твои конфиги для подключения:</b>\n"]
    for cred in active_sub["credentials"]:
        proto = cred.get("proto", "unknown")
        config_text = cred.get("config_text", "")
        if config_text:
            lines.append(f"<b>{proto}:</b>")
            lines.append(f"<code>{config_text}</code>\n")

    # Add dynamic subscription link if available
    sub_token = active_sub.get("sub_token")
    if sub_token and SUB_LINK_BASE_URL:
        sub_url = f"{SUB_LINK_BASE_URL.rstrip('/')}/{sub_token}"
        lines.append(f"🔗 <b>Ссылка подписки</b> (автообновляется при смене сервера):")
        lines.append(f"<code>{sub_url}</code>\n")

    lines.append(
        "Не знаешь как настроить? Нажми кнопку с твоей платформой ниже 👇"
    )

    await message.answer(
        "\n".join(lines),
        reply_markup=onboarding_keyboard(),
        parse_mode="HTML",
    )


# ── Onboarding instructions callbacks ──

@router.callback_query(F.data.startswith("onboard:"))
async def onboarding_instruction(callback_query: types.CallbackQuery):
    platform = callback_query.data.split(":", maxsplit=1)[1]
    text = ONBOARDING_INSTRUCTIONS.get(platform, "Инструкция не найдена.")
    await callback_query.message.answer(text, parse_mode="HTML")
    await callback_query.answer()


# ── /status ──

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
            await message.answer("Подписок не найдено. Используй /plans для покупки.")
            return
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
        return

    lines = ["📊 <b>Твои подписки:</b>\n"]
    for sub in data:
        auto_renew = "✅" if sub.get("auto_renew") else "❌"
        lines.append(
            f"<b>План:</b> {sub['plan_name']}\n"
            f"<b>Сервер:</b> {sub.get('node') or sub.get('server', 'n/a')} ({sub.get('region', '??')})\n"
            f"<b>Истекает:</b> {sub['expires_at'][:10]}\n"
            f"<b>Статус:</b> {sub['status']}\n"
            f"<b>Автопродление:</b> {auto_renew}"
        )
    await message.answer("\n\n".join(lines), parse_mode="HTML")


# ── /renew — продление подписки ──

@router.message(F.text == "Продлить")
@router.message(Command("renew"))
async def cmd_renew(message: types.Message):
    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/users/by_telegram/{message.from_user.id}",
            headers=_admin_headers(message.from_user.id),
        )
        if status_code != 200 or not data:
            await message.answer("Нет подписок для продления. Используй /plans.")
            return
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
        return

    # Find active or recently expired subscriptions
    renewable = [
        sub for sub in data
        if sub.get("status") in ("active", "expired")
    ]
    if not renewable:
        await message.answer("Нет подписок для продления. Используй /plans для новой покупки.")
        return

    # Create renewal invoice for the most recent subscription
    sub = renewable[0]
    payload = {
        "telegram_id": str(message.from_user.id),
        "plan_id": sub.get("plan_id", 1),
        "subscription_id": sub["id"],
        "action": "renewal",
    }

    try:
        inv_status, invoice = await _fetch_json(
            "POST", f"{BACKEND_URL}/api/invoices", json=payload
        )
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен.")
        return

    if inv_status != 200:
        await message.answer("Не удалось создать счет на продление.")
        return

    try:
        co_status, checkout = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/invoices/{invoice['id']}/checkout",
            json={"provider": PAYMENT_PROVIDER},
        )
    except aiohttp.ClientError:
        co_status, checkout = 0, None

    if co_status != 200 or not checkout:
        await message.answer(
            f"Счет #{invoice['id']} на продление создан, но платёжный провайдер "
            "временно недоступен."
        )
        return

    pay_url = checkout.get("pay_url")
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="Оплатить продление", url=pay_url)],
            [types.InlineKeyboardButton(
                text="Включить автопродление",
                callback_data=f"auto_renew:{sub['id']}",
            )],
        ]
    )
    await message.answer(
        f"💳 Продление подписки #{sub['id']}\n"
        f"Сумма: {invoice.get('amount')} {invoice.get('currency', '')}\n\n"
        "Нажми кнопку для оплаты. Или включи автопродление.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("auto_renew:"))
async def toggle_auto_renew(callback_query: types.CallbackQuery):
    sub_id = int(callback_query.data.split(":", maxsplit=1)[1])
    try:
        status_code, _ = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/subscriptions/{sub_id}/auto_renew",
            json={"auto_renew": True},
            headers=_admin_headers(callback_query.from_user.id),
        )
    except aiohttp.ClientError:
        await callback_query.answer("Бэкенд недоступен", show_alert=True)
        return

    if status_code == 200:
        await callback_query.answer("Автопродление включено!")
        await callback_query.message.answer("✅ Автопродление включено. Счет будет создан за 3 дня до истечения.")
    else:
        await callback_query.answer("Не удалось включить автопродление", show_alert=True)


# ── /referral — реферальная ссылка ──

@router.message(F.text == "Реферальная ссылка")
@router.message(Command("referral"))
async def cmd_referral(message: types.Message):
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/referral/code",
            json={"telegram_id": str(message.from_user.id)},
            headers=_admin_headers(message.from_user.id),
        )
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен.")
        return

    if status_code != 200 or not data:
        await message.answer("Не удалось получить реферальную ссылку.")
        return

    code = data.get("code", "")
    uses = data.get("uses", 0)
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{code}"

    await message.answer(
        f"🎁 <b>Твоя реферальная ссылка:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"Приглашённый получает +3 дня бесплатно, ты тоже получаешь +3 дня.\n"
        f"Приглашено: {uses} чел.",
        parse_mode="HTML",
    )


# ── Self-service: смена ноды, перегенерация конфига ──

@router.message(Command("newconfig"))
async def cmd_new_config(message: types.Message):
    """Regenerate VPN config on a different node (self-service migration)."""
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/users/by_telegram/{message.from_user.id}/regenerate",
            headers=_admin_headers(message.from_user.id),
        )
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен.")
        return

    if status_code == 200:
        await message.answer(
            "🔄 Конфиг перегенерирован. Через минуту используй /config для получения нового конфига."
        )
    elif status_code == 404:
        await message.answer("Нет активных подписок.")
    else:
        await message.answer("Не удалось перегенерировать конфиг. Попробуй позже.")


# ── Admin commands ──

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
        await message.answer("Бэкенд недоступен.")
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
            f"#{inv['id']} — план: {inv['plan_name']}, "
            f"пользователь: {inv['user_telegram_id']}, "
            f"сумма: {inv['amount']} {inv['currency']}"
        )
        keyboard_rows.append([
            types.InlineKeyboardButton(
                text=f"Отметить #{inv['id']}", callback_data=f"invoice_paid:{inv['id']}"
            )
        ])

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

    # Push config to user
    user_id = invoice.get("user_telegram_id")
    credentials = invoice.get("credentials", [])
    if user_id and credentials:
        configs_text = ["✅ Оплата подтверждена! Твои конфиги:\n"]
        for cred in credentials:
            configs_text.append(f"<b>{cred['proto']}:</b>")
            configs_text.append(f"<code>{cred['config_text']}</code>")
        configs_text.append("\nНе знаешь как настроить? 👇")
        await callback_query.message.bot.send_message(
            chat_id=user_id,
            text="\n".join(configs_text),
            parse_mode="HTML",
            reply_markup=onboarding_keyboard(),
        )
    elif user_id:
        await callback_query.message.bot.send_message(
            chat_id=user_id,
            text="✅ Счет оплачен! Конфиг будет готов через минуту. Используй /config.",
        )
