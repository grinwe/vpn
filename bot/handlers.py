import asyncio
import hashlib
import html
import logging
import os
import re
from urllib.parse import urlparse

import aiohttp
from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext

from .config import (
    ADMIN_API_TOKEN,
    ADMIN_IDS,
    BACKEND_URL,
    PAYMENT_PROVIDER,
    PAYMENT_PROVIDER_CHOICES,
    SUB_LINK_ALT_SHARE,
    SUB_LINK_BASE_URL,
    SUB_LINK_BASE_URL_ALT,
    TELEGRAM_STARS_WEBHOOK_SECRET,
)
from .keyboards import (
    WEBAPP_BASE_URL,
    WEBAPP_URL,
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

# Stage 9b — человекочитаемые подписи кнопок выбора способа оплаты.
# Неизвестное имя провайдера показывается как есть (кнопка всё равно
# работает: имя уходит в checkout verbatim).
_PROVIDER_LABELS = {
    "telegram_stars": "⭐ Telegram Stars",
    "stars": "⭐ Telegram Stars",
    "lava_top": "💳 Карта РФ / СБП",
    "tribute": "💳 Карта (Tribute)",
    "cryptobot": "🪙 Крипта (USDT)",
}


def _provider_label(name: str) -> str:
    return _PROVIDER_LABELS.get(name, name)


def _payment_method_rows(invoice_id: int, kind: str) -> list[list[types.InlineKeyboardButton]]:
    """Ряды inline-кнопок выбора способа оплаты (kind: new|ren)."""
    return [
        [
            types.InlineKeyboardButton(
                text=_provider_label(p),
                callback_data=f"payvia:{kind}:{invoice_id}:{p}",
            )
        ]
        for p in PAYMENT_PROVIDER_CHOICES
    ]


def _sub_base_for(sub_token: str) -> str:
    """Домен саб-ссылки для этого токена: основной или запасной.

    Зеркало ``SUB_LINK_BASE_URL_ALT`` идёт мимо Cloudflare, прямо на origin,
    и открывается там, где основной фронт лёг. Раскладываем токены по двум
    доменам, чтобы падение одного уносило часть людей, а не всех.

    Копия ``backend/app/services/sub_links.use_alt`` — там же и объяснение,
    почему sha256, а не встроенный hash(). Считают независимо бот, бэкенд и
    провижининг, поэтому расхождение недопустимо: держи обе реализации в
    одном виде (тест ``test_sub_link_balance.py`` сверяет их между собой).
    """
    primary = (SUB_LINK_BASE_URL or "").strip().rstrip("/")
    alt = (SUB_LINK_BASE_URL_ALT or "").strip().rstrip("/")
    try:
        share = max(0, min(100, int((SUB_LINK_ALT_SHARE or "0").strip())))
    except ValueError:
        share = 0
    if not alt or share <= 0:
        return primary
    if share >= 100:
        return alt
    digest = hashlib.sha256(sub_token.encode("utf-8")).hexdigest()
    return alt if int(digest[:8], 16) % 100 < share else primary


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
    base = _sub_base_for(sub_token)
    if base:
        return f"{base}/{sub_token}"
    webapp_base = os.getenv("WEBAPP_BASE_URL", "")
    if webapp_base:
        parsed = urlparse(webapp_base)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/api/sub/{sub_token}"
    return None
router = Router()
logger = logging.getLogger(__name__)

_SESSION: aiohttp.ClientSession | None = None
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
_RETRIES = 2

# Короткий per-attempt таймаут для ретраящихся запросов (по умолчанию — GET).
# Без него 3 попытки по total=10с давали пользователю ~30с тишины при лежащем
# бэкенде. Плюс общий дедлайн на всю _fetch_json, чтобы суммарное ожидание было
# ограничено ~10-12с, а не множилось на число ретраев.
_RETRY_ATTEMPT_TIMEOUT_S = float(os.getenv("BOT_RETRY_ATTEMPT_TIMEOUT_S", "4"))
_RETRY_CONNECT_TIMEOUT_S = float(os.getenv("BOT_RETRY_CONNECT_TIMEOUT_S", "2"))
_FETCH_DEADLINE_S = float(os.getenv("BOT_FETCH_DEADLINE_S", "12"))


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


async def _fetch_json(method: str, url: str, *, retry: bool | None = None, **kwargs):
    """Call backend with retries and a shared session.

    retry управляет повторами (сеть/таймаут/5xx). По умолчанию (retry=None)
    повторяем ТОЛЬКО идемпотентные GET: повтор POST после обрыва соединения,
    случившегося уже ПОСЛЕ того как бэкенд принял запрос, породил бы дубль
    (второй счёт, повторная миграция устройства). Для заведомо идемпотентного
    POST можно явно передать retry=True.
    """
    session = await get_session()
    is_get = method.upper() == "GET"
    do_retry = is_get if retry is None else retry
    last_exc: Exception | None = None
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _FETCH_DEADLINE_S
    # Per-attempt таймаут. Если caller передал timeout явно (напр. /start даёт
    # register 3с) — уважаем его. Иначе для идемпотентного GET ставим короткий
    # таймаут, чтобы N ретраев не растянулись на ~30с. Не-GET без явного timeout
    # оставляем на общем _HTTP_TIMEOUT сессии (одна попытка, дублей не плодим).
    attempt_timeout = kwargs.pop("timeout", None)
    if attempt_timeout is None and is_get:
        attempt_timeout = aiohttp.ClientTimeout(
            total=_RETRY_ATTEMPT_TIMEOUT_S, connect=_RETRY_CONNECT_TIMEOUT_S
        )
    for attempt in range(_RETRIES + 1):
        # Общий дедлайн исчерпан — не начинаем новую попытку (быстрый честный
        # ответ «недоступно» вместо трёх полных таймаутов подряд).
        if attempt > 0 and loop.time() >= deadline:
            break
        try:
            if attempt_timeout is not None:
                kwargs["timeout"] = attempt_timeout
            async with session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                try:
                    payload = await resp.json()
                except Exception:
                    payload = {"message": text}
                if resp.status >= 500 and do_retry and attempt < _RETRIES:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                # Финальный 5xx логируем — иначе деградация бэкенда невидима
                # в логах бота (вызыватели часто показывают юзеру общий текст).
                if resp.status >= 500:
                    logger.warning(
                        "backend %s %s -> %s: %.200s", method, url, resp.status, text
                    )
                return resp.status, payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # asyncio.TimeoutError — общий ClientTimeout(total=...) aiohttp;
            # он НЕ подкласс ClientError, но должен идти тем же путём
            # «ретрай → status=0», иначе таймаут бэкенда роняет хендлер.
            last_exc = exc
            if do_retry and attempt < _RETRIES:
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            break
    logger.warning("backend request failed: %s %s: %s", method, url, last_exc)
    return 0, {"message": f"backend unreachable: {last_exc}"}


# ── Фоновые задачи с сильной ссылкой ──
# asyncio держит незакреплённую задачу лишь по weak-ref: fire-and-forget
# create_task может быть собран GC до завершения (особенно долгий 15-мин нудж).
# Кладём в модульный set и снимаем по завершении — держим сильную ссылку.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


# /start шлёт register best-effort и НЕ должен держать приветствие: даём ему
# короткий таймаут, чтобы новый юзер (часто с рекламы — первое впечатление) не
# смотрел до 10с в пустоту при подтормаживающем бэкенде.
_REGISTER_TIMEOUT_S = float(os.getenv("BOT_REGISTER_TIMEOUT_S", "3"))


async def _register_retry_bg(user_id: int, payload: dict) -> None:
    """Фоновая до-регистрация, если /start не дождался быстрого register.

    register = get-or-create (повтор безопасен). Цель — не потерять рекламную
    метку/реферал (source/referral_code) при медленном бэкенде: быстрый 3с
    таймаут мог оборваться раньше, чем метка доехала."""
    try:
        await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/users/register",
            json=payload,
            headers=_admin_headers(user_id),
        )
    except Exception:  # noqa: BLE001
        pass


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

# ── /ops debounce ──
# /ops запускает полный агентный прогон Claude (дорого, ~до минуты). Кулдаун на
# юзера — анти-даблтап/анти-спам; настоящий потолок держит бэкенд (рейт-лимит по
# X-Admin-Actor + семафор), это лишь первый барьер.
_OPS_COOLDOWN_S = 20
_ops_last: dict[int, float] = {}


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

_TRIAL_LINE = (
    "🎁 Первый месяц — бесплатно, карта не нужна.\n"
    "Один тап в кабинете: получишь ссылку и инструкцию, как подключиться.\n\n"
)


async def _fetch_user_flags(telegram_id: int) -> tuple[bool, bool]:
    """``(trial_available, has_devices)`` для юзера — общий хелпер входных экранов.

    Нужен там, где приветствие/тарифы рисуются НЕ из ``/start`` (главное меню,
    список тарифов): раньше эти экраны хардкодили ``trial_available=False`` и
    подарок из них пропадал. Регистрация идемпотентна, поэтому переиспользуем
    её же эндпоинт вместо отдельного read-API.

    Fail-safe в сторону новичка (как в ``cmd_start``): не достучались — считаем,
    что подарок ещё доступен. Показать оффер лишний раз безопасно, бэкенд
    валидирует его при активации; не показать — потерять юзера.
    """
    try:
        _status, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/users/register",
            json={"telegram_id": str(telegram_id)},
            headers=_admin_headers(telegram_id),
            timeout=aiohttp.ClientTimeout(total=_REGISTER_TIMEOUT_S),
        )
        if data:
            return bool(data.get("trial_available")), bool(data.get("has_devices"))
    except Exception:  # noqa: BLE001
        logger.warning("_fetch_user_flags: бэкенд недоступен, показываем оффер")
    return True, False


def format_welcome(name: str, is_new: bool, trial_available: bool) -> str:
    """Приветствие. Тексты согласованы в роадмапе (E1.1/E1.1a).

    Описание продукта показываем и новичку, и тому, кто ещё не забрал подарок:
    человек, вернувшийся через месяц, продукт всё равно не помнит, а «Рад снова
    видеть» без единого аргумента — пустой экран.
    """
    if is_new or trial_available:
        body = (
            f"👋 Привет, {name}!\n\n"
            "🚀 Быстрый VPN без танцев с настройками:\n"
            "• Работает там, где другие отвалились — обход блокировок на уровне протокола\n"
            "• Оплата прямо в Telegram, без карт и регистраций\n"
            # E1.1a: было «Один тариф — до 5 устройств одновременно», а в проде
            # шесть тарифов и 5 устройств только у Pro. Юзер читал «плачу один
            # раз — получаю 5», потом видел прайс и терял доверие ровно на шаге
            # принятия решения.
            "• До 5 устройств одновременно — на тарифе Pro\n"
            "• Поддержка отвечает в чате, не роботом\n\n"
        )
    else:
        body = f"👋 Рад снова видеть, {name}!\n\n"
    if trial_available:
        body += _TRIAL_LINE
        return body.rstrip("\n")
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

    # Deep-link из VPN-клиента: кнопка «Продлить» в блоке статуса подписки
    # (Happ рисует его по sub-info-*, ссылка ведёт сюда). Человек уже знает,
    # чего хочет, — показываем тарифы сразу, без приветствия.
    if len(args) > 1 and args[1].strip() == "renew":
        await list_plans(message)
        return

    # Тот же deep-link, но в личный кабинет: кнопки «Продлить» и Telegram в
    # VPN-клиенте ведут сюда, когда прямой ссылки на мини-апп нет (короткое
    # имя приложения задаётся в BotFather). Открыть WebApp из внешнего
    # браузера нельзя — только кнопкой изнутри Telegram, поэтому её и даём.
    if len(args) > 1 and args[1].strip() == "account":
        if WEBAPP_BASE_URL.startswith("https://"):
            await message.answer(
                "Продление и всё по подписке — в личном кабинете.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="🔐 Открыть личный кабинет",
                        web_app=types.WebAppInfo(url=WEBAPP_URL),
                    )
                ]]),
            )
        else:
            await list_plans(message)
        return

    # Deep-link старт-параметр: ``ref_<code>`` — реферал человека (referred_by_id);
    # любой другой непустой payload — рекламная метка (source, first-touch).
    # Бэкенд валидирует/чистит метку, так что прокидываем сыро.
    referral_code = None
    source = None
    if len(args) > 1 and args[1].strip():
        payload = args[1].strip()
        if payload.startswith("ref_"):
            referral_code = payload[4:]
        else:
            source = payload

    # Register user (and apply referral if present). Backend returns
    # {"created": bool} so we can pick a new-vs-returning welcome copy.
    # Failures are still best-effort — worst case we greet them as
    # returning; they'll be created on first purchase anyway.
    register_payload = {"telegram_id": str(message.from_user.id)}
    if referral_code:
        register_payload["referral_code"] = referral_code
    if source:
        register_payload["source"] = source

    # E1.2 — fail-safe в сторону НОВИЧКА. Регистрация синхронная с таймаутом
    # 3 с и без ретраев; при любом сбое флаги оставались False, и человек с
    # рекламы видел «Рад снова видеть» без описания продукта и без подарка —
    # он не мог знать, что это сбой, и уходил. Дефолты трактуем как «скорее
    # всего новый»: показать подарок лишний раз безопасно (бэкенд всё равно
    # валидирует его при активации), а не показать — потерять юзера.
    is_new = True
    trial_available = True
    has_devices = False
    try:
        _status, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/users/register",
            json=register_payload,
            headers=_admin_headers(message.from_user.id),
            timeout=aiohttp.ClientTimeout(total=_REGISTER_TIMEOUT_S),
        )
        if data:
            is_new = bool(data.get("created"))
            trial_available = bool(data.get("trial_available"))
            has_devices = bool(data.get("has_devices"))
        # Быстрый таймаут мог оборваться раньше, чем метка/реферал доехали до
        # бэка. Если не достучались, но атрибуция была — до-регистрируем в фоне
        # полным таймаутом, чтобы не потерять источник конверсии.
        if _status == 0 and (referral_code or source):
            _spawn(_register_retry_bg(message.from_user.id, register_payload))
    except Exception:
        logger.warning("cmd_start: register failed, показываем welcome как новичку")

    first_name = message.from_user.first_name or "друг"
    welcome = format_welcome(first_name, is_new, trial_available)

    # Telegram allows only one reply_markup per message, so we send two:
    #   1) Welcome + inline action keyboard (WebApp button + quick actions)
    #   2) Tiny nudge + persistent reply keyboard (always at the bottom,
    #      for both new and returning users — returning users complained
    #      the bottom buttons disappeared).
    await message.answer(
        welcome,
        reply_markup=welcome_action_keyboard(
            trial_available=trial_available, is_new=is_new
        ),
    )
    # E1.5: новичку не сообщаем про поломки до того, как он что-то получил —
    # «если что-то сломалось» на первом экране читается как «тут всё ломается».
    hint = "⌨️ Кнопки внизу всегда под рукой."
    await message.answer(hint, reply_markup=start_keyboard(has_devices=has_devices))


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

    # E1.4 — оффер ПЕРЕД прайсом. «💎 Подписка» продублирована в инлайн- и
    # нижней клавиатуре, поэтому на прайс попадает половина новичков — и до
    # этого фикса видела просьбу заплатить без единого упоминания подарка,
    # который ей уже пообещали на первом экране.
    trial_available, _has_devices = await _fetch_user_flags(message.from_user.id)

    lines: list[str] = []
    if trial_available:
        lines += [
            "🎁 <b>Сначала — бесплатный месяц.</b>",
            "Он уже ждёт в личном кабинете: один тап, карта не нужна.",
            "",
            "Ниже — тарифы, если захочешь больше устройств или сразу на год.",
            "",
        ]
    lines += [
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
    if trial_available and WEBAPP_BASE_URL.startswith("https://"):
        # Кнопка подарка ПЕРВОЙ строкой клавиатуры — иначе оффер остаётся
        # текстом, а тапабельны только платные варианты.
        rows.insert(0, [types.InlineKeyboardButton(
            text="🎁 Забрать бесплатный месяц",
            web_app=types.WebAppInfo(url=WEBAPP_URL),
        )])
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

    # Stage 9b: при нескольких настроенных способах оплаты сначала даём
    # выбор — checkout произойдёт в payvia-callback'е выбранным провайдером.
    if len(PAYMENT_PROVIDER_CHOICES) >= 2:
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=_payment_method_rows(invoice["id"], "new")
        )
        await callback_query.message.answer(
            f"Счёт #{invoice['id']} на {invoice.get('amount')} {invoice.get('currency', '')}.\n"
            "Выберите способ оплаты ⬇️ Конфиг придёт автоматически после "
            "подтверждения оплаты.",
            reply_markup=keyboard,
        )
        await callback_query.answer()
        return

    try:
        co_status, checkout = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/invoices/{invoice['id']}/checkout",
            json={
                "provider": PAYMENT_PROVIDER,
                "telegram_id": str(callback_query.from_user.id),
            },
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


@router.callback_query(F.data.startswith("payvia:"))
async def choose_payment_method(callback_query: types.CallbackQuery):
    """Stage 9b: юзер выбрал способ оплаты — делаем checkout этим провайдером.

    Кнопки способов остаются в клавиатуре: если оплата одним способом не
    прошла (антифрод карточного агрегатора и т.п.), юзер выбирает другой —
    каждый выбор создаёт свою Payment-строку, вебхук пометит оплаченную
    (#117). Заменяется только pay-кнопка (единственная с url).
    """
    try:
        _, kind, invoice_id_raw, provider = callback_query.data.split(":", maxsplit=3)
        invoice_id = int(invoice_id_raw)
    except ValueError:
        await callback_query.answer("Некорректный запрос", show_alert=True)
        return

    # callback_data подделываема кастомным клиентом (Telegram не сверяет её
    # с реальной клавиатурой): принимаем только провайдера из настроенного
    # меню, а бэкенд дополнительно сверяет владельца счёта по telegram_id.
    if provider not in PAYMENT_PROVIDER_CHOICES:
        await callback_query.answer("Неизвестный способ оплаты", show_alert=True)
        return

    try:
        co_status, checkout = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/invoices/{invoice_id}/checkout",
            json={
                "provider": provider,
                "telegram_id": str(callback_query.from_user.id),
            },
        )
    except aiohttp.ClientError:
        co_status, checkout = 0, None

    pay_url = (checkout or {}).get("pay_url")
    if co_status != 200 or not pay_url:
        await callback_query.answer(
            "Этот способ временно недоступен — попробуйте другой.", show_alert=True
        )
        return

    btn_text = "Оплатить продление" if kind == "ren" else "Оплатить"
    pay_row = [
        types.InlineKeyboardButton(
            text=f"{btn_text} · {_provider_label(provider)}", url=pay_url
        )
    ]
    link_text = f"Ссылка на оплату ({_provider_label(provider)}):\n{pay_url}"

    msg = callback_query.message
    # Сообщение старше 48ч приходит как InaccessibleMessage (нет
    # reply_markup / edit_reply_markup) — редактировать нельзя. Шлём
    # ссылку новым сообщением в тот же чат (в ЛС chat.id == from_user.id).
    if not isinstance(msg, types.Message):
        chat_id = getattr(getattr(msg, "chat", None), "id", None) or callback_query.from_user.id
        await callback_query.bot.send_message(
            chat_id=chat_id,
            text=link_text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[pay_row]),
        )
        await callback_query.answer()
        return

    old_rows = msg.reply_markup.inline_keyboard if msg.reply_markup else []
    kept = [row for row in old_rows if not any(btn.url for btn in row)]
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[pay_row] + kept)
    try:
        await msg.edit_reply_markup(reply_markup=keyboard)
    except TelegramBadRequest:
        # message is not modified (повторный тап тем же способом) либо
        # сообщение слишком старое для edit — шлём ссылку новым сообщением.
        await msg.answer(link_text)
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
        # retry=True: апдейт Telegram consumed единожды, юзер не может повторить
        # тап — держим ретраи, чтобы пережить короткий 5xx/рестарт бэка. Форвард
        # идемпотентен (бэкенд дедупит по telegram_payment_charge_id).
        status, _ = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/payments/webhook/telegram_stars",
            json=forward,
            headers={"X-Telegram-Stars-Secret": TELEGRAM_STARS_WEBHOOK_SECRET},
            retry=True,
        )
    except aiohttp.ClientError:
        status = 0

    if status != 200:
        # Апдейт Telegram consumed один раз, очереди нет — логируем ПОЛНЫЙ
        # payload (в т.ч. telegram_payment_charge_id) на ERROR, чтобы платёж
        # можно было восстановить вручную из логов бота, а не потерять.
        logger.error(
            "failed to forward Stars payment (status=%s) payload=%s", status, forward
        )
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
        # status_code==0 → бэкенд недоступен (_fetch_json уже съел ClientError).
        # Не путаем это с «нет подписок»: иначе во время деплоя платящий юзер
        # видит ложное «у тебя нет подписок».
        if status_code == 0:
            await message.answer("Сервис временно недоступен. Попробуй позже.")
            return
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
    # E3.2 — момент первого подключения единственный, где эта подсказка нужна:
    # кнопка «🆘 VPN не работает» реально переносит устройство на другой сервер,
    # но юзер об этом не знает и при первой неудаче просто уходит.
    text += (
        "\n\n<i>Не подключается? Нажми «🆘 VPN не работает» — перенесём "
        "тебя на другой сервер.</i>"
    )
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
        if status_code == 0:
            await message.answer("Сервис временно недоступен. Попробуй позже.")
            return
        if status_code != 200:
            await message.answer("Подписок не найдено. Используй /plans для покупки.")
            return
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
        return

    lines = ["📊 <b>Твои подписки:</b>\n"]
    esc = html.escape
    for sub in data:
        auto_renew = "✅" if sub.get("auto_renew") else "❌"
        # Имена нод/плана/региона приходят с бэкенда — экранируем перед HTML.
        node = sub.get("node") or sub.get("server", "n/a")
        lines.append(
            f"<b>План:</b> {esc(str(sub['plan_name']))}\n"
            f"<b>Сервер:</b> {esc(str(node))} ({esc(str(sub.get('region', '??')))})\n"
            f"<b>Истекает:</b> {esc(str(sub['expires_at'][:10]))}\n"
            f"<b>Статус:</b> {esc(str(sub['status']))}\n"
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
        if status_code == 0:
            await message.answer("Сервис временно недоступен. Попробуй позже.")
            return
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

    # Stage 9b: несколько способов оплаты — сначала выбор, checkout в
    # payvia-callback'е. Кнопка автопродления остаётся в той же клавиатуре
    # (payvia сохраняет callback-кнопки при подстановке pay-ссылки).
    if len(PAYMENT_PROVIDER_CHOICES) >= 2:
        rows = _payment_method_rows(invoice["id"], "ren")
        rows.append(
            [types.InlineKeyboardButton(
                text="Включить автопродление",
                callback_data=f"auto_renew:{sub['id']}",
            )]
        )
        await message.answer(
            f"💳 Продление подписки #{sub['id']}\n"
            f"Сумма: {invoice.get('amount')} {invoice.get('currency', '')}\n\n"
            "Выберите способ оплаты ⬇️ Или включите автопродление.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return

    try:
        co_status, checkout = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/invoices/{invoice['id']}/checkout",
            json={
                "provider": PAYMENT_PROVIDER,
                "telegram_id": str(message.from_user.id),
            },
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
        _spawn(
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
    # Operator-aware routing P1 + per-device picker: тянем живые устройства
    # юзера. >1 → спрашиваем, КАКОЕ перенести (рабочие устройства не трогаем);
    # ==1 → переносим сразу (один тап, как раньше). Через делей
    # (_STILL_BROKEN_DELAY_S), если бэкенд НЕ видит переподключения, шлём нудж
    # «всё ещё не работает?» → поддержка. Исход для операторской матрицы
    # проставляет backend-watcher (resolve_pending_reports).
    tg_id = message.from_user.id
    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/admin/client-control/devices-by-telegram"
            f"?telegram_id={tg_id}",
            headers=_admin_headers(tg_id),
        )
    except aiohttp.ClientError:
        await message.answer(
            "Не получилось обработать — попробуй ещё раз через минуту."
        )
        return
    if status_code != 200 or not isinstance(data, dict):
        # 4xx/5xx/0 здесь = сбой контракта или недоступность бэка. _fetch_json
        # логирует только >=500 и 0, поэтому 4xx (напр. протухший telegram_id,
        # рассинхрон) иначе исчезал бы без следа — логируем с контекстом.
        logger.warning(
            "self_report devices-by-telegram failed: tg_id=%s status=%s data=%.300s",
            tg_id, status_code, data,
        )
        await message.answer(
            "Не получилось обработать — попробуй ещё раз через минуту."
        )
        return

    devices = data.get("devices") or []
    # Троттл проверяем ДО пикера: бэкенд отклонит перенос, если предыдущий был
    # меньше 5 минут назад, и раньше юзер узнавал об этом только ПОСЛЕ выбора
    # устройства — бот сначала спрашивал «какое не работает?», а потом отвечал
    # «уже перекидывали недавно». Для человека это выглядит как противоречие
    # (репорт 2026-07-26).
    retry_after = data.get("retry_after_sec")
    if devices and retry_after:
        # Жалобу бэкенд уже зафиксировал сам (devices-by-telegram пишет
        # complaint_received, когда отвечает троттлом) — здесь только текст для
        # человека. Раньше на этом месте сигнал терялся совсем: бот молча
        # показывал «уже перенесли» и на сервер ничего не отправлял.
        mins = max(1, int(retry_after) // 60)
        await message.answer(
            f"👍 Мы уже перенесли тебя на другой сервер пару минут назад.\n\n"
            f"Подписка обновляется в приложении сама — нажми 🔄 рядом с профилем "
            f"и попробуй подключиться.\n\n"
            f"Если через {mins} мин. всё ещё не работает — жми /help, разберёмся.",
        )
        return

    if not devices:
        # E1.3: команды /buy в боте нет (ни хендлера, ни в списке команд) —
        # раньше человек, который уже жалуется на проблему, упирался в тупик.
        await message.answer(
            "У тебя нет активной подписки — чинить пока нечего.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(text="💎 Выбрать тариф", callback_data="go:plans"),
            ]]),
        )
        return

    if len(devices) > 1:
        # Несколько устройств — спрашиваем, какое перенести (имена = как у
        # юзера записаны). Показ пикера НЕ троттлим: выбор бесплатен, а саму
        # миграцию защищает проверка статуса устройства на бэке.
        await message.answer(
            "У тебя несколько устройств. Какое не работает? Перенесём только "
            "его на другой сервер — остальные не тронем.",
            reply_markup=broken_device_keyboard(devices),
        )
        return

    # Ровно одно устройство — переносим сразу, с прежним per-user cooldown
    # против спама кнопкой.
    now_mono = asyncio.get_event_loop().time()
    last = _self_report_last.get(tg_id)
    if last is not None and (now_mono - last) < _SELF_REPORT_COOLDOWN_S:
        await message.answer(
            "👍 Мы уже перекинули тебя на другой сервер недавно. Дай минуту "
            "переподключиться. Если через 10 минут не работает — /help."
        )
        return
    action = await _do_device_failover(
        message.bot, message.chat.id, tg_id, int(devices[0]["device_id"])
    )
    if action == "migrated":
        _self_report_last[tg_id] = now_mono


async def _do_device_failover(
    bot, chat_id: int, tg_id: int, device_id: int
) -> str | None:
    """Перенести ОДНО устройство на свободный сервер + спросить оператора.

    Зовёт per-device бэкенд (report-broken-device → failover_device): соседние
    устройства не трогаются, нода user-wide не банится. Возвращает action для
    вызывающего (single-device путь по нему ставит cooldown)."""
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/admin/client-control/report-broken-device",
            json={"telegram_id": str(tg_id), "device_id": device_id},
            headers=_admin_headers(tg_id),
        )
    except aiohttp.ClientError:
        await bot.send_message(
            chat_id, "Не получилось обработать — попробуй ещё раз через минуту."
        )
        return None
    if status_code != 200 or not isinstance(data, dict):
        # Персистентный 4xx (баг контракта/рассинхрон устройств) иначе гнал бы
        # юзера в бесконечный «попробуй через минуту» без единой строки в логах.
        logger.warning(
            "report-broken-device failed: tg_id=%s device_id=%s status=%s data=%.300s",
            tg_id, device_id, status_code, data,
        )
        await bot.send_message(
            chat_id, "Не получилось обработать — попробуй ещё раз через минуту."
        )
        return None

    action = data.get("action")
    if action == "migrated":
        report_id = data.get("report_id")
        # Имя устройства задаёт юзер в webapp — экранируем перед вставкой в
        # HTML-сообщение (default parse_mode=HTML), иначе '<' / несбалансированный
        # тег → 400 «can't parse entities», и юзер не получит ни клавиатуры, ни нуджа.
        dev_name = html.escape(data.get("device_name") or "устройство")
        await bot.send_message(
            chat_id,
            f"🔄 Поменяли сервер для «{dev_name}». Подписка обновится в "
            "приложении сама — нажми 🔄 рядом с профилем и попробуй подключиться "
            "через пару минут.\n\n"
            "Чтобы мы быстрее ловили блокировки — подскажи, какой у тебя интернет?",
            reply_markup=(
                operator_keyboard(int(report_id)) if report_id else help_keyboard()
            ),
        )
        if report_id:
            _spawn(
                _delayed_still_broken_prompt(bot, chat_id, int(report_id))
            )
    elif action == "reshuffled":
        # Первый шаг лестницы: ноды те же, протоколы другие. Человеку про
        # протоколы знать незачем — ему важно «мы поменяли способ связи и надо
        # обновить подписку».
        report_id = data.get("report_id")
        await bot.send_message(
            chat_id,
            "🔀 Переключили тебя на другой способ подключения — чаще всего "
            "не работает именно он, а не сам сервер.\n\n"
            "Нажми 🔄 рядом с профилем в приложении и попробуй подключиться.\n\n"
            "Чтобы мы быстрее ловили блокировки — подскажи, какой у тебя интернет?",
            reply_markup=(
                operator_keyboard(int(report_id)) if report_id else help_keyboard()
            ),
        )
        if report_id:
            _spawn(_delayed_still_broken_prompt(bot, chat_id, int(report_id)))
    elif action == "duplicated":
        # Третий шаг: человеку с единственным живым протоколом дали второй
        # сервер по нему же — страховка на случай падения его ноды.
        report_id = data.get("report_id")
        await bot.send_message(
            chat_id,
            "➕ Добавили тебе запасной сервер по тому способу связи, который у "
            "тебя работает.\n\n"
            "Нажми 🔄 рядом с профилем — в списке появится ещё один вариант.\n\n"
            "Если и после этого не заработает — жми /help, разберёмся руками.",
            reply_markup=(
                operator_keyboard(int(report_id)) if report_id else help_keyboard()
            ),
        )
        if report_id:
            _spawn(_delayed_still_broken_prompt(bot, chat_id, int(report_id)))
    elif action == "throttled":
        # Ветки не было: throttled проваливался в else и юзер получал «Не смогли
        # автоматически подобрать другой сервер. Напиши в поддержку» — то есть
        # штатный пятиминутный троттл выглядел как отказ сервиса и гнал человека в
        # поддержку (репорт 2026-07-26).
        await bot.send_message(
            chat_id,
            "👍 Мы уже перенесли тебя на другой сервер пару минут назад.\n\n"
            "Подписка обновляется в приложении сама — нажми 🔄 рядом с профилем "
            "и попробуй подключиться.\n\n"
            "Если через 10 минут всё ещё не работает — жми /help, разберёмся.",
        )
    elif action == "no_subscription":
        await bot.send_message(
            chat_id,
            "Это устройство уже перенесли или оно отключено. Обнови список "
            "устройств в приложении.",
        )
    else:  # no_target / user_not_found / прочее
        await bot.send_message(
            chat_id,
            "Не смогли автоматически подобрать другой сервер. Напиши в "
            "поддержку — разберёмся вручную.",
            reply_markup=help_keyboard(),
        )
    return action


async def _do_whole_sub_failover(bot, chat_id: int, tg_id: int) -> None:
    """«Все устройства» — старый whole-sub путь (report-broken): мигрирует всю
    подписку + бан старой ноды user-wide. У него свой per-user троттл на бэке."""
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/admin/client-control/report-broken",
            json={"telegram_id": str(tg_id)},
            headers=_admin_headers(tg_id),
        )
    except aiohttp.ClientError:
        await bot.send_message(
            chat_id, "Не получилось обработать — попробуй ещё раз через минуту."
        )
        return
    if status_code != 200 or not isinstance(data, dict):
        logger.warning(
            "report-broken (whole-sub) failed: tg_id=%s status=%s data=%.300s",
            tg_id, status_code, data,
        )
        await bot.send_message(
            chat_id, "Не получилось обработать — попробуй ещё раз через минуту."
        )
        return

    action = data.get("action")
    if action == "migrated":
        report_id = data.get("report_id")
        await bot.send_message(
            chat_id,
            "🔄 Поменяли сервер для всех устройств. Подписка обновится в "
            "приложении сама — нажми 🔄 рядом с профилем и попробуй подключиться "
            "через пару минут.\n\n"
            "Чтобы мы быстрее ловили блокировки — подскажи, какой у тебя интернет?",
            reply_markup=(
                operator_keyboard(int(report_id)) if report_id else help_keyboard()
            ),
        )
        if report_id:
            _spawn(
                _delayed_still_broken_prompt(bot, chat_id, int(report_id))
            )
    elif action == "throttled":
        await bot.send_message(
            chat_id,
            "👍 Мы уже перенесли тебя на другой сервер пару минут назад.\n\n"
            "Подписка обновляется в приложении сама — нажми 🔄 рядом с профилем "
            "и попробуй подключиться.\n\n"
            "Если через 10 минут всё ещё не работает — жми /help, разберёмся.",
        )
    elif action == "no_subscription":
        await bot.send_message(
            chat_id,
            "У тебя нет активной подписки — чинить пока нечего.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(text="💎 Выбрать тариф", callback_data="go:plans"),
            ]]),
        )
    else:  # no_target / user_not_found / прочее
        await bot.send_message(
            chat_id,
            "Не смогли автоматически подобрать другой сервер. Напиши в "
            "поддержку — разберёмся вручную.",
            reply_markup=help_keyboard(),
        )


# «Всё равно не работает» показываем не сразу, а через этот делей — даём
# юзеру время переподключиться. Через делей дёргаем бэк: если он ВИДИТ
# переподключение — молчим; если нет — присылаем пуш с этой кнопкой.
# 15 мин (а не 5): делей ДОЛЖЕН быть заметно больше TRAFFIC_STATS_INTERVAL
# (300с) — иначе к моменту проверки между жалобой и now может не лечь ни
# одного traffic-сэмпла → reconnected=False у реально переподключившегося →
# ложный пуш. За 15м ложится 2-3 сэмпла, переподключение видно надёжно.
_STILL_BROKEN_DELAY_S = 900


def _still_broken_keyboard(report_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="✅ Всё работает",
                    callback_data=f"op:ok:{report_id}",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="❌ Всё равно не работает",
                    callback_data=f"op:still:{report_id}",
                )
            ],
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


# value → человекочитаемый ярлык для ответа юзеру (value стабилен, уходит в
# бэкенд). Держим в синхроне с кнопками operator_keyboard и webapp/api.ts.
_OPERATOR_LABELS = {
    "mts": "МТС",
    "beeline": "Билайн",
    "megafon": "МегаФон (Yota)",
    "tele2": "Tele2 (Т-Мобайл)",
    "home_wifi": "домашний интернет / Wi-Fi",
    "other": "другой провайдер",
}


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
            # Yota — MVNO на сети МегаФона, Т-Мобайл (бывш. Tinkoff) — на сети
            # Tele2: их юзеры не находили себя в списке (см. «Йота в сделку не
            # входила») и уходили в «Другое», теряя точность роутинга. Подписываем.
            [_b("МегаФон (Yota)", "megafon"), _b("Tele2 (Т-Мобайл)", "tele2")],
            [_b("🏠 Домашний/WiFi", "home_wifi"), _b("Другое", "other")],
        ]
    )


def broken_device_keyboard(devices: list[dict]) -> types.InlineKeyboardMarkup:
    """Пикер «какое устройство не работает» (multi-device юзер). Текст кнопок =
    Device.name (как записал юзер); callback несёт только device_id (умещается
    в 64-байтный лимит Telegram, длинные имена идут в text — безопасно). Плюс
    «🔁 Все мои устройства» — старый whole-sub перенос для тех, у кого лёг
    весь пул."""
    rows = [
        [
            types.InlineKeyboardButton(
                text=(d.get("name") or "Устройство"),
                callback_data=f"brk:{d['device_id']}",
            )
        ]
        for d in devices
    ]
    rows.append(
        [
            types.InlineKeyboardButton(
                text="🔁 Все мои устройства", callback_data="brk:all"
            )
        ]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("brk:"))
async def broken_device_choice(callback_query: types.CallbackQuery) -> None:
    tg_id = callback_query.from_user.id
    arg = callback_query.data.split(":", 1)[1]
    await callback_query.answer()
    # Снимаем клавиатуру сразу — визуально чище (повтор по уже перенесённому
    # устройству и так отсечётся проверкой статуса на бэке).
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass

    chat_id = callback_query.message.chat.id
    if arg == "all":
        await _do_whole_sub_failover(callback_query.bot, chat_id, tg_id)
        return
    try:
        device_id = int(arg)
    except ValueError:
        return
    await _do_device_failover(callback_query.bot, chat_id, tg_id, device_id)


@router.callback_query(F.data.startswith("op:"))
async def operator_choice(callback_query: types.CallbackQuery) -> None:
    parts = callback_query.data.split(":")
    tg_id = callback_query.from_user.id
    if len(parts) < 3:
        await callback_query.answer()
        return

    # op:ok:<report_id> — «всё работает» → target-нода ok (позитивный сигнал).
    if parts[1] == "ok":
        try:
            rid = int(parts[2])
        except ValueError:
            await callback_query.answer()
            return
        # Проверяем статус ответа (как в health_ping_response): при 4xx/5xx/0
        # сигнал не записан — не рапортуем успех, оставляем клавиатуру для повтора.
        status_code, _ = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/admin/client-control/report-ok",
            json={"report_id": rid},
            headers=_admin_headers(tg_id),
        )
        if status_code != 200:
            logger.warning(
                "operator_choice report-ok failed: report_id=%s status=%s",
                rid, status_code,
            )
            await callback_query.answer(
                "Не получилось сохранить, попробуй ещё раз", show_alert=True
            )
            return
        await callback_query.answer()
        try:
            await callback_query.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await callback_query.message.answer("Отлично, рад что заработало! 🎉")
        return

    # op:still:<report_id> — «всё равно не работает» → поддержка + target=fail.
    if parts[1] == "still":
        try:
            rid = int(parts[2])
        except ValueError:
            await callback_query.answer()
            return
        status_code, _ = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/admin/client-control/report-still-broken",
            json={"report_id": rid},
            headers=_admin_headers(tg_id),
        )
        if status_code != 200:
            logger.warning(
                "operator_choice report-still-broken failed: report_id=%s status=%s",
                rid, status_code,
            )
            await callback_query.answer(
                "Не получилось сохранить, попробуй ещё раз", show_alert=True
            )
            return
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
    status_code, _ = await _fetch_json(
        "POST",
        f"{BACKEND_URL}/api/admin/client-control/report-operator",
        json={"report_id": report_id, "operator": operator},
        headers=_admin_headers(tg_id),
    )
    if status_code != 200:
        logger.warning(
            "operator_choice report-operator failed: report_id=%s operator=%s status=%s",
            report_id, operator, status_code,
        )
        await callback_query.answer(
            "Не получилось сохранить, попробуй ещё раз", show_alert=True
        )
        return
    await callback_query.answer()
    # Операторскую клавиатуру убираем. «Всё равно не работает» придёт
    # отдельным сообщением через делей (_delayed_still_broken_prompt).
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    # callback_query.answer(text) — это эфемерный тост, юзер его не видит в
    # чате («а мне не ответили ничего»). Отвечаем настоящим сообщением: он
    # подсказал нам сеть — подтверждаем, что услышали.
    label = _OPERATOR_LABELS.get(operator, "твоя сеть")
    await callback_query.message.answer(
        f"Понял — у тебя {label}. Спасибо, что подсказал! 🙏 Учтём, чтобы "
        f"быстрее ловить блокировки на твоей сети.",
    )


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

async def _send_balance(target: types.Message, telegram_id: int) -> None:
    """Показать баланс пользователю telegram_id, отвечая в чат target.

    Вынесено из cmd_balance, чтобы инлайн-кнопка «💳 Пополнить» (go_topup)
    могла переиспользовать логику: у callback message.from_user — это бот,
    а мутировать frozen-модель aiogram нельзя.
    """
    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/users/by_telegram/{telegram_id}/balance",
            headers=_admin_headers(telegram_id),
        )
    except aiohttp.ClientError:
        await target.answer("Бэкенд недоступен.")
        return

    if status_code == 404:
        await target.answer(
            "У вас ещё нет аккаунта — нажмите /start, потом выберите тариф."
        )
        return
    if status_code != 200 or not data:
        await target.answer("Не удалось получить баланс. Попробуйте позже.")
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
            # plan_name с бэкенда — экранируем перед HTML.
            lines.append(
                f"{badge} {html.escape(str(s['plan_name']))} — "
                f"{price_str}{exp_str}{renew_str}"
            )
    else:
        lines.append("Нет активных подписок. Нажмите /plans, чтобы выбрать тариф.")

    webapp_kb = webapp_inline_keyboard()
    await target.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=webapp_kb,
    )


@router.message(F.text == BTN_TOPUP)
@router.message(F.text == "Баланс")
@router.message(Command("balance"))
async def cmd_balance(message: types.Message):
    """Show balance + days remaining + a deep-link into the WebApp.

    Stage 4: this is the primary "how much money do I have" surface
    outside the WebApp. Topup happens through the WebApp because Stars
    invoice flow is much smoother there than via inline buttons.
    """
    await _send_balance(message, message.from_user.id)


# ── /referral — реферальная ссылка ──

# username бота неизменен в рамках процесса — кэшируем, чтобы не дёргать
# Telegram get_me на каждый реферальный запрос.
_bot_username: str | None = None


async def _get_bot_username(bot) -> str | None:
    """username бота (кэш). None → краткий сетевой блип к Telegram; вызывающий
    решает, как деградировать, вместо того чтобы ронять весь ответ в глобальный
    on_dispatch_error (юзер бы увидел generic-ошибку при уже полученном коде)."""
    global _bot_username
    if _bot_username:
        return _bot_username
    try:
        me = await bot.get_me()
    except Exception:  # noqa: BLE001
        logger.warning("get_me failed while building referral link", exc_info=True)
        return None
    _bot_username = me.username
    return _bot_username


async def _send_referral(msg: types.Message, bot, user_id: int) -> None:
    """Единый флоу выдачи реферальной ссылки (общий для команды и inline-кнопки
    приветствия). Раньше был скопирован в cmd_referral и go_referral 1-в-1 —
    правки текста/бонуса разъезжались."""
    status_code, data = await _fetch_json(
        "POST",
        f"{BACKEND_URL}/api/referral/code",
        json={"telegram_id": str(user_id)},
        headers=_admin_headers(user_id),
    )
    if status_code == 0:
        await msg.answer("Бэкенд недоступен.")
        return
    if status_code != 200 or not data:
        await msg.answer("Не удалось получить реферальную ссылку.")
        return

    code = data.get("code", "")
    uses = data.get("uses", 0)
    username = await _get_bot_username(bot)
    if not username:
        # Сетевой блип к Telegram: код у нас есть, ссылку собрать не смогли —
        # отдаём код текстом, чтобы юзер не остался ни с чем.
        await msg.answer(
            f"🎁 Твой реферальный код: <code>ref_{html.escape(str(code))}</code>\n"
            "Готовую ссылку собрать не вышло — попробуй ещё раз через минуту.",
            parse_mode="HTML",
        )
        return
    ref_link = f"https://t.me/{username}?start=ref_{code}"

    # Stage 4: реферал теперь через денежный бонус, не через дни.
    # Сумма берётся из бэкенда (REFERRAL_BONUS_KOPECKS, дефолт 50 ₽).
    await msg.answer(
        f"🎁 <b>Твоя реферальная ссылка:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"Приглашённый получает <b>+50 ₽ на баланс</b>, ты — <b>+50 ₽</b>.\n"
        f"Приглашено: {uses} чел.",
        parse_mode="HTML",
    )


@router.message(F.text == BTN_INVITE)
@router.message(F.text == "Реферальная ссылка")
@router.message(Command("referral"))
async def cmd_referral(message: types.Message):
    await _send_referral(message, message.bot, message.from_user.id)


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


@router.message(Command("adstats"))
async def ad_sources_stats(message: types.Message):
    """Admin: воронка по рекламным меткам (старт → триал → оплата + выручка)."""
    if not _is_admin(message.from_user.id):
        await message.answer("Эта команда только для админов.")
        return
    try:
        status, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/admin/ad-sources",
            headers=_admin_headers(message.from_user.id),
        )
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен.")
        return
    if status != 200 or not isinstance(data, dict):
        await message.answer("Не удалось загрузить статистику рекламы.")
        return

    sources = data.get("sources") or []
    if not sources:
        await message.answer(
            "Пока нет данных по рекламным меткам.\n\nМетка ставится из ссылки "
            "<code>t.me/имя_бота?start=МЕТКА</code> (напр. <code>?start=tg_blogger1</code>).",
            parse_mode="HTML",
        )
        return

    esc = html.escape
    lines = ["📊 <b>Реклама — воронка по меткам</b>", ""]
    for s in sources:
        started = int(s.get("started", 0) or 0)
        trial = int(s.get("trial", 0) or 0)
        paid = int(s.get("paid", 0) or 0)
        rev = (s.get("revenue_kopecks", 0) or 0) / 100
        cr = f"{paid / started * 100:.0f}%" if started else "—"
        lines.append(
            f"<b>{esc(str(s.get('source')))}</b>: старт {started} → триал {trial} "
            f"→ оплата {paid} ({cr}) · {rev:.0f}₽"
        )
    lines.append("")
    lines.append(
        f"<b>Итого:</b> старт {int(data.get('total_started', 0) or 0)}, "
        f"оплат {int(data.get('total_paid', 0) or 0)}, "
        f"выручка {(data.get('total_revenue_kopecks', 0) or 0) / 100:.0f}₽"
    )
    text = "\n".join(lines)
    await message.answer(text[:4000], parse_mode="HTML", disable_web_page_preview=True)


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

    # Уведомление юзеру оборачиваем в try/except: юзер мог заблокировать бота
    # (TelegramForbiddenError) или конфиг содержит символы, ломающие HTML
    # (TelegramBadRequest). Без обёртки момент «оплата подтверждена» роняет
    # хендлер молча, и админ уверен, что всё доставлено (тост уже показан).
    bot = callback_query.message.bot
    try:
        if kind == "topup":
            amount = invoice.get("amount", 0)
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ Баланс пополнен на {amount:.0f} ₽",
            )
        elif action == "renewal":
            await bot.send_message(
                chat_id=user_id,
                text="✅ Подписка продлена!",
            )
        elif credentials:
            configs_text = ["✅ Оплата подтверждена! Твои конфиги:\n"]
            for cred in credentials:
                # proto/config_text приходят с бэкенда — vless-ссылки содержат
                # '&', имена нод в fragment — спецсимволы; экранируем под HTML.
                configs_text.append(f"<b>{html.escape(str(cred['proto']))}:</b>")
                configs_text.append(
                    f"<code>{html.escape(str(cred['config_text']))}</code>"
                )
            configs_text.append("\nНе знаешь как настроить? 👇")
            await bot.send_message(
                chat_id=user_id,
                text="\n".join(configs_text),
                parse_mode="HTML",
                reply_markup=onboarding_keyboard(),
            )
        else:
            await bot.send_message(
                chat_id=user_id,
                text="✅ Счет оплачен! Конфиг будет готов через минуту. Используй /config.",
            )
    except Exception as exc:  # noqa: BLE001 — блокировка бота, битый HTML и т.п.
        logger.error(
            "invoice #%s marked paid but user notify failed (user=%s): %s",
            invoice.get("id"), user_id, exc,
        )
        try:
            await callback_query.message.answer(
                f"⚠️ Счёт #{invoice.get('id')} отмечен оплаченным, но уведомить "
                f"юзера {user_id} не удалось ({exc}). Свяжись вручную."
            )
        except Exception:  # noqa: BLE001
            pass


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

# С 15.04.2026 Яндекс, банки, маркетплейсы и госсервисы закрываются при
# включённом VPN — самая частая свежая жалоба. Оговорка в тексте осталась одна:
# RU-обход живёт на relay-нодах, а на standalone-зарубежной ноде правило —
# безвредный no-op (docs/infrastructure/nodes.md § RU-обход). По протоколам
# оговорка снята: с 2026-07-28 все четыре ходят одинаково.
_HELP_RU_SITES = (
    "🇷🇺 Российские сайты и банки при включённом VPN\n\n"
    "Многие сервисы — Яндекс, банки, маркетплейсы, госуслуги — с весны 2026 "
    "закрываются, если видят VPN.\n\n"
    "У нас для этого есть раздельный маршрут: на российских серверах трафик к "
    "российским сайтам идёт напрямую, мимо VPN. Выключать его каждый раз не "
    "нужно.\n\n"
    "Работает не на всех серверах. Если что-то российское не открывается — "
    "не отключай VPN целиком, а напиши в поддержку: подберём подходящий сервер."
)


@router.callback_query(F.data == "help:ru_sites")
async def help_ru_sites(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.answer(
        _HELP_RU_SITES, reply_markup=help_back_keyboard()
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
    # E1.6: раньше флаги были захардкожены False — вернувшись в главное меню,
    # юзер терял строку про подарок, хотя подарок не забран. Оффер, который то
    # есть, то нет, читается как «предложение истекло».
    trial_available, has_devices = await _fetch_user_flags(callback_query.from_user.id)
    welcome = format_welcome(first_name, is_new=False, trial_available=trial_available)
    await callback_query.message.answer(
        welcome,
        reply_markup=welcome_action_keyboard(trial_available=trial_available),
    )
    await callback_query.message.answer(
        "⌨️ Кнопки внизу всегда под рукой.",
        reply_markup=start_keyboard(has_devices=has_devices),
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
    # НЕ мутируем callback_query.message: модели aiogram 3 frozen,
    # присваивание from_user кидает ValidationError. Вместо этого зовём
    # общий хелпер с правильным telegram_id инициатора.
    await _send_balance(callback_query.message, callback_query.from_user.id)


@router.callback_query(F.data == "go:referral")
async def go_referral(callback_query: types.CallbackQuery):
    """Run the /referral flow from an inline button on the welcome msg.

    cmd_referral reads message.from_user; from a callback that'd be the
    bot itself, поэтому передаём callback_query.from_user.id в общий хелпер
    _send_referral (текст/бонус живут в одном месте).
    """
    await callback_query.answer()
    await _send_referral(
        callback_query.message, callback_query.bot, callback_query.from_user.id
    )


# ── Ops-агент: /ops <команда> → dry-run план (Phase 2, ничего не выполняет) ──

_OPS_TIER_ICON = {"read": "👀", "reversible": "♻️", "costly": "💸", "destructive": "⚠️"}


def _truncate_lines(lines: list[str], limit: int) -> str:
    """Склеить строки в HTML-сообщение, не превышая limit. Режем по ЦЕЛЫМ
    строкам (не посреди тега/entity — иначе Telegram отдаёт 400 на parse_mode=HTML)
    и явно помечаем обрез, чтобы скрытые шаги были видны как скрытые."""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    marker = "\n…<i>план обрезан</i>"
    budget = limit - len(marker)
    kept: list[str] = []
    used = 0
    for ln in lines:
        add = len(ln) + (1 if kept else 0)
        if used + add > budget:
            break
        kept.append(ln)
        used += add
    return "\n".join(kept) + marker


def _strip_html(text: str) -> str:
    """HTML → плоский текст для фолбэка, если Telegram отверг разметку."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _render_ops_plan(result: dict) -> str:
    """Plan dict из POST /api/agent/ops/plan → HTML-сообщение Telegram.
    Все динамические строки экранируем (план приходит от LLM, может содержать <>&)."""
    plan = (result or {}).get("plan") or {}
    esc = html.escape
    lines: list[str] = ["🧭 <b>Ops-план</b> <i>(dry-run — ничего не выполнено)</i>"]

    summary = plan.get("summary")
    if summary:
        lines.append(esc(str(summary)))

    if plan.get("feasible") is False:
        reason = plan.get("blocked_reason") or "не уточнено"
        lines.append(f"\n⛔ <b>Невыполнимо:</b> {esc(str(reason))}")
        if plan.get("notes"):
            lines.append(f"ℹ️ {esc(str(plan['notes']))}")
        return "\n".join(lines)

    steps = plan.get("steps") or []
    if steps:
        lines.append("")
        for i, st in enumerate(steps, 1):
            icon = _OPS_TIER_ICON.get(str(st.get("tier")), "•")
            desc = esc(str(st.get("description") or st.get("kind") or "шаг"))
            block = [f"{i}. {icon} {desc}"]
            extra = []
            if st.get("est_cost_rub") not in (None, 0):
                extra.append(f"~{st['est_cost_rub']}₽")
            if st.get("est_users_affected") not in (None, 0):
                extra.append(f"юзеров: {st['est_users_affected']}")
            if st.get("reversible") is False:
                extra.append("необратимо")
            if extra:
                block.append(f"   <i>{esc(' · '.join(str(x) for x in extra))}</i>")
            for w in st.get("warnings") or []:
                block.append(f"   ⚠️ {esc(str(w))}")
            lines.append("\n".join(block))
    else:
        lines.append("\n(шагов нет)")

    totals = []
    if plan.get("total_est_cost_rub") not in (None, 0):
        totals.append(f"итого ~{plan['total_est_cost_rub']}₽")
    if plan.get("total_users_affected") not in (None, 0):
        totals.append(f"юзеров затронем: {plan['total_users_affected']}")
    if totals:
        lines.append(f"\n<b>{esc(' · '.join(str(x) for x in totals))}</b>")

    if plan.get("needs_confirmation"):
        lines.append("\n⚠️ <b>Есть платные/необратимые шаги — нужно подтверждение.</b>")
    if plan.get("notes"):
        lines.append(f"ℹ️ {esc(str(plan['notes']))}")
    lines.append(
        f"\n<i>модель: {esc(str(result.get('model') or '?'))}, "
        f"итераций: {result.get('iterations')}</i>"
    )

    return _truncate_lines(lines, 4000)


_OPS_EXEC_TERMINAL = {"executed", "partial", "failed", "expired", "cancelled"}

# Поллинг статуса исполнения: после N подряд сетевых провалов считаем связь с
# бэком потерянной и честно сообщаем «статус неизвестен», а не врём «executing».
# Per-poll таймаут снижен с 15с — 15с для короткого статус-GET много, из-за
# него недоступность выявлялась слишком медленно.
_OPS_POLL_MAX_CONSEC_FAIL = int(os.getenv("OPS_POLL_MAX_CONSEC_FAIL", "3"))
_OPS_POLL_TIMEOUT_S = float(os.getenv("OPS_POLL_TIMEOUT_S", "8"))


def _ops_exec_keyboard(payload: dict) -> "types.InlineKeyboardMarkup | None":
    """Кнопка «Исполнить» под планом — если он выполним и есть шаги."""
    plan = (payload or {}).get("plan") or {}
    plan_id = (payload or {}).get("plan_id")
    if not plan_id or plan.get("feasible") is False or not (plan.get("steps") or []):
        return None
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(text="🚀 Исполнить", callback_data=f"opsx:{plan_id}")
        ]]
    )


def _ops_confirm_keyboard(plan_id: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(text="✅ Да, исполнить", callback_data=f"opsxgo:{plan_id}"),
            types.InlineKeyboardButton(text="✖️ Отмена", callback_data=f"opsxno:{plan_id}"),
        ]]
    )


def _ops_run_keyboard(plan_id: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(text="🚀 Исполнить", callback_data=f"opsx:{plan_id}")
        ]]
    )


def _render_ops_exec(payload: dict) -> str:
    """Результат исполнения (из GET /agent/ops/plan/{id}) → HTML-сообщение."""
    esc = html.escape
    status = str((payload or {}).get("status") or "?")
    execn = (payload or {}).get("execution") or {}
    head = {
        "executed": "✅ <b>План исполнен</b>",
        "partial": "⚠️ <b>Исполнено частично</b>",
        "failed": "⛔ <b>Ошибка исполнения</b>",
        "expired": "⌛ <b>План протух</b>",
        "executing": "⏳ <b>Ещё выполняется…</b>",
    }.get(status, f"<b>Статус: {esc(status)}</b>")
    lines = [head]

    # Ранний отказ (validate/preflight/integrity) — покажем причину.
    phase = execn.get("phase")
    if phase in ("validate", "preflight", "integrity"):
        why = execn.get("rejections") or execn.get("reasons") or [execn.get("reason")]
        lines.append("⛔ " + esc("; ".join(str(x) for x in why if x)))

    for s in execn.get("steps") or []:
        ic = {"done": "✅", "skipped": "⏭", "failed": "⛔"}.get(str(s.get("status")), "•")
        line = f"{ic} {esc(str(s.get('kind')))} — {esc(str(s.get('status')))}"
        ids = s.get("created_node_ids")
        if ids:
            line += f" (ноды: {esc(', '.join(str(i) for i in ids))})"
        lines.append(line)
        if s.get("status") == "failed" and s.get("detail"):
            lines.append(f"   ⚠️ {esc(str(s['detail']))}")

    if execn.get("total_cost"):
        lines.append(f"\n💸 ~{esc(str(execn['total_cost']))}₽")
    if status == "executing":
        lines.append("\n<i>Заказ ещё идёт — проверь ноды через минуту.</i>")
    return _truncate_lines(lines, 4000)


async def _poll_ops_exec(session, plan_id: int, actor_id: int, *, tries: int = 20, delay: float = 3.0) -> dict:
    """Поллим статус плана, пока не терминальный. Возвращает последний снимок."""
    last: dict = {"status": "executing", "plan_id": plan_id}
    consecutive_failures = 0
    for _ in range(tries):
        await asyncio.sleep(delay)
        try:
            async with session.request(
                "GET",
                f"{BACKEND_URL}/api/agent/ops/plan/{plan_id}",
                headers=_admin_headers(actor_id),
                timeout=aiohttp.ClientTimeout(total=_OPS_POLL_TIMEOUT_S),
            ) as resp:
                # Любой ответ = бэк достижим, сбрасываем счётчик недоступности.
                consecutive_failures = 0
                if resp.status == 200:
                    last = await resp.json()
                    if str(last.get("status")) in _OPS_EXEC_TERMINAL:
                        return last
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Сетевая ошибка/таймаут одной итерации. Копим подряд идущие: пока
            # их мало — ждём дальше (транзиент), при пороге — выходим с явным
            # маркером «связь потеряна, статус неизвестен».
            consecutive_failures += 1
            if consecutive_failures >= _OPS_POLL_MAX_CONSEC_FAIL:
                logger.warning(
                    "ops-exec poll: backend unreachable %s times, plan_id=%s",
                    consecutive_failures, plan_id,
                )
                return {"status": "unreachable", "plan_id": plan_id}
            continue
    return last


@router.message(Command("ops"))
async def ops_plan(message: types.Message, command: CommandObject):
    """Admin: NL ops-команда → dry-run план от агента. НИЧЕГО НЕ ВЫПОЛНЯЕТ —
    только показывает шаги/оценку. Выполнение (за подтверждением) — отдельно."""
    if not _is_admin(message.from_user.id):
        await message.answer("Эта команда только для админов.")
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "Напиши команду после <code>/ops</code>, напр.:\n"
            "<code>/ops закажи 2 ноды в Германии, подними туннель, "
            "перевези юзеров с ноды 19</code>",
            parse_mode="HTML",
        )
        return

    tg_id = message.from_user.id
    now_mono = asyncio.get_event_loop().time()
    last = _ops_last.get(tg_id)
    if last is not None and (now_mono - last) < _OPS_COOLDOWN_S:
        await message.answer(
            f"⏳ Подожди ~{_OPS_COOLDOWN_S}с между /ops — агент ещё считает прошлый запрос."
        )
        return
    _ops_last[tg_id] = now_mono

    wait = await message.answer("⏳ Собираю план… (агент опрашивает флот, до ~минуты)")
    # Прямой single-shot вызов (НЕ _fetch_json): у него ретрай на 5xx, а 503 от
    # агента — это полный прогон Claude, повторять дорого. Таймаут щедрый —
    # bot→backend идёт напрямую (http://backend:8000), без nginx-кэпа 60с.
    session = await get_session()
    try:
        async with session.request(
            "POST",
            f"{BACKEND_URL}/api/agent/ops/plan",
            json={"command": text},
            headers=_admin_headers(message.from_user.id),
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            status = resp.status
            try:
                payload = await resp.json()
            except Exception:  # noqa: BLE001
                payload = {"message": await resp.text()}
    except asyncio.TimeoutError:
        await wait.edit_text("Агент думал слишком долго (>120с). Упрости команду и повтори.")
        return
    except aiohttp.ClientError as exc:
        await wait.edit_text(f"Бэкенд недоступен: {exc}")
        return

    if status == 503:
        detail = (payload or {}).get("detail") or "агент недоступен"
        await wait.edit_text(f"Агент недоступен: {html.escape(str(detail))}")
        return
    if status == 429:
        await wait.edit_text("⏳ Слишком часто — рейт-лимит агента. Повтори через минуту.")
        return
    if status != 200:
        detail = (payload or {}).get("detail") or (payload or {}).get("message") or status
        await wait.edit_text(f"Не удалось построить план: {html.escape(str(detail))}")
        return

    rendered = _render_ops_plan(payload)
    kb = _ops_exec_keyboard(payload)
    try:
        await wait.edit_text(
            rendered,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except TelegramBadRequest:
        # Невалидный HTML (сырьё от LLM / несбалансированный тег) — Telegram
        # отверг разметку. Отдаём план без неё, чтобы он ДОШЁЛ до админа, а не
        # завис на «⏳ Собираю план…».
        await wait.edit_text(
            _strip_html(rendered)[:4000], disable_web_page_preview=True, reply_markup=kb
        )


@router.callback_query(F.data.startswith("opsx:"))
async def ops_exec_confirm(callback_query: types.CallbackQuery) -> None:
    """Тап «Исполнить» → экран подтверждения (двойной тап вместо PIN)."""
    if not _is_admin(callback_query.from_user.id):
        await callback_query.answer("Только для админов.", show_alert=True)
        return
    plan_id = callback_query.data.split(":", maxsplit=1)[1]
    try:
        await callback_query.message.edit_reply_markup(
            reply_markup=_ops_confirm_keyboard(plan_id)
        )
    except TelegramBadRequest:
        pass
    await callback_query.answer("Подтверди исполнение плана")


@router.callback_query(F.data.startswith("opsxno:"))
async def ops_exec_cancel(callback_query: types.CallbackQuery) -> None:
    if not _is_admin(callback_query.from_user.id):
        await callback_query.answer("Только для админов.", show_alert=True)
        return
    plan_id = callback_query.data.split(":", maxsplit=1)[1]
    try:
        await callback_query.message.edit_reply_markup(reply_markup=_ops_run_keyboard(plan_id))
    except TelegramBadRequest:
        pass
    await callback_query.answer("Отменено")


@router.callback_query(F.data.startswith("opsxgo:"))
async def ops_exec_go(callback_query: types.CallbackQuery) -> None:
    """Подтверждение → POST /execute → «запускаю» → поллинг статуса → результат."""
    if not _is_admin(callback_query.from_user.id):
        await callback_query.answer("Только для админов.", show_alert=True)
        return
    plan_id = callback_query.data.split(":", maxsplit=1)[1]
    actor_id = callback_query.from_user.id
    await callback_query.answer()
    try:  # убираем кнопки — чтобы повторно не тапнули
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    launching = await callback_query.message.answer("🚀 Запускаю исполнение…")

    session = await get_session()
    try:
        async with session.request(
            "POST",
            f"{BACKEND_URL}/api/agent/ops/execute",
            json={"plan_id": int(plan_id)},
            headers=_admin_headers(actor_id),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            status = resp.status
            try:
                data = await resp.json()
            except Exception:  # noqa: BLE001
                data = {"message": await resp.text()}
    except asyncio.TimeoutError:
        # Как в ops_plan: total-таймаут кидает asyncio.TimeoutError, не ClientError.
        await launching.edit_text(
            "Бэкенд не ответил за 30с — статус запуска неизвестен. Проверь план через /ops."
        )
        return
    except aiohttp.ClientError as exc:
        await launching.edit_text(f"Бэкенд недоступен: {html.escape(str(exc))}")
        return

    if status == 503:
        detail = (data or {}).get("detail") or "исполнение недоступно"
        await launching.edit_text(f"Исполнение недоступно: {html.escape(str(detail))}")
        return
    if status == 409:
        await launching.edit_text("Этот план уже исполняется или исполнен.")
        return
    if status != 200:
        detail = (data or {}).get("detail") or (data or {}).get("message") or status
        await launching.edit_text(f"Не удалось запустить: {html.escape(str(detail))}")
        return

    await launching.edit_text("🚀 Запущено, жду результат заказа…")
    result = await _poll_ops_exec(session, int(plan_id), actor_id)
    if result.get("status") == "unreachable":
        # Бэк был недоступен весь поллинг — НЕ показываем оптимистичное
        # «executing/проверь через минуту»: статус реально неизвестен.
        await launching.edit_text(
            "⚠️ Не удалось получить статус исполнения — бэкенд недоступен. "
            "Статус неизвестен: проверь ноды и план вручную (/ops) ПРЕЖДЕ чем "
            "запускать повторно, чтобы не задвоить заказ."
        )
        return
    try:
        await launching.edit_text(
            _render_ops_exec(result), parse_mode="HTML", disable_web_page_preview=True
        )
    except TelegramBadRequest:
        await launching.edit_text(_strip_html(_render_ops_exec(result))[:4000])
