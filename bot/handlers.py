import asyncio
import logging
import os
from urllib.parse import urlparse

import aiohttp
from aiogram import F, Router, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext

from .config import (
    ADMIN_API_TOKEN,
    ADMIN_IDS,
    BACKEND_URL,
    PAYMENT_PROVIDER,
    SUB_LINK_BASE_URL,
    TELEGRAM_STARS_WEBHOOK_SECRET,
)


def _build_sub_url(sub_token: str | None) -> str | None:
    """Абсолютный URL подписки для показа юзеру.

    Порядок приоритетов:
    1. SUB_LINK_BASE_URL — если задан (например, CF-воркер), используем как есть.
    2. WEBAPP_BASE_URL — берём origin (https://grinwer.online) и приклеиваем
       /api/sub/{token}. В проде всегда задан, потому что без него
       Telegram Mini App не запускается — фактически это гарантированный
       fallback.
    3. None — значит URL построить неоткуда, вызывающий код должен это
       обработать (сейчас такого в проде не случается).
    """
    if not sub_token:
        return None
    if SUB_LINK_BASE_URL:
        return f"{SUB_LINK_BASE_URL.rstrip('/')}/{sub_token}"
    webapp_base = os.getenv("WEBAPP_BASE_URL", "")
    if webapp_base:
        parsed = urlparse(webapp_base)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/api/sub/{sub_token}"
    return None
from .keyboards import (
    BTN_BUY,
    BTN_HELP,
    BTN_INVITE,
    BTN_MAIN_MENU,
    BTN_TOPUP,
    BTN_VPN_BROKEN,
    help_back_keyboard,
    help_keyboard,
    onboarding_keyboard,
    start_keyboard,
    webapp_inline_keyboard,
    welcome_action_keyboard,
)

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
    devices = plan.get("max_devices")
    devices_label = f" · до {devices} устр." if devices else ""
    if duration >= 365:
        return f"{name} — {price}₽ / год{devices_label}"
    elif duration >= 28:
        months = duration // 30
        return f"{name} — {price}₽ / {months}мес{devices_label}"
    return f"{name} — {price}₽ / {duration}д{devices_label}"


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


# ── Self-report VPN breakage debounce ──
#
# In-memory {telegram_id: monotonic seconds} — last time this user pressed
# «🆘 VPN не работает». Не сохраняется между рестартами бота, и это ОК:
# задача cooldown — защитить от случайного двойного тапа и спама, не от
# долгосрочного злоупотребления. Бэкенд всё равно принимает повтор —
# 2-3 лишние строчки в AuditLog не страшны.
_SELF_REPORT_COOLDOWN_S = 300  # 5 минут
_self_report_last: dict[int, float] = {}


# Удаляем ack-сообщение опроса «Помогите нам улучшить сервис» через N сек.
# после ответа юзера, чтобы чат не захламлялся. Telegram даёт боту удалять
# свои сообщения в private chat без админ-прав в течение 48ч — этого
# окна с большим запасом хватает.
HEALTH_PING_ACK_DELETE_DELAY_S = int(
    os.getenv("HEALTH_PING_ACK_DELETE_DELAY_S", "60")
)


async def _delete_message_after(
    bot, chat_id: int, message_id: int, delay_s: int
) -> None:
    """Fire-and-forget delete с задержкой. Best-effort: любые исключения
    Telegram (сообщение уже удалено юзером, бот без прав, network) тихо
    глотаем — это всегда косметика, никогда не критично.
    """
    try:
        await asyncio.sleep(delay_s)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:  # noqa: BLE001
        pass


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

_TRIAL_LINE = "🎁 Первый месяц — в подарок. Забери в личном кабинете.\n\n"


def format_welcome(name: str, is_new: bool, trial_available: bool) -> str:
    if is_new:
        body = (
            f"👋 Привет, {name}!\n\n"
            "🚀 Быстрый VPN без танцев с настройками:\n"
            "• Работает там, где другие отвалились — обход блокировок на уровне протокола\n"
            "• Оплата прямо в Telegram, без карт и регистраций\n"
            "• Один тариф — до 5 устройств одновременно\n"
            "• Поддержка отвечает в чате, не роботом\n\n"
        )
    else:
        body = f"👋 Рад снова видеть, {name}!\n\n"
    if trial_available:
        body += _TRIAL_LINE
    body += "Выбери действие ниже 👇"
    return body


@router.message(F.text == BTN_MAIN_MENU)
@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    # Check for deep link arguments: /start ref_XXXXX or /start support
    args = message.text.split(maxsplit=1)

    # Deep link from webapp: /start support → jump straight to support flow
    if len(args) > 1 and args[1].strip() == "support":
        from .support import SupportStates
        from .config import ADMIN_IDS
        if not ADMIN_IDS:
            await message.answer("Поддержка пока не настроена.")
            return
        await state.set_state(SupportStates.waiting_user_message)
        await message.answer(
            "💬 Опиши проблему одним сообщением — текст, фото или голосовое. "
            "Мы передадим админу, ответ придёт сюда же.\n\n"
            "Отменить — /cancel.",
        )
        return

    referral_code = None
    if len(args) > 1 and args[1].startswith("ref_"):
        referral_code = args[1][4:]

    # Register user (and apply referral if present). Backend returns
    # {"created": bool} so we can pick a new-vs-returning welcome copy.
    # Failures are still best-effort — worst case we greet them as
    # returning; they'll be created on first purchase anyway.
    register_payload = {"telegram_id": str(message.from_user.id)}
    if referral_code:
        register_payload["referral_code"] = referral_code

    is_new = False
    trial_available = False
    try:
        _status, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/users/register",
            json=register_payload,
            headers=_admin_headers(message.from_user.id),
        )
        is_new = bool(data and data.get("created"))
        trial_available = bool(data and data.get("trial_available"))
    except Exception:
        pass

    first_name = message.from_user.first_name or "друг"
    welcome = format_welcome(first_name, is_new, trial_available)

    # Telegram allows only one reply_markup per message, so we send two:
    #   1) Welcome + inline action keyboard (WebApp button + quick actions)
    #   2) Tiny nudge + persistent reply keyboard (always at the bottom,
    #      for both new and returning users — returning users complained
    #      the bottom buttons disappeared).
    await message.answer(welcome, reply_markup=welcome_action_keyboard())
    hint = (
        "⌨️ Кнопки внизу всегда под рукой. Если что-то сломалось — жми /help."
        if is_new
        else "⌨️ Кнопки внизу всегда под рукой."
    )
    await message.answer(hint, reply_markup=start_keyboard())


# ── /plans ──

@router.message(F.text == BTN_BUY)
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

    plans = [p for p in (data or []) if p.get("is_visible", True)]
    if not plans:
        await message.answer("Тарифы пока не настроены. Попробуйте позже.")
        return

    # Group monthly vs yearly so it's obvious there are two billing cycles
    # of the same tier; sort by max_devices so cheap → premium reads top-down.
    monthly = sorted(
        [p for p in plans if p.get("duration_days", 0) < 365],
        key=lambda p: p.get("max_devices") or 0,
    )
    yearly = sorted(
        [p for p in plans if p.get("duration_days", 0) >= 365],
        key=lambda p: p.get("max_devices") or 0,
    )

    lines: list[str] = [
        "<b>Все тарифы дают одно и то же:</b>",
        "• безлимитный трафик",
        "• автоматическое переключение между протоколами",
        "• работают на iOS, Android, Windows, macOS, Linux",
        "",
        "<b>Отличие — только в количестве устройств одновременно.</b>",
        "",
    ]
    def _row(p: dict) -> str:
        star = " ⭐" if (p.get("max_devices") == 3) else ""
        return (
            f"• <b>{p['name']}</b>{star} — до {p.get('max_devices', '?')} устр. "
            f"— {p.get('price', '?')}₽"
        )

    if monthly:
        lines.append("<b>Месяц:</b>")
        for p in monthly:
            lines.append(_row(p))
        lines.append("")
    if yearly:
        lines.append("<b>Год (выгоднее ~20%):</b>")
        for p in yearly:
            lines.append(_row(p))
        lines.append("")
    lines.append("⭐ — самый популярный")
    lines.append("Выберите тариф ⬇️")

    rows = [
        [
            types.InlineKeyboardButton(
                text=_format_plan_button(plan), callback_data=f"plan:{plan['id']}"
            )
        ]
        for plan in monthly + yearly
    ]
    rows.append(
        [types.InlineKeyboardButton(text="🤔 Какой выбрать?", callback_data="plans:help")]
    )
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("\n".join(lines), reply_markup=keyboard)


@router.callback_query(F.data == "plans:help")
async def plans_help(callback_query: types.CallbackQuery):
    await callback_query.answer()
    text = (
        "<b>Какой тариф выбрать?</b>\n\n"
        "• <b>Solo</b> — 1 устройство. Берите, если VPN нужен только на телефоне "
        "<i>или</i> только на ноутбуке.\n\n"
        "• <b>Family</b> ⭐ — 3 устройства. Самый ходовой: телефон + ноут + "
        "планшет, или вы делитесь с близкими.\n\n"
        "• <b>Pro</b> — 5 устройств. Если у вас много техники или нужно покрыть "
        "всю семью (родители, дети, партнёр).\n\n"
        "<b>Месяц или год?</b>\n"
        "Годовой ~на 20% дешевле в пересчёте на месяц. Берите год, если уже "
        "пользовались VPN раньше и точно знаете, что он вам нужен надолго.\n\n"
        "Передумаете — список устройств можно перезаписать в любой момент."
    )
    await callback_query.message.answer(text)


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
        status, _ = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/payments/webhook/telegram_stars",
            json=forward,
            headers={"X-Telegram-Stars-Secret": TELEGRAM_STARS_WEBHOOK_SECRET},
        )
    except aiohttp.ClientError:
        status = 0

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

    # /config отдаёт ОДНУ подписочную ссылку, а не список vless://.
    # Клиенты (Hiddify / V2rayNG / Streisand) сами разворачивают её в
    # набор серверов и потом рефрешат при миграциях. Список из 10
    # сырых vless-линков нужен был только как дебажный fallback на
    # случай, когда SUB_LINK_BASE_URL не задан — теперь fallback берём
    # из WEBAPP_BASE_URL, поэтому URL всегда есть.
    sub_token = active_sub.get("sub_token")
    sub_url = _build_sub_url(sub_token)
    if not sub_url:
        # Такое случается только в dev-окружении без WEBAPP_BASE_URL.
        logger.warning(
            "cmd_config: no sub_url for user %s (SUB_LINK_BASE_URL + WEBAPP_BASE_URL оба пусты)",
            message.from_user.id,
        )
        await message.answer(
            "Не удалось собрать ссылку подписки. Напиши в поддержку."
        )
        return

    await message.answer(
        "🔗 <b>Твоя ссылка подписки</b>\n"
        "Импортируй её в VPN-клиент (Hiddify / V2rayNG / Streisand) один раз — "
        "при смене сервера клиент сам подтянет новые настройки по этой ссылке.\n\n"
        "⬇️ Ссылка следующим сообщением, тапни чтобы скопировать.\n\n"
        "Не знаешь как настроить? Нажми кнопку с твоей платформой ниже 👇",
        reply_markup=onboarding_keyboard(),
        parse_mode="HTML",
    )
    await message.answer(
        f"<code>{sub_url}</code>",
        parse_mode="HTML",
        disable_web_page_preview=True,
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


# ── Phase C: bot health-ping responses ──
#
# Three callback shapes (see health_ping_keyboard() in keyboards.py):
#   hping:ok:<sub_id>     → user says VPN works
#   hping:bad:<sub_id>    → user says VPN doesn't work
#   hping:optout          → "не показывать этот опрос" (no sub_id)
#
# All three POST to backend and edit the original prompt in-place to
# acknowledge the response so the inline keyboard goes away.

@router.callback_query(F.data.startswith("hping:"))
async def health_ping_response(callback_query: types.CallbackQuery):
    parts = callback_query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    sub_id: int | None = None
    if len(parts) > 2 and parts[2].isdigit():
        sub_id = int(parts[2])

    user_telegram_id = str(callback_query.from_user.id)

    if action == "optout":
        try:
            status_code, _ = await _fetch_json(
                "POST",
                f"{BACKEND_URL}/api/users/health-ping-opt-out",
                json={"telegram_id": user_telegram_id},
                headers=_admin_headers(callback_query.from_user.id),
            )
        except aiohttp.ClientError:
            await callback_query.answer("Бэкенд недоступен", show_alert=True)
            return
        if status_code != 200:
            await callback_query.answer("Не удалось сохранить", show_alert=True)
            return
        await callback_query.answer("Больше не побеспокоим 👌")
        try:
            await callback_query.message.edit_text(
                "👍 Окей, больше не будем спрашивать. "
                "Если что-то сломается — пиши в /help."
            )
        except Exception:
            pass
        return

    if action not in ("ok", "bad"):
        await callback_query.answer()
        return

    payload: dict = {"telegram_id": user_telegram_id, "answer": action}
    if sub_id is not None:
        payload["subscription_id"] = sub_id
    try:
        status_code, _ = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/users/health-ping-response",
            json=payload,
            headers=_admin_headers(callback_query.from_user.id),
        )
    except aiohttp.ClientError:
        await callback_query.answer("Бэкенд недоступен", show_alert=True)
        return
    if status_code != 200:
        await callback_query.answer("Не удалось сохранить", show_alert=True)
        return

    if action == "ok":
        await callback_query.answer("Спасибо! 💛")
        ack_text = (
            "🙌 Спасибо за помощь! Ваш ответ помогает нам "
            "следить за качеством сервиса."
        )
    else:
        await callback_query.answer("Спасибо! Чиним.")
        ack_text = (
            "🛠 Спасибо! Мы получили сигнал и проверяем ваш сервер.\n"
            "Если проблема не уйдёт за 10 минут — напишите в /help, "
            "приложите модель устройства."
        )
    try:
        await callback_query.message.edit_text(ack_text)
    except Exception:
        pass
    # Авто-удаление ack-сообщения через HEALTH_PING_ACK_DELETE_DELAY_S.
    # Применяется к обоим веткам (ok/bad) — для bad важная инфа продублирована
    # в toast `callback_query.answer("Спасибо! Чиним.")` и админы уже
    # оповещены через notify_admins, повторное чтение бабла юзеру не нужно.
    if HEALTH_PING_ACK_DELETE_DELAY_S > 0:
        asyncio.create_task(
            _delete_message_after(
                callback_query.bot,
                callback_query.message.chat.id,
                callback_query.message.message_id,
                HEALTH_PING_ACK_DELETE_DELAY_S,
            )
        )


# Self-report: юзер жмёт «🆘 VPN не работает» в reply-клавиатуре.
# Плановый health-ping приходит юзеру раз в 7-14 дней (random jitter),
# только в обеденное окно МСК (11–14) —
# self-report закрывает эту дыру и позволяет пожаловаться прямо сейчас.
# source="self_reported" помечает запись, чтобы админка выделяла такие
# жалобы красным как более сильный сигнал, чем ответ на плановый пинг.
@router.message(F.text == BTN_VPN_BROKEN)
async def self_report_vpn_broken(message: types.Message) -> None:
    # Operator-aware routing P1: тап → бэкенд авто-мигрирует на свободную
    # ноду (+бан старой) и заводит OperatorNodeReport → спрашиваем оператора
    # одним тапом. «Всё равно не работает» → поддержка. Watcher через 15м
    # проставит исход по факту переподключения.
    tg_id = message.from_user.id
    now_mono = asyncio.get_event_loop().time()
    last = _self_report_last.get(tg_id)
    if last is not None and (now_mono - last) < _SELF_REPORT_COOLDOWN_S:
        await message.answer(
            "👍 Мы уже перекинули тебя на другой сервер недавно. Дай минуту "
            "переподключиться. Если через 10 минут не работает — /help."
        )
        return

    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/admin/client-control/report-broken",
            json={"telegram_id": str(tg_id)},
            headers=_admin_headers(tg_id),
        )
    except aiohttp.ClientError:
        await message.answer(
            "Не получилось обработать — попробуй ещё раз через минуту."
        )
        return
    if status_code != 200 or not isinstance(data, dict):
        await message.answer(
            "Не получилось обработать — попробуй ещё раз через минуту."
        )
        return

    action = data.get("action")
    if action == "migrated":
        _self_report_last[tg_id] = now_mono
        report_id = data.get("report_id")
        await message.answer(
            "🔄 Поменяли тебе сервер. Подписка обновится в приложении сама — "
            "нажми 🔄 рядом с профилем и попробуй подключиться через пару минут.\n\n"
            "Чтобы мы быстрее ловили блокировки — подскажи, какой у тебя интернет?",
            reply_markup=(
                operator_keyboard(int(report_id)) if report_id else help_keyboard()
            ),
        )
        # «Всё равно не работает» отдаём НЕ сразу, а через делей — даём юзеру
        # время переподключиться, чтобы он не ломился в поддержку с порога.
        if report_id:
            asyncio.create_task(
                _delayed_still_broken_prompt(
                    message.bot, message.chat.id, int(report_id)
                )
            )
    elif action == "throttled":
        _self_report_last[tg_id] = now_mono
        await message.answer(
            "👍 Уже перекидывали недавно — дай минуту переподключиться. "
            "Если не помогло за 10 минут — /help."
        )
    elif action == "no_subscription":
        await message.answer("У тебя нет активной подписки. Оформить — /buy.")
    else:  # no_target / user_not_found / прочее
        await message.answer(
            "Не смогли автоматически подобрать другой сервер. Напиши в "
            "поддержку — разберёмся вручную.",
            reply_markup=help_keyboard(),
        )


# «Всё равно не работает» показываем не сразу, а через этот делей — даём
# юзеру время переподключиться. Через делей дёргаем бэк: если он ВИДИТ
# переподключение — молчим; если нет — присылаем пуш с этой кнопкой.
_STILL_BROKEN_DELAY_S = 300


def _still_broken_keyboard(report_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="❌ Всё равно не работает",
                    callback_data=f"op:still:{report_id}",
                )
            ]
        ]
    )


async def _delayed_still_broken_prompt(bot, chat_id: int, report_id: int) -> None:
    """Через делей: если юзер НЕ переподключился — предложить «всё равно не
    работает» → поддержка. Если бэк видит переподключение — молчим."""
    await asyncio.sleep(_STILL_BROKEN_DELAY_S)
    # Пушим только если бэкенд НЕ видит переподключения за это время.
    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/admin/client-control/report-status/{report_id}",
            headers=_admin_headers(chat_id),
        )
        if (
            status_code == 200
            and isinstance(data, dict)
            and data.get("reconnected")
        ):
            return  # переподключился — не дёргаем
    except aiohttp.ClientError:
        pass  # бэк недоступен — лучше предложить помощь, чем промолчать
    try:
        await bot.send_message(
            chat_id,
            "Похоже, подключиться так и не вышло 🤔 Всё ещё не работает?",
            reply_markup=_still_broken_keyboard(report_id),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "delayed still-broken prompt failed for chat %s", chat_id
        )


def operator_keyboard(report_id: int) -> types.InlineKeyboardMarkup:
    """Выбор оператора после авто-миграции (operator-aware routing P1).

    Без «всё равно не работает» — она приходит отдельным сообщением через
    делей (см. _delayed_still_broken_prompt).
    """

    def _b(text: str, op: str) -> types.InlineKeyboardButton:
        return types.InlineKeyboardButton(
            text=text, callback_data=f"op:{report_id}:{op}"
        )

    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [_b("МТС", "mts"), _b("Билайн", "beeline")],
            [_b("МегаФон", "megafon"), _b("Tele2", "tele2")],
            [_b("🏠 Домашний/WiFi", "home_wifi"), _b("Другое", "other")],
        ]
    )


@router.callback_query(F.data.startswith("op:"))
async def operator_choice(callback_query: types.CallbackQuery) -> None:
    parts = callback_query.data.split(":")
    tg_id = callback_query.from_user.id
    if len(parts) < 3:
        await callback_query.answer()
        return

    # op:still:<report_id> — «всё равно не работает» → поддержка + target=fail.
    if parts[1] == "still":
        try:
            await _fetch_json(
                "POST",
                f"{BACKEND_URL}/api/admin/client-control/report-still-broken",
                json={"report_id": int(parts[2])},
                headers=_admin_headers(tg_id),
            )
        except (aiohttp.ClientError, ValueError):
            pass
        await callback_query.answer()
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await callback_query.message.answer(
            "Жаль, что не помогло 😕 Передаём в поддержку — опиши проблему и "
            "приложи модель устройства.",
            reply_markup=help_keyboard(),
        )
        return

    # op:<report_id>:<operator> — записываем оператора.
    try:
        report_id = int(parts[1])
    except ValueError:
        await callback_query.answer()
        return
    operator = parts[2]
    try:
        await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/admin/client-control/report-operator",
            json={"report_id": report_id, "operator": operator},
            headers=_admin_headers(tg_id),
        )
    except aiohttp.ClientError:
        pass
    await callback_query.answer("Спасибо! 🙏")
    # Операторскую клавиатуру убираем. «Всё равно не работает» придёт
    # отдельным сообщением через делей (_delayed_still_broken_prompt).
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


def health_ping_keyboard(sub_id: int | None) -> types.InlineKeyboardMarkup:
    """Build the 3-button inline keyboard for the health-ping prompt.

    sub_id may be None if the worker couldn't resolve a node for the
    user (shouldn't happen in practice — the tick filters on active
    subs — but the callback handler still works without it).
    """
    sid_part = str(sub_id) if sub_id is not None else "0"
    rows = [
        [
            types.InlineKeyboardButton(
                text="✅ Всё работает",
                callback_data=f"hping:ok:{sid_part}",
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="❌ Не работает",
                callback_data=f"hping:bad:{sid_part}",
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="🙅 Не показывать",
                callback_data="hping:optout",
            ),
        ],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def node_diagnosis_keyboard(
    kind: str | None, target_id: int | None
) -> types.InlineKeyboardMarkup | None:
    """Inline keyboard for the «нода диагностирована» admin push.

    Mirrors the diagnose-control endpoints (POST /api/diagnostics/{kind}/{id}/...):
    ack ("вижу, работаю"), mute на 1/4/24ч или совсем, follow (экспонента).
    Все callback_data парсятся одним хендлером diag_control() ниже.

    target_id None → пуш собрался криво (бэкенд не проставил target_id);
    возвращаем None, чтобы send_message ушёл без клавиатуры, а не падал.
    """
    if target_id is None or not kind:
        return None
    rows = [
        [
            types.InlineKeyboardButton(
                text="👀 Вижу, работаю",
                callback_data=f"diag:ack:{kind}:{target_id}",
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="🔕 1ч", callback_data=f"diag:mute:{kind}:{target_id}:1"
            ),
            types.InlineKeyboardButton(
                text="🔕 4ч", callback_data=f"diag:mute:{kind}:{target_id}:4"
            ),
            types.InlineKeyboardButton(
                text="🔕 24ч", callback_data=f"diag:mute:{kind}:{target_id}:24"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="🔕 совсем",
                callback_data=f"diag:mute:{kind}:{target_id}:-1",
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="📈 Следить (экспонента)",
                callback_data=f"diag:follow:{kind}:{target_id}",
            ),
        ],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# ── Node-diagnosis admin push: inline control callbacks ──
#
# Все кнопки из node_diagnosis_keyboard() дёргают diagnose-control
# эндпоинты бэкенда. callback_data:
#   diag:ack:{kind}:{id}            → POST .../ack            (тело: нет)
#   diag:mute:{kind}:{id}:{hours}   → POST .../mute  {"hours": int}
#   diag:follow:{kind}:{id}         → POST .../follow {"mode":"exponential"}
# kind ∈ {"node","exit"}. Только для админов — mirror mark_invoice_paid.

@router.callback_query(F.data.startswith("diag:"))
async def diag_control(callback_query: types.CallbackQuery):
    if not _is_admin(callback_query.from_user.id):
        await callback_query.answer("Недостаточно прав", show_alert=True)
        return

    parts = callback_query.data.split(":")
    # diag:<action>:<kind>:<id>[:<hours>]
    if len(parts) < 4:
        await callback_query.answer()
        return
    action, kind, target_id = parts[1], parts[2], parts[3]

    body: dict | None = None
    if action == "mute":
        hours = parts[4] if len(parts) > 4 else "0"
        try:
            body = {"hours": int(hours)}
        except ValueError:
            await callback_query.answer()
            return
    elif action == "follow":
        body = {"mode": "exponential"}
    elif action != "ack":
        await callback_query.answer()
        return

    try:
        status_code, _ = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/diagnostics/{kind}/{target_id}/{action}",
            json=body,
            headers=_admin_headers(callback_query.from_user.id),
        )
    except aiohttp.ClientError:
        await callback_query.answer("Бэкенд недоступен", show_alert=True)
        return

    if status_code != 200:
        await callback_query.answer("Не удалось применить", show_alert=True)
        return

    # Человекочитаемый toast + строка в сообщение, какое действие применено.
    if action == "ack":
        toast = "Принято: вижу, работаю 👀"
        applied = "✅ Вижу, работаю"
    elif action == "follow":
        toast = "Слежу по экспоненте 📈"
        applied = "✅ Слежу (экспонента)"
    else:  # mute
        h = body["hours"]
        if h < 0:
            toast = "Заглушено совсем 🔕"
            applied = "✅ Заглушено совсем"
        elif h == 0:
            toast = "Звук включён 🔔"
            applied = "✅ Звук включён"
        else:
            toast = f"Заглушено на {h}ч 🔕"
            applied = f"✅ Заглушено на {h}ч"

    await callback_query.answer(toast)
    # Дроп клавиатуры + добавляем строку «что сделали», чтобы было видно
    # в истории чата. edit_text best-effort — текст пуша приходит из
    # бэкенда уже как HTML; если редактирование упало (сообщение слишком
    # старое и т.п.), хотя бы убираем кнопки.
    try:
        original = callback_query.message.html_text or callback_query.message.text or ""
        await callback_query.message.edit_text(
            f"{original}\n\n{applied}",
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


# ── /settings — notification preferences ──

def _notif_prefs_keyboard(prefs: dict) -> types.InlineKeyboardMarkup:
    """Build inline keyboard reflecting current notification preferences."""
    renewals_on = prefs.get("notify_renewals", True)
    migrations_on = prefs.get("notify_migrations", True)
    health_off = prefs.get("health_ping_opt_out", False)

    def _toggle(label: str, on: bool, cb: str) -> types.InlineKeyboardButton:
        icon = "✅" if on else "❌"
        return types.InlineKeyboardButton(text=f"{icon} {label}", callback_data=cb)

    return types.InlineKeyboardMarkup(inline_keyboard=[
        [_toggle("Напоминания о продлении", renewals_on, "nprefs:renewals")],
        [_toggle("Уведомления о миграции", migrations_on, "nprefs:migrations")],
        [_toggle("Опрос о качестве", not health_off, "nprefs:health")],
    ])


@router.message(Command("settings"))
async def cmd_settings(message: types.Message):
    """Show notification preferences with toggle buttons."""
    tid = str(message.from_user.id)
    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/users/notification-prefs?telegram_id={tid}",
            headers=_admin_headers(message.from_user.id),
        )
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен.")
        return

    if status_code == 404:
        await message.answer("У вас ещё нет аккаунта — нажмите /start.")
        return
    if status_code != 200:
        await message.answer("Не удалось загрузить настройки.")
        return

    await message.answer(
        "⚙️ <b>Настройки уведомлений</b>\n\n"
        "Нажмите, чтобы включить или выключить:",
        reply_markup=_notif_prefs_keyboard(data),
    )


@router.callback_query(F.data.startswith("nprefs:"))
async def toggle_notification_pref(callback_query: types.CallbackQuery):
    """Toggle a single notification preference and refresh the keyboard."""
    tid = str(callback_query.from_user.id)
    key = callback_query.data.split(":")[1]

    # First, fetch current prefs
    try:
        status_code, prefs = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/users/notification-prefs?telegram_id={tid}",
            headers=_admin_headers(callback_query.from_user.id),
        )
    except aiohttp.ClientError:
        await callback_query.answer("Бэкенд недоступен", show_alert=True)
        return
    if status_code != 200:
        await callback_query.answer("Ошибка", show_alert=True)
        return

    # Toggle the requested field
    payload: dict = {"telegram_id": tid}
    if key == "renewals":
        payload["notify_renewals"] = not prefs.get("notify_renewals", True)
    elif key == "migrations":
        payload["notify_migrations"] = not prefs.get("notify_migrations", True)
    elif key == "health":
        payload["health_ping_opt_out"] = not prefs.get("health_ping_opt_out", False)
    else:
        await callback_query.answer()
        return

    try:
        status_code, updated = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/users/notification-prefs",
            json=payload,
            headers=_admin_headers(callback_query.from_user.id),
        )
    except aiohttp.ClientError:
        await callback_query.answer("Бэкенд недоступен", show_alert=True)
        return
    if status_code != 200:
        await callback_query.answer("Не удалось сохранить", show_alert=True)
        return

    await callback_query.answer("Сохранено ✓")
    try:
        await callback_query.message.edit_reply_markup(
            reply_markup=_notif_prefs_keyboard(updated),
        )
    except Exception:
        pass


# ── /balance — текущий баланс и runway ──

@router.message(F.text == BTN_TOPUP)
@router.message(F.text == "Баланс")
@router.message(Command("balance"))
async def cmd_balance(message: types.Message):
    """Show balance + days remaining + a deep-link into the WebApp.

    Stage 4: this is the primary "how much money do I have" surface
    outside the WebApp. Topup happens through the WebApp because Stars
    invoice flow is much smoother there than via inline buttons.
    """
    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/users/by_telegram/{message.from_user.id}/balance",
            headers=_admin_headers(message.from_user.id),
        )
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен.")
        return

    if status_code == 404:
        await message.answer(
            "У вас ещё нет аккаунта — нажмите /start, потом выберите тариф."
        )
        return
    if status_code != 200 or not data:
        await message.answer("Не удалось получить баланс. Попробуйте позже.")
        return

    balance_rub = data.get("balance_rub", 0)
    min_days = data.get("min_days_remaining")
    subs = data.get("subscriptions", [])

    lines = [f"💰 <b>Баланс: {balance_rub:.0f} ₽</b>"]
    if min_days is not None:
        if min_days <= 3:
            lines.append(f"⚠️ Подписка истекает через <b>{min_days} дн.</b> — пора пополнить!")
        else:
            lines.append(f"Подписка активна ещё <b>{min_days} дн.</b>")
    lines.append("")

    if subs:
        lines.append("<b>Активные подписки:</b>")
        for s in subs:
            price = s.get("plan_price_kopecks")
            expires_at = s.get("expires_at")
            auto_renew = s.get("auto_renew", True)
            status = s.get("status")
            badge = "🟢" if status == "active" else "❄️" if status == "frozen" else "•"
            price_str = f"{price / 100:.0f} ₽/мес" if price else "—"
            if expires_at and status == "active":
                from datetime import datetime as _dt
                try:
                    exp = _dt.fromisoformat(expires_at)
                    exp_str = f" · до {exp.strftime('%d.%m')}"
                except (ValueError, TypeError):
                    exp_str = ""
            else:
                exp_str = ""
            renew_str = " · автопродление" if auto_renew else ""
            lines.append(f"{badge} {s['plan_name']} — {price_str}{exp_str}{renew_str}")
    else:
        lines.append("Нет активных подписок. Нажмите /plans, чтобы выбрать тариф.")

    webapp_kb = webapp_inline_keyboard()
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=webapp_kb,
    )


# ── /referral — реферальная ссылка ──

@router.message(F.text == BTN_INVITE)
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

    # Stage 4: реферал теперь через денежный бонус, не через дни.
    # Сумма берётся из бэкенда (REFERRAL_BONUS_KOPECKS, дефолт 50 ₽).
    await message.answer(
        f"🎁 <b>Твоя реферальная ссылка:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"Приглашённый получает <b>+50 ₽ на баланс</b>, ты — <b>+50 ₽</b>.\n"
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

    # Notify user about the payment result
    user_id = invoice.get("user_telegram_id")
    if not user_id:
        return

    kind = invoice.get("kind", "subscription")
    action = invoice.get("action", "")
    credentials = invoice.get("credentials", [])

    if kind == "topup":
        amount = invoice.get("amount", 0)
        await callback_query.message.bot.send_message(
            chat_id=user_id,
            text=f"✅ Баланс пополнен на {amount:.0f} ₽",
        )
    elif action == "renewal":
        await callback_query.message.bot.send_message(
            chat_id=user_id,
            text="✅ Подписка продлена!",
        )
    elif credentials:
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
    else:
        await callback_query.message.bot.send_message(
            chat_id=user_id,
            text="✅ Счет оплачен! Конфиг будет готов через минуту. Используй /config.",
        )


# ── /help — support and FAQ ──

_HELP_INTRO = (
    "🛟 Чем помочь?\n\n"
    "Выбери раздел ниже — там пошаговые инструкции. "
    "Если ничего не помогло, напиши в поддержку, мы отвечаем живыми людьми."
)

_HELP_CABINET = (
    "🔐 Кабинет не открывается — попробуй по порядку:\n\n"
    "👉 Если ты на Wi-Fi — выключи его и зайди через мобильный интернет (или наоборот)\n"
    "👉 Попробуй открыть кабинет с включённым VPN — иногда провайдер режет наш домен\n"
    "👉 Длинное нажатие на кнопку «Открыть личный кабинет» → «Открыть в браузере» — откроется в Chrome/Safari\n"
    "👉 Обнови страницу (меню браузера → ⟳)\n"
    "👉 Полностью закрой Telegram и открой заново\n\n"
    "Если не помогло — жми «Связаться с поддержкой» внизу."
)

_HELP_VPN = (
    "🌐 VPN не подключается или тормозит — чек-лист:\n\n"
    "👉 Обнови подписку в VPN-клиенте — нажми кнопку 🔄 рядом с профилем, "
    "чтобы подтянуть актуальный конфиг\n"
    "👉 Проверь баланс в личном кабинете — при 0 ₽ доступ блокируется\n"
    "👉 Обнови клиент (Hiddify / v2rayNG / Streisand) до последней версии в сторе\n"
    "👉 В настройках устройства выключи другие VPN-профили — два VPN одновременно работать не будут\n"
    "👉 Перезагрузи телефон\n"
    "👉 Проверь, что у тебя активна подписка (в кабинете — раздел «Мои подписки»)\n\n"
    "Если прошёл все шаги и всё ещё не работает — жми «Связаться с поддержкой», "
    "приложи скриншот клиента и модель телефона."
)


@router.message(F.text == BTN_HELP)
@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(_HELP_INTRO, reply_markup=help_keyboard())


@router.callback_query(F.data == "help:cabinet")
async def help_cabinet(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.answer(
        _HELP_CABINET, reply_markup=help_back_keyboard()
    )


@router.callback_query(F.data == "help:vpn")
async def help_vpn(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.answer(
        _HELP_VPN, reply_markup=help_back_keyboard()
    )


# help:support handled by support_router in bot/support.py — it owns
# the FSM state machine for the forward-to-admin / reply-back flow.


@router.callback_query(F.data == "help:back")
async def help_back(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.answer(_HELP_INTRO, reply_markup=help_keyboard())


# ── Inline shortcuts from the /start welcome action keyboard ──

@router.callback_query(F.data == "go:help")
async def go_help(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.answer(_HELP_INTRO, reply_markup=help_keyboard())


@router.callback_query(F.data == "go:start")
async def go_start(callback_query: types.CallbackQuery):
    """«🏠 Главное меню» из любого inline-меню — шорткат на /start."""
    await callback_query.answer()
    first_name = callback_query.from_user.first_name or "друг"
    welcome = format_welcome(first_name, is_new=False, trial_available=False)
    await callback_query.message.answer(welcome, reply_markup=welcome_action_keyboard())
    await callback_query.message.answer(
        "⌨️ Кнопки внизу всегда под рукой.",
        reply_markup=start_keyboard(),
    )


@router.callback_query(F.data == "go:plans")
async def go_plans(callback_query: types.CallbackQuery):
    """Inline shortcut to /plans."""
    await callback_query.answer()
    await list_plans(callback_query.message)


@router.callback_query(F.data == "go:topup")
async def go_topup(callback_query: types.CallbackQuery):
    """Inline shortcut to /balance (shows balance + topup link)."""
    await callback_query.answer()
    # Reuse cmd_balance but with the correct from_user
    msg = callback_query.message
    msg.from_user = callback_query.from_user
    await cmd_balance(msg)


@router.callback_query(F.data == "go:referral")
async def go_referral(callback_query: types.CallbackQuery):
    """Run the /referral flow from an inline button on the welcome msg.

    cmd_referral reads message.from_user; from a callback that'd be the
    bot itself, so we re-implement the same 3-line backend call here
    against callback_query.from_user instead of refactoring the command.
    """
    await callback_query.answer()
    user = callback_query.from_user
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/referral/code",
            json={"telegram_id": str(user.id)},
            headers=_admin_headers(user.id),
        )
    except aiohttp.ClientError:
        await callback_query.message.answer("Бэкенд недоступен.")
        return
    if status_code != 200 or not data:
        await callback_query.message.answer("Не удалось получить реферальную ссылку.")
        return
    code = data.get("code", "")
    uses = data.get("uses", 0)
    bot_info = await callback_query.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{code}"
    await callback_query.message.answer(
        f"🎁 <b>Твоя реферальная ссылка:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"Приглашённый получает <b>+50 ₽ на баланс</b>, ты — <b>+50 ₽</b>.\n"
        f"Приглашено: {uses} чел.",
        parse_mode="HTML",
    )
