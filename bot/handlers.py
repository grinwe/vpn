import asyncio
import hashlib
import html
import logging
import math
import os
import re
from typing import NamedTuple
from urllib.parse import urlparse

import aiohttp
from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from .config import (
    ADMIN_API_TOKEN,
    ADMIN_IDS,
    BACKEND_URL,
    LEGAL_BASE_URL,
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
    about_keyboard,
    help_back_keyboard,
    help_keyboard,
    onboarding_keyboard,
    start_keyboard,
    webapp_inline_keyboard,
    welcome_action_keyboard,
)
from .plural import plural_days

# Stage 9b — человекочитаемые подписи кнопок выбора способа оплаты.
# Неизвестное имя провайдера показывается как есть (кнопка всё равно
# работает: имя уходит в checkout verbatim).
_PROVIDER_LABELS = {
    "telegram_stars": "⭐ Telegram Stars",
    "stars": "⭐ Telegram Stars",
    # Одна интеграция lava — два способа (2026-09-19: карта у агрегатора
    # PAY2ME закрыта, единой кнопки «карта / СБП» больше нет).
    "lava_top_sbp": "🏦 СБП",
    "lava_top": "💳 Карта РФ",
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

# Раньше эта мысль жила в шапке /config и приходила каждому вместе со
# ссылкой (шум: жалоба владельца 28.08 на 7 сообщений за один тап). Она
# нужна только тому, кто реально настраивает клиент, — то есть здесь.
_SUB_LINK_ONCE_NOTE = (
    "Ссылку достаточно добавить один раз: при смене сервера клиент сам "
    "подтянет новые настройки."
)

ONBOARDING_INSTRUCTIONS = {
    "android": (
        "<b>Настройка на Android (v2rayNG):</b>\n\n"
        "1. Установите <b>v2rayNG</b> из Google Play или GitHub\n"
        "2. Скопируйте ссылку подписки: она в личном кабинете или по команде /config\n"
        "3. Откройте v2rayNG → нажмите <b>+</b> → <b>Импорт из буфера</b>\n"
        "4. Нажмите кнопку ▶️ для подключения\n\n"
        "Альтернатива: <b>Hiddify</b> (Google Play) — автоимпорт по ссылке.\n\n"
        + _SUB_LINK_ONCE_NOTE
    ),
    "ios": (
        "<b>Настройка на iOS (Hiddify / Streisand):</b>\n\n"
        "1. Установите <b>Hiddify</b> или <b>Streisand</b> из App Store\n"
        "2. Скопируйте ссылку подписки: она в личном кабинете или по команде /config\n"
        "3. Откройте приложение → <b>+</b> → <b>Добавить из буфера</b>\n"
        "4. Нажмите <b>Подключить</b>\n\n"
        + _SUB_LINK_ONCE_NOTE
    ),
    "windows": (
        "<b>Настройка на Windows (Hiddify / Nekoray):</b>\n\n"
        "1. Скачайте <b>Hiddify</b> с hiddify.com или <b>Nekoray</b> с GitHub\n"
        "2. Скопируйте ссылку подписки: она в личном кабинете или по команде /config\n"
        "3. В программе: <b>Добавить профиль из буфера</b>\n"
        "4. Активируйте системный прокси и подключитесь\n\n"
        + _SUB_LINK_ONCE_NOTE
    ),
    "macos": (
        "<b>Настройка на macOS (Hiddify):</b>\n\n"
        "1. Скачайте <b>Hiddify</b> с hiddify.com\n"
        "2. Скопируйте ссылку подписки: она в личном кабинете или по команде /config\n"
        "3. Добавьте профиль из буфера обмена\n"
        "4. Подключитесь\n\n"
        + _SUB_LINK_ONCE_NOTE
    ),
}


# ── /start ──

# Бесплатные дни триала, если бэкенд не ответил (дефолт TRIAL_DURATION_DAYS
# на бэкенде). Точное число, в том числе 3 + 3 по приглашению, приходит из
# /users/register: бот пишет ту же цифру, которую выдаст активация.
_TRIAL_DAYS_DEFAULT = 3


def _trial_line(days: int, ref_days: int) -> str:
    """Строка про подарок в приветствии: 3 дня или 3 + 3 по приглашению."""
    if ref_days > 0:
        head = (
            f"🎁 Тебя пригласил друг, поэтому у тебя {plural_days(days + ref_days)} "
            f"VPN бесплатно вместо {days}. Карта не нужна.\n"
        )
    else:
        head = f"🎁 {plural_days(days)} VPN бесплатно, карта не нужна.\n"
    return (
        head
        + "Один тап по кнопке ниже: получишь ссылку и инструкцию, как подключиться.\n\n"
    )


def _trial_days_from(data: dict | None) -> tuple[int, int]:
    """``(бесплатные дни, дни по приглашению)`` из ответа ``/users/register``.

    Старый бэкенд или мусор в поле: дефолт 3 + 0, как при недоступном бэкенде.
    """
    data = data or {}
    try:
        days = int(data.get("trial_days") or _TRIAL_DAYS_DEFAULT)
    except (TypeError, ValueError):
        days = _TRIAL_DAYS_DEFAULT
    try:
        ref_days = max(0, int(data.get("trial_referral_days") or 0))
    except (TypeError, ValueError):
        ref_days = 0
    return days, ref_days


class UserFlags(NamedTuple):
    """Флаги входных экранов из ``/users/register``."""

    trial_available: bool
    has_devices: bool
    has_subscription: bool
    # Бесплатные дни триала и дни по приглашению (3 и 0 или 3): для текстов
    # приветствия, /plans и кнопки подарка.
    trial_days: int = _TRIAL_DAYS_DEFAULT
    ref_days: int = 0


async def _fetch_user_flags(telegram_id: int) -> UserFlags:
    """Общий хелпер входных экранов: флаги ``UserFlags`` из ``/users/register``.

    Нужен там, где приветствие/тарифы рисуются НЕ из ``/start`` (главное меню,
    список тарифов): раньше эти экраны хардкодили ``trial_available=False`` и
    подарок из них пропадал. Регистрация идемпотентна, поэтому переиспользуем
    её же эндпоинт вместо отдельного read-API.

    Два флага про подписку — нарочно разные. ``has_devices`` бэкенд считает
    по ACTIVE-девайсам и им гейтится «🆘 VPN не работает» (чинить можно только
    выданное). ``has_subscription`` — есть подписка active/frozen, и им
    гейтится путь к ссылке (go:config): у юзера на cold-пути девайс ~минуту
    pending (или failed), у замороженного девайсов нет вовсе — но подписка
    есть, и ссылку/статус ему показывать надо (ревью инцидента 2026-08-25).

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
            days, ref_days = _trial_days_from(data)
            return UserFlags(
                bool(data.get("trial_available")),
                bool(data.get("has_devices")),
                bool(data.get("has_subscription")),
                days,
                ref_days,
            )
    except Exception:  # noqa: BLE001
        logger.warning("_fetch_user_flags: бэкенд недоступен, показываем оффер")
    return UserFlags(True, False, False, _TRIAL_DAYS_DEFAULT, 0)


def format_welcome(
    name: str,
    is_new: bool,
    trial_available: bool,
    trial_days: int = _TRIAL_DAYS_DEFAULT,
    ref_days: int = 0,
) -> str:
    """Приветствие. Тексты согласованы в роадмапе (E1.1/E1.1a).

    ``trial_days`` / ``ref_days`` — бесплатные дни и дни по приглашению из
    ``/users/register``: строка про подарок пишет «3 дня» или «6 дней вместо 3».

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
        body += _trial_line(trial_days, ref_days)
    # Без «Выбери действие ниже 👇»: приглашение к действию теперь несёт
    # второе, короткое сообщение с inline-кнопками (см. _send_welcome_pair),
    # и повторять его здесь — тот самый шум, на который жаловался владелец.
    return body.rstrip("\n")


async def _send_welcome_pair(
    send,
    *,
    welcome: str,
    trial_available: bool,
    is_new: bool,
    has_devices: bool,
    has_link: bool,
    trial_days: int = _TRIAL_DAYS_DEFAULT,
) -> None:
    """Приветствие двумя сообщениями: текст + нижняя reply-клавиатура, затем
    короткий вопрос + inline-кнопки действий.

    ``trial_days`` — бесплатных дней всего (3 или 3 + 3), число для кнопки
    подарка.

    Telegram даёт один ``reply_markup`` на сообщение, а нам нужны обе
    клавиатуры: reply (всегда внизу) и inline (подарок / тарифы / ЛК). До
    28.08 порядок был обратным: приветствие с inline-кнопками, а reply-
    клавиатура ехала на отдельной строке «⌨️ Кнопки внизу всегда под рукой»,
    у которой не было никакой функции, кроме как везти клавиатуру. Теперь
    второе сообщение — вопрос-приглашение: у него есть смысл (это и есть
    призыв к действию из приветствия), а кнопки действий оказываются
    последним, что юзер видит на экране. Итог те же два сообщения, но без
    пустой строки. ``send`` — ``message.answer`` либо ``callback.message.answer``.
    """
    await send(welcome, reply_markup=start_keyboard(has_devices=has_devices))
    await send(
        "Начнём? 👇" if trial_available else "Что дальше? 👇",
        reply_markup=welcome_action_keyboard(
            trial_available=trial_available,
            is_new=is_new,
            has_link=has_link,
            trial_days=trial_days,
        ),
    )


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
            await message.answer(
                "Поддержка пока не настроена.", reply_markup=help_keyboard()
            )
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
    has_subscription = False
    trial_days, ref_days = _TRIAL_DAYS_DEFAULT, 0
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
            has_subscription = bool(data.get("has_subscription"))
            # Дни считаются после привязки реферала в этом же запросе: по
            # ссылке друга новичок сразу видит «6 дней».
            trial_days, ref_days = _trial_days_from(data)
        # Быстрый таймаут мог оборваться раньше, чем метка/реферал доехали до
        # бэка. Если не достучались, но атрибуция была — до-регистрируем в фоне
        # полным таймаутом, чтобы не потерять источник конверсии.
        if _status == 0 and (referral_code or source):
            _spawn(_register_retry_bg(message.from_user.id, register_payload))
    except Exception:
        logger.warning("cmd_start: register failed, показываем welcome как новичку")

    first_name = message.from_user.first_name or "друг"
    welcome = format_welcome(
        first_name, is_new, trial_available, trial_days=trial_days, ref_days=ref_days
    )

    # E1.5: новичку не сообщаем про поломки до того, как он что-то получил —
    # «если что-то сломалось» на первом экране читается как «тут всё ломается»
    # (has_devices прячет «🆘 VPN не работает» в нижней клавиатуре).
    await _send_welcome_pair(
        message.answer,
        welcome=welcome,
        trial_available=trial_available,
        is_new=is_new,
        has_devices=has_devices,
        has_link=has_subscription or has_devices,
        trial_days=trial_days + ref_days,
    )


# ── /plans ──

@router.message(F.text == BTN_BUY)
@router.message(F.text == "Купить VPN")
@router.message(Command("plans"))
async def list_plans(
    message: types.Message | None,
    user_id: int | None = None,
    *,
    bot: "types.Bot | None" = None,
    chat_id: int | None = None,
):
    """Прайс. ``user_id`` обязателен при вызове из callback'ов: там
    ``message.from_user`` — сам БОТ, и триал-флаги считались бы по нему —
    ложный оффер бесплатных дней юзерам с истраченным триалом (аудит
    2026-08-21, тупик №3). ``bot``/``chat_id`` — путь для сообщений
    старше 48ч (InaccessibleMessage без ``.answer``)."""
    if message is not None:
        bot = message.bot
        chat_id = message.chat.id
    uid = user_id if user_id is not None else message.from_user.id

    async def _say(text: str, **kwargs):
        if message is not None:
            await message.answer(text, **kwargs)
        else:
            await bot.send_message(chat_id, text, **kwargs)

    try:
        status, data = await _fetch_json("GET", f"{BACKEND_URL}/api/plans")
    except aiohttp.ClientError:
        await _say("Бэкенд недоступен. Попробуйте позже.")
        return

    if status == 0:
        # _fetch_json ест сетевые ошибки сам и возвращает 0 — отличаем
        # «бэкенд лежит» от «бэкенд ответил не-200» (аудит №14).
        await _say("Сервис временно недоступен. Попробуй позже.")
        return
    if status != 200:
        await _say("Не удалось получить список тарифов. Попробуйте позже.")
        return

    plans = [p for p in (data or []) if p.get("is_visible", True)]
    if not plans:
        await _say("Тарифы пока не настроены. Попробуйте позже.")
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
    flags = await _fetch_user_flags(uid)
    trial_available = flags.trial_available
    # Путь к ссылке показываем по подписке, а не только по живым девайсам:
    # на cold-пути девайс ~минуту pending, у замороженного девайсов нет —
    # оба иначе видели бы голый прайс.
    has_link = flags.has_subscription or flags.has_devices
    trial_total_days = flags.trial_days + flags.ref_days

    lines: list[str] = []
    if trial_available:
        if flags.ref_days > 0:
            offer = (
                f"🎁 <b>Сначала {plural_days(trial_total_days)} бесплатно: "
                "тебя пригласил друг.</b>"
            )
        else:
            offer = f"🎁 <b>Сначала {plural_days(flags.trial_days)} бесплатно.</b>"
        lines += [
            offer,
            "Один тап по кнопке ниже, карта не нужна.",
            "",
            "Ниже тарифы, если захочешь больше устройств или сразу на год.",
            "",
        ]
    elif has_link:
        # Инцидент 2026-08-25: после провала выдачи ссылки юзер тапал
        # «💎 Подписка» и упирался в голый прайс — ни слова о том, что подписка
        # у него уже есть и где взять ссылку. Прайс остаётся (апгрейд/год),
        # но путь к ссылке — первым.
        lines += [
            "У тебя уже есть подписка. Ссылка и всё по ней лежат в личном кабинете.",
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
    if trial_available:
        # Кнопка подарка ПЕРВОЙ строкой клавиатуры — иначе оффер остаётся
        # текстом, а тапабельны только платные варианты. Активация нативная
        # (trial:activate), а не web_app: ЛК у части юзеров не открывается.
        rows.insert(0, [types.InlineKeyboardButton(
            text=f"🎁 Забрать {plural_days(trial_total_days)} бесплатно",
            callback_data="trial:activate",
        )])
    elif has_link:
        # Тому, у кого подписка уже есть, первой строкой — путь к ссылке:
        # ЛК (основное место) и нативная выдача (go:config) как фолбэк на
        # случай, когда ЛК не открывается.
        link_row = []
        if WEBAPP_BASE_URL.startswith("https://"):
            link_row.append(types.InlineKeyboardButton(
                text="🔐 Личный кабинет",
                web_app=types.WebAppInfo(url=WEBAPP_URL),
            ))
        link_row.append(types.InlineKeyboardButton(
            text="🔗 Ссылка для подключения", callback_data="go:config"
        ))
        rows.insert(0, link_row)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=rows)
    await _say("\n".join(lines), reply_markup=keyboard)


# Тексты 409 бэкенда на /api/trial/activate_full (services/trial.py,
# activate_trial_full). LIVE: подписка жива (в т.ч. заморожена), юзеру нужна
# ссылка. USED: бесплатные дни уже были, подписок живых нет, нужен /renew.
# Любой ДРУГОЙ detail в 409 — это str(RuntimeError) провижининга: подписку
# собрать не вышло (нет нод / лимит устройств), бонус откатился вместе с
# транзакцией, и повтор пройдёт. Тексты бота должны эти случаи различать:
# иначе USED попал бы в ветку провижининга с ложным «подарок зачислен».
_TRIAL_409_LIVE_DETAIL = "User already has a live subscription"
_TRIAL_409_USED_DETAIL = "Trial already used"

# 409 бэкенда на покупку другого тарифа, когда живая подписка — неоплаченные
# бесплатные дни (api/invoices.py, ON_TRIAL_DETAIL).
_INVOICE_409_ON_TRIAL_DETAIL = "on trial"

# Фолбэк, когда ссылку в чат прислать не удалось. Обещание ссылки никогда не
# должно обрываться молча (инцидент 2026-08-25): юзер должен знать, где её
# забрать самому — ЛК как основное место, /config как нативный путь.
_SUB_LINK_FALLBACK_TEXT = (
    "Ссылку не удалось прислать сюда. Забери её в личном кабинете "
    "(кнопка внизу) или напиши /config чуть позже."
)
_SUB_LINK_MISSING_HINT = (
    "Если ссылка не пришла, она всегда есть в личном кабинете (кнопка "
    "внизу). Или напиши /config через минуту."
)


async def _try_send_sub_link(
    callback_query: types.CallbackQuery,
    uid: int,
    chat_id: int,
    *,
    intro: str | None = None,
) -> str:
    """Ссылка подписки из callback-пути через тот же ``cmd_config``, что и /config.

    Возвращает ``"sent"`` (ссылка ушла), ``"missing"`` (ссылки нет: cmd_config
    в тихом режиме либо промолчал, либо сказал «ещё создаётся») или
    ``"failed"`` (cmd_config упал — фолбэк на ЛК уже отправлен здесь).

    ``intro`` — заголовок («🎉 Готово! VPN бесплатно на 3 дня.», «У тебя уже
    есть подписка 😉»), который cmd_config клеит к своему первому сообщению:
    «готово» и ссылка приходят одним сообщением, а не двумя. При падении
    выдачи заголовок уходит вместе с фолбэком: юзер должен узнать, что
    бесплатные дни включены, даже если ссылку прислать не вышло (возможный повтор
    заголовка на этом редком пути дешевле, чем его потеря).

    Инцидент 2026-08-25 (user 1000054): RecursionError внутри cmd_config
    улетел в глобальный errors-хендлер, и после «Сейчас пришлю ссылку» юзер
    не получил ничего; повторный тап дал 409 → «подарок уже использован» +
    прайс. Поэтому любое исключение тут ловим и отвечаем фолбэком, а не
    тишиной. ``message`` может быть InaccessibleMessage (>48ч) — тогда путь
    через bot/chat_id.
    """
    bot = callback_query.bot
    try:
        if isinstance(callback_query.message, types.Message):
            delivered = await cmd_config(
                callback_query.message, user_id=uid, quiet_if_missing=True, intro=intro
            )
        else:
            delivered = await cmd_config(
                None, user_id=uid, bot=bot, chat_id=chat_id,
                quiet_if_missing=True, intro=intro,
            )
    except Exception:  # noqa: BLE001 — любой сбой выдачи → фолбэк, не тишина
        logger.exception("sub-link delivery failed for user %s", uid)
        fallback = f"{intro}\n\n{_SUB_LINK_FALLBACK_TEXT}" if intro else _SUB_LINK_FALLBACK_TEXT
        await bot.send_message(
            chat_id, fallback, reply_markup=webapp_inline_keyboard()
        )
        return "failed"
    return "sent" if delivered else "missing"


def _trial_done_intro(data: dict | None) -> str:
    """Заголовок после активации: сколько дней выдано, без даты.

    Дату не пишем нарочно: подписка идёт до конца скрытых суток
    (TRIAL_HIDDEN_HOURS), и дата 4-го дня противоречила бы «3 дня». Числа
    берём из ответа activate_full. ``trial_days`` = null — исторический путь
    (старый бонус 150 ₽ потрачен на обычный тариф), там дней триала нет.
    """
    data = data or {}
    try:
        days = int(data["trial_days"])
    except (KeyError, TypeError, ValueError):
        return "🎉 Готово! Подписка активирована."
    try:
        ref_days = max(0, int(data.get("referral_days") or 0))
    except (TypeError, ValueError):
        ref_days = 0
    if ref_days > 0:
        return (
            f"🎉 Готово! VPN бесплатно на {plural_days(days + ref_days)}: "
            f"{plural_days(days)} дарим всем и ещё {ref_days}, потому что тебя "
            "пригласил друг."
        )
    return f"🎉 Готово! VPN бесплатно на {plural_days(days)}."


@router.callback_query(F.data == "trial:activate")
async def trial_activate_cb(callback_query: types.CallbackQuery):
    """Нативная активация бесплатных дней прямо в боте.

    Раньше единственным путём была web_app-кнопка на ЛК — а ЛК у части
    юзеров не открывается, и обещанный подарок одним тапом был
    недостижим (аудит 2026-08-21, паритет A, критично). Эндпоинт
    /api/trial/activate_full и кабинет зовут один сервис
    (trial.activate_trial_full), дрейфа нет.
    """
    await callback_query.answer()
    uid = callback_query.from_user.id
    chat_id = (
        callback_query.message.chat.id
        if isinstance(callback_query.message, types.Message)
        else uid
    )
    bot = callback_query.bot

    # activate_full = бонус + сразу подписка одним вызовом. Одношаговый
    # /api/trial/activate давал только деньги на баланс, а потратить их в
    # боте нечем — юзер застревал на бонусе с ложным обещанием ссылки
    # (ревью 2026-08-21, critical; та же воронка-ловушка из анализа 2026-07).
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/trial/activate_full",
            json={"telegram_id": str(uid)},
            headers=_admin_headers(uid),
        )
    except aiohttp.ClientError:
        status_code, data = 0, None

    if status_code == 200:
        # «Готово» не шлём отдельным сообщением: это заголовок к ссылке (или
        # к честному «ещё создаётся» на cold-пути), cmd_config клеит его к
        # своему первому сообщению. Абзацы про «около минуты» и ЛК/config
        # убраны: первое верно только на cold-пути и там уже есть, второе
        # дублировало кнопки клавиатуры (жалоба владельца 28.08: 7 сообщений
        # за один тап).
        # Ссылку выдаёт тот же код, что /config: единая точка правды. Тихий
        # режим: «нет активных подписок / /plans» сразу после «Готово!» был
        # бы ложью — вместо него подсказка про ЛК и /config.
        outcome = await _try_send_sub_link(
            callback_query, uid, chat_id,
            intro=_trial_done_intro(data if isinstance(data, dict) else None),
        )
        if outcome == "missing":
            await bot.send_message(
                chat_id, _SUB_LINK_MISSING_HINT, reply_markup=webapp_inline_keyboard()
            )
        return
    if status_code == 409:
        detail = str(data.get("detail") or "") if isinstance(data, dict) else ""
        if detail == _TRIAL_409_LIVE_DETAIL:
            # Подписка уже есть — юзеру нужна ссылка, а не прайс. Ровно этот
            # экран видел user 1000054 после провала первой выдачи: «подарок
            # уже использован» + тарифы, хотя подписка у него уже была.
            # Шапка едет заголовком первого сообщения cmd_config, не отдельно.
            outcome = await _try_send_sub_link(
                callback_query, uid, chat_id, intro="У тебя уже есть подписка 😉"
            )
            if outcome == "missing":
                await bot.send_message(
                    chat_id,
                    "Если ссылка не пришла, она есть в личном кабинете (кнопка "
                    "внизу). Статус подписки: /status, ссылка позже: /config.",
                    reply_markup=webapp_inline_keyboard(),
                )
            return
        if detail == _TRIAL_409_USED_DETAIL:
            # Бесплатные дни уже были, живой подписки нет (старая кнопка в
            # чате после истечения). Новой подписки бэкенд не выдаёт: путь
            # один, продление прежней или покупка.
            await bot.send_message(
                chat_id,
                "Бесплатные дни уже использованы. Продлить прежнюю подписку: "
                "/renew, все тарифы: /plans.",
            )
            return
        if detail:
            # str(RuntimeError) провижининга: подписку собрать не вышло, бонус
            # откатился вместе с транзакцией, повтор пройдёт. Прайс тут
            # бесполезен: покупка упрётся в то же самое.
            await bot.send_message(
                chat_id,
                "Не получилось собрать подписку 😔 Подарок остаётся за тобой: "
                "попробуй ещё раз через пару минут или напиши /help.",
            )
            return
        # detail неизвестен (не-JSON ответ и т.п.) — безопасный дефолт:
        # сначала пробуем отдать ссылку, прайс только если её нет.
        if await _try_send_sub_link(callback_query, uid, chat_id) != "missing":
            return
        await bot.send_message(
            chat_id,
            "Подарок уже был использован, либо у тебя уже есть активная "
            "подписка 😉 Ссылка для подключения лежит в личном кабинете (кнопка "
            "внизу) и приходит по команде /config. Тарифы:",
        )
        if isinstance(callback_query.message, types.Message):
            await list_plans(callback_query.message, user_id=uid)
        else:
            await list_plans(None, user_id=uid, bot=bot, chat_id=chat_id)
        return
    if status_code == 402:
        await bot.send_message(
            chat_id,
            "Бонус уже потрачен, включить бесплатные дни с него не получилось. "
            "Выбери тариф: /plans, или загляни в баланс: /balance.",
        )
        return
    if status_code == 404:
        await bot.send_message(
            chat_id, "Не нашёл твой аккаунт. Нажми /start и попробуй ещё раз."
        )
        return
    if status_code == 503:
        await bot.send_message(
            chat_id,
            "Сейчас большой наплыв — попробуй через пару минут, подарок "
            "никуда не денется 😊",
        )
        return
    await bot.send_message(
        chat_id,
        "Не получилось активировать подарок 😔 Попробуй позже или напиши в "
        "поддержку: /help.",
    )


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
    await callback_query.message.answer(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="💎 К тарифам", callback_data="go:plans")
        ]]),
    )


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

    if status == 409:
        # Живая подписка другого плана: раньше создавалась ВТОРАЯ
        # параллельная с двойным списанием (аудит 2026-08-21, C1).
        await callback_query.answer()
        msg = callback_query.message
        detail = str(invoice.get("detail") or "") if isinstance(invoice, dict) else ""
        if detail == _INVOICE_409_ON_TRIAL_DETAIL:
            # Живая подписка — неоплаченные бесплатные дни: «продли через
            # /renew» тут не к месту. Смена тарифа в кабинете идёт через
            # confirm(), который в Telegram подавлен, поэтому /help обязателен
            # как запасной путь.
            text = (
                "Сейчас у тебя бесплатные дни на Solo. Другой тариф можно взять "
                "в личном кабинете, неиспользованные дни зачтутся, или напиши "
                "/help, поменяем вручную."
            )
        else:
            text = (
                "У тебя уже есть активная подписка на другой тариф.\n"
                "Продлить её: /renew\n"
                "Сменить тариф с перерасчётом можно в личном кабинете, либо "
                "напиши в поддержку (/help) и мы поменяем вручную."
            )
        if isinstance(msg, types.Message):
            await msg.answer(text)
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
    # Сырой pay_url в тексте не показываем: у lava.top он с километровым
    # paymentParams-блобом и занимает пол-экрана. Прячем в гиперссылку;
    # html.escape обязателен — в URL есть &, Telegram-HTML разворачивает
    # entities и внутри href.
    link_text = (
        f'Оплатить можно <a href="{html.escape(pay_url, quote=True)}">'
        f"по этой ссылке</a> ({_provider_label(provider)})."
    )

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
            disable_web_page_preview=True,
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
        # сообщение слишком старое для edit — шлём ссылку новым сообщением,
        # с той же кнопкой, что и в edit-пути.
        await msg.answer(
            link_text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[pay_row]),
            disable_web_page_preview=True,
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
        "Ссылка для подключения: в личном кабинете (кнопка ниже) или по "
        "команде /config.",
        reply_markup=onboarding_keyboard(),
    )


# ── /config — получить конфиг подключения ──

@router.message(F.text == "Мой конфиг")
@router.message(Command("config"))
async def cmd_config(
    message: types.Message | None,
    user_id: int | None = None,
    *,
    bot: "types.Bot | None" = None,
    chat_id: int | None = None,
    quiet_if_missing: bool = False,
    intro: str | None = None,
) -> bool:
    """Ссылка подписки. Возвращает True, если ссылка отправлена.

    ``bot``/``chat_id`` — путь для callback'ов на сообщениях старше 48ч
    (InaccessibleMessage без ``.answer``).

    ``intro`` — заголовок от вызывающего («🎉 Готово! VPN бесплатно на 3 дня.»),
    который клеится к ПЕРВОМУ отправленному сообщению, каким бы оно ни было:
    шапка со ссылкой, «⏳ ещё создаётся», «❄️ заморожена», «сервис недоступен».
    Так «готово» и ссылка приходят одним сообщением вместо двух. Если в тихом
    режиме слать нечего, заголовок уходит сам по себе — факт активации юзер
    должен увидеть в любом случае.

    ``quiet_if_missing=True`` — НЕ слать «нет активных подписок / /plans»,
    только вернуть False: вызывающий сам решает, что показать
    (trial_activate_cb: после «Готово!» такой текст был бы ложью, после 409 —
    прайс уместен только если ссылки правда нет). «⏳ ещё создаётся»,
    «❄️ заморожена» и «сервис недоступен» шлём всегда: это честный статус
    подписки, а не отсылка к покупке.

    Инцидент 2026-08-25 (user 1000054): вложенный ``_say`` при живом
    ``message`` звал САМ СЕБЯ вместо ``message.answer`` (внесено 5d696f6) →
    RecursionError → /config, «Мой конфиг» и ссылка после триала не приходили
    никому. Структурный тест на самовызов вложенных хелперов —
    ``backend/tests/test_bot_sublink_delivery.py``.
    """
    if message is not None:
        bot = message.bot
        chat_id = message.chat.id
    uid = user_id if user_id is not None else message.from_user.id
    # Ячейка «заголовок ещё не ушёл»: список вместо nonlocal, чтобы вложенный
    # хелпер мог её опустошить без объявления.
    intro_pending: list[str] = [intro] if intro else []

    async def _say(text: str, **kwargs):
        if intro_pending:
            text = f"{intro_pending.pop()}\n\n{text}"
        if message is not None:
            await message.answer(text, **kwargs)
        else:
            await bot.send_message(chat_id, text, **kwargs)

    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/users/by_telegram/{uid}",
            headers=_admin_headers(uid),
        )
        # status_code==0 → бэкенд недоступен (_fetch_json уже съел ClientError).
        # Не путаем это с «нет подписок»: иначе во время деплоя платящий юзер
        # видит ложное «у тебя нет подписок».
        if status_code == 0:
            await _say("Сервис временно недоступен. Попробуй позже.")
            return False
        if status_code != 200:
            if not quiet_if_missing:
                await _say("У тебя пока нет активных подписок. Используй /plans для покупки.")
            elif intro_pending:
                await _say(intro_pending.pop())
            return False
    except aiohttp.ClientError:
        await _say("Бэкенд недоступен. Попробуйте позже.")
        return False

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
                        await _say(
                            "⏳ Твой конфиг ещё создаётся. Подожди минуту и попробуй снова."
                        )
                        return False
        if any(sub.get("status") == "frozen" for sub in data):
            # Замороженному раньше писали «нет активных подписок» — ложь,
            # уводившая в прайс: подписка есть, её надо разморозить. Это
            # статус, а не отсылка к покупке, поэтому идёт и в тихом режиме.
            await _say(
                "❄️ Подписка заморожена. Разморозь её через /status, "
                "и ссылка снова заработает."
            )
            return False
        if quiet_if_missing:
            if intro_pending:
                await _say(intro_pending.pop())
            return False
        await _say("У тебя нет активных подписок с готовыми конфигами. Используй /plans.")
        return False

    # /config отдаёт ОДНУ подписочную ссылку, а не список vless://.
    # Клиенты (Hiddify / V2rayNG / Streisand) сами разворачивают её в
    # набор серверов и потом рефрешат при миграциях. Список из 10
    # сырых vless-линков нужен был только как дебажный fallback на
    # случай, когда SUB_LINK_BASE_URL не задан — теперь fallback берём
    # из WEBAPP_BASE_URL, поэтому URL всегда есть.
    # link_token — токен primary-устройства (у новых подписок), а не всей
    # подписки: иначе одна ссылка отдаёт логины всех устройств (sub_links.py).
    sub_token = active_sub.get("link_token") or active_sub.get("sub_token")
    sub_url = _build_sub_url(sub_token)
    if not sub_url:
        # Такое случается только в dev-окружении без WEBAPP_BASE_URL.
        logger.warning(
            "cmd_config: no sub_url for user %s (SUB_LINK_BASE_URL + WEBAPP_BASE_URL оба пусты)",
            uid,
        )
        await _say(
            "Не удалось собрать ссылку подписки. Напиши в поддержку."
        )
        return False

    # Шапка коротко: где ссылка и что с ней делать. Абзацы про импорт «один
    # раз» (теперь в платформенных инструкциях, где он по делу) и про ЛК/config
    # убраны — кнопка ЛК первой строкой в onboarding_keyboard, а menu-button ЛК
    # стоит у поля ввода в любом чате (bot.py: set_chat_menu_button); текстом
    # это повторялось до четырёх раз за один тап (жалоба владельца 28.08).
    await _say(
        "🔗 Твоя ссылка для подключения. Тапни по ней ниже, чтобы скопировать.\n\n"
        "Не знаешь, как настроить? Выбери платформу 👇",
        reply_markup=onboarding_keyboard(),
        parse_mode="HTML",
    )
    await _say(
        f"<code>{sub_url}</code>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return True


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
        if status_code != 200 or not data:
            # Аудит 2026-08-21 №7: голый текст без следующего шага — тупик.
            await message.answer(
                "Подписок пока нет. Выбери тариф кнопкой ниже 👇",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="💎 Тарифы", callback_data="go:plans"
                    )
                ]]),
            )
            return
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
        return

    lines = ["📊 <b>Твои подписки:</b>\n"]
    esc = html.escape
    unfreeze_rows: list[list[types.InlineKeyboardButton]] = []
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
        # Разморозка прямо из бота (паритет с ЛК, аудит 2026-08-21):
        # замороженный без работающего ЛК был заперт до авто-разморозки.
        if sub.get("status") == "frozen" and sub.get("id"):
            unfreeze_rows.append([
                types.InlineKeyboardButton(
                    text=f"🔥 Разморозить {esc(str(sub['plan_name']))}",
                    callback_data=f"unfreeze:{sub['id']}",
                )
            ])
    await message.answer(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=(
            types.InlineKeyboardMarkup(inline_keyboard=unfreeze_rows)
            if unfreeze_rows
            else None
        ),
    )


@router.callback_query(F.data.startswith("unfreeze:"))
async def unfreeze_cb(callback_query: types.CallbackQuery):
    """Разморозить подписку из /status. Владельца сверяет бэкенд."""
    try:
        sub_id = int(callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback_query.answer("Некорректный запрос", show_alert=True)
        return
    await callback_query.answer()
    uid = callback_query.from_user.id
    chat_id = (
        callback_query.message.chat.id
        if isinstance(callback_query.message, types.Message)
        else uid
    )
    try:
        status_code, _data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/subscriptions/{sub_id}/unfreeze",
            json={"telegram_id": str(uid)},
            headers=_admin_headers(uid),
        )
    except aiohttp.ClientError:
        status_code = 0

    if status_code == 200:
        await callback_query.bot.send_message(
            chat_id,
            "🔥 Разморозил! VPN оживёт через минуту-две, ссылка прежняя, "
            "добавлять её заново не нужно. Учти: заморозка даётся один "
            "раз в год, и эта попытка уже использована.",
        )
        return
    if status_code == 409:
        await callback_query.bot.send_message(
            chat_id, "Эта подписка не заморожена. Посмотри статус: /status"
        )
        return
    await callback_query.bot.send_message(
        chat_id, "Не получилось разморозить. Попробуй позже или напиши /help."
    )


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

    # Find active or recently expired subscriptions. Бэкенд отдаёт список без
    # ORDER BY: при старой истёкшей платной подписке и истёкшем триале
    # продлевалась бы случайная. Сначала active, затем самая свежая по id.
    renewable = sorted(
        (sub for sub in data if sub.get("status") in ("active", "expired")),
        key=lambda sub: (sub.get("status") != "active", -(sub.get("id") or 0)),
    )
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
                callback_data=f"auto_renew:{sub['id']}:on",
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
    """Тумблер автопродления в ОБЕ стороны.

    Раньше значение было прошито в True — «включить легко, выключить
    нельзя»: остановить списания можно было только через поддержку
    (аудит 2026-08-21, паритет A, высокий). Старые сообщения со старым
    форматом callback_data (без :on/:off) трактуем как включение.
    """
    parts = callback_query.data.split(":")
    try:
        sub_id = int(parts[1])
    except (ValueError, IndexError):
        await callback_query.answer("Некорректный запрос", show_alert=True)
        return
    enable = len(parts) < 3 or parts[2] != "off"

    try:
        status_code, _ = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/subscriptions/{sub_id}/auto_renew",
            # telegram_id обязателен: callback_data подделываема, бэкенд
            # сверяет владельца подписки (ревью 2026-08-21, IDOR).
            json={
                "auto_renew": enable,
                "telegram_id": str(callback_query.from_user.id),
            },
            headers=_admin_headers(callback_query.from_user.id),
        )
    except aiohttp.ClientError:
        await callback_query.answer("Бэкенд недоступен", show_alert=True)
        return

    if status_code != 200:
        await callback_query.answer(
            "Не удалось изменить автопродление", show_alert=True
        )
        return

    msg = callback_query.message
    if enable:
        await callback_query.answer("Автопродление включено!")
        if isinstance(msg, types.Message):
            await msg.answer(
                "✅ Автопродление включено. Счёт будет создан за 3 дня до "
                "истечения. Передумаешь — выключи кнопкой ниже.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="Выключить автопродление",
                        callback_data=f"auto_renew:{sub_id}:off",
                    )
                ]]),
            )
    else:
        await callback_query.answer("Автопродление выключено")
        if isinstance(msg, types.Message):
            await msg.answer(
                "☑️ Автопродление выключено: списаний больше не будет, "
                "подписка доработает оплаченный срок. Включить обратно "
                "можно кнопкой ниже.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="Включить автопродление",
                        callback_data=f"auto_renew:{sub_id}:on",
                    )
                ]]),
            )


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
        status_code, data = await _fetch_json(
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

    # Ответ бэка на answer=bad по контракту ReportBrokenResponse (action и т.д.),
    # если он тут же починил через ядро self_repair; без активной подписки —
    # просто {ok: true}.
    repair: dict | None = None
    if action == "ok":
        await callback_query.answer("Спасибо! 💛")
        ack_text = (
            "🙌 Спасибо за помощь! Ваш ответ помогает нам "
            "следить за качеством сервиса."
        )
    else:
        await callback_query.answer("Спасибо! Чиним.")
        if isinstance(data, dict) and data.get("action"):
            # Исход починки уйдёт отдельным (не удаляемым) сообщением ниже —
            # тем же, что у кнопки «🆘 VPN не работает»: с вопросом об
            # операторе и отложенным нуджем. Ack остаётся коротким и не
            # противоречит исходу: «чиним» только когда реально починили.
            repair = data
            if data.get("action") in ("migrated", "reshuffled", "duplicated"):
                ack_text = "🛠 Спасибо! Чиним."
            else:
                ack_text = "🛠 Спасибо, сигнал получили."
        else:
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
    # Применяется к обоим веткам (ok/bad): для bad всё важное (что именно
    # сделали, оператор, нудж) живёт в отдельном сообщении исхода
    # (_send_repair_outcome), а не в этом бабле.
    if HEALTH_PING_ACK_DELETE_DELAY_S > 0:
        _spawn(
            _delete_message_after(
                callback_query.bot,
                callback_query.message.chat.id,
                callback_query.message.message_id,
                HEALTH_PING_ACK_DELETE_DELAY_S,
            )
        )
    if repair is not None:
        await _send_repair_outcome(
            callback_query.bot, callback_query.message.chat.id, repair
        )


# Self-report: юзер жмёт «🆘 VPN не работает» в reply-клавиатуре.
# Плановый health-ping приходит юзеру раз в 7-14 дней (random jitter),
# только в обеденное окно МСК (11–14) —
# self-report закрывает эту дыру и позволяет пожаловаться прямо сейчас.
# Бэкенд-вход тот же, что у кабинета и страницы: devices-by-telegram (пре-чек)
# → report-broken-device / report-broken → ядро self_repair (source=
# bot_vpn_broken); health-ping-response отсюда НЕ шлётся.
@router.message(F.text == BTN_VPN_BROKEN)
async def self_report_vpn_broken(message: types.Message) -> None:
    # Порядок: пре-чек повторов → пикер устройств → починка. Повторы ограничивает
    # ТОЛЬКО бэкенд — единая политика ядра self_repair (троттл + суточный потолок
    # по подписке), одна на бот, кабинет и страницу. Клиентского кулдауна в боте
    # больше нет: он был мёртв (серверный пре-чек ниже срабатывал раньше него) и
    # к тому же терялся при рестарте. Живые устройства (active+failed) тянем с
    # бэка: >1 → спрашиваем, КАКОЕ перенести (рабочие не трогаем); ==1 → чиним
    # сразу. Исход показывает _send_repair_outcome; через делей
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
        await message.answer(_REPAIR_RETRY_TEXT)
        return
    if status_code != 200 or not isinstance(data, dict):
        # 4xx/5xx/0 здесь = сбой контракта или недоступность бэка. _fetch_json
        # логирует только >=500 и 0, поэтому 4xx (напр. протухший telegram_id,
        # рассинхрон) иначе исчезал бы без следа — логируем с контекстом.
        logger.warning(
            "self_report devices-by-telegram failed: tg_id=%s status=%s data=%.300s",
            tg_id, status_code, data,
        )
        await message.answer(_REPAIR_RETRY_TEXT)
        return

    # Пре-чек ДО пикера: если только что уже чинили (throttled) или суточный
    # потолок исчерпан (daily_limit), бэкенд отдаёт retry_after_sec + wait_reason,
    # а жалобу уже записал сам (complaint_received) — здесь только текст для
    # человека. Показываем независимо от числа устройств: раньше бот сначала
    # спрашивал «какое не работает?», а потом отвечал «уже перекидывали недавно»
    # — для человека это выглядело как противоречие (репорт 2026-07-26). Тексты
    # те же, что у исхода починки: один источник в _send_repair_outcome.
    retry_after = data.get("retry_after_sec")
    wait_reason = data.get("wait_reason")
    if retry_after or wait_reason:
        await _send_repair_outcome(
            message.bot,
            message.chat.id,
            {"action": wait_reason or "throttled", "retry_after_sec": retry_after},
        )
        return

    devices = data.get("devices") or []
    if not devices:
        if data.get("subscription_id"):
            # Подписка есть, живых устройств нет — все ещё pending (собираются);
            # чинить пока нечего, но и «нет подписки» говорить нельзя.
            await message.answer(
                "⏳ Устройства ещё настраиваются — подожди пару минут и нажми 🔄 "
                "рядом с профилем. Если через 10 минут не заработает — нажми "
                "кнопку ещё раз."
            )
        else:
            await _send_repair_outcome(
                message.bot, message.chat.id, {"action": "no_subscription"}
            )
        return

    if len(devices) > 1:
        # Несколько устройств — спрашиваем, какое перенести (имена = как у
        # юзера записаны). Показ пикера бесплатен: повторы ограничит бэкенд.
        await message.answer(
            "У тебя несколько устройств. Какое не работает? Перенесём только "
            "его — остальные не тронем.",
            reply_markup=broken_device_keyboard(devices),
        )
        return

    await _do_device_failover(
        message.bot, message.chat.id, tg_id, int(devices[0]["device_id"])
    )


# Транспортная ошибка / не-200 от бэка — один текст на все входы починки.
_REPAIR_RETRY_TEXT = "Не получилось обработать — попробуй ещё раз через минуту."

# Хвост успешного исхода: вопрос об операторе (operator-aware routing P1).
_OPERATOR_QUESTION = (
    "\n\nЧтобы мы быстрее ловили блокировки — подскажи, какой у тебя интернет?"
)


def _retry_minutes(retry_after_sec) -> int:
    """retry_after_sec бэка → минуты для «попробуй через N мин.» (вверх, не
    меньше 1). Если бэк число не прислал — 2 мин., как дефолтный троттл ядра
    (SELF_REPAIR_THROTTLE_SEC=120)."""
    try:
        sec = int(retry_after_sec or 0)
    except (TypeError, ValueError):
        sec = 0
    return max(1, math.ceil(sec / 60)) if sec > 0 else 2


def _plans_keyboard() -> types.InlineKeyboardMarkup:
    """«Выбрать тариф» для исхода no_subscription. E1.3: команды /buy в боте нет
    (ни хендлера, ни в списке команд) — человек, который уже жалуется на
    проблему, не должен упираться в тупик."""
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="💎 Выбрать тариф", callback_data="go:plans"),
    ]])


async def _send_repair_outcome(bot, chat_id: int, data: dict) -> str | None:
    """Показать человеку исход починки — ЕДИНСТВЕННОЕ место с текстами исходов
    для всех входов бота: «🆘 VPN не работает» (одно устройство, пикер, «все
    устройства»), «не работает» на плановый опрос (hping:bad) и /newconfig.

    ``data`` — ответ бэка по контракту ReportBrokenResponse (action, report_id,
    device_name, scope, retry_after_sec). Семантика исходов общая с кабинетом и
    страницей (docs/operations/vpn_broken_channels_parity_2026_09_12.md), поэтому
    тексты тут и там должны совпадать по смыслу. После успешного шага
    (migrated/reshuffled/duplicated) спрашиваем оператора (operator_keyboard) и
    ставим отложенный нудж «всё ещё не работает?» (_delayed_still_broken_prompt)
    — если бэк вернул report_id. Возвращает action (None, если его нет)."""
    action = data.get("action")
    report_id = data.get("report_id")

    if action in ("migrated", "reshuffled", "duplicated"):
        if action == "migrated":
            if data.get("scope") == "subscription":
                head = "🔄 Поменяли сервер для всех устройств."
            else:
                # Имя устройства задаёт юзер в webapp — экранируем перед вставкой
                # в HTML-сообщение (default parse_mode=HTML), иначе '<' /
                # несбалансированный тег → 400 «can't parse entities», и юзер не
                # получит ни клавиатуры, ни нуджа.
                dev_name = html.escape(data.get("device_name") or "устройство")
                head = f"🔄 Поменяли сервер для «{dev_name}»."
            text = (
                f"{head} Подписка обновится в приложении сама — нажми 🔄 рядом "
                "с профилем и попробуй подключиться через пару минут."
            )
        elif action == "reshuffled":
            # Первый шаг лестницы: ноды те же, протоколы другие. Человеку про
            # протоколы знать незачем — ему важно «поменяли способ связи и надо
            # обновить подписку».
            text = (
                "🔀 Переключили тебя на другой способ подключения — чаще всего "
                "не работает именно он, а не сам сервер.\n\n"
                "Нажми 🔄 рядом с профилем в приложении и попробуй подключиться."
            )
        else:
            # Третий шаг: человеку с единственным живым протоколом дали второй
            # сервер по нему же — страховка на случай падения его ноды.
            text = (
                "➕ Добавили тебе запасной сервер по тому способу связи, который "
                "у тебя работает.\n\n"
                "Нажми 🔄 рядом с профилем — в списке появится ещё один вариант."
            )
        if report_id:
            await bot.send_message(
                chat_id,
                text + _OPERATOR_QUESTION,
                reply_markup=operator_keyboard(int(report_id)),
            )
            _spawn(_delayed_still_broken_prompt(bot, chat_id, int(report_id)))
        else:
            # Без report_id оператора не к чему привязать, а нудж не проверить
            # (report-status) — оставляем человеку выход в поддержку.
            await bot.send_message(chat_id, text, reply_markup=help_keyboard())
        return action

    if action == "throttled":
        # Штатный троттл — не отказ сервиса: раньше он проваливался в «не смогли
        # подобрать сервер, напиши в поддержку» и гнал человека в поддержку
        # (репорт 2026-07-26). Кнопка доступна сразу — сервер сам ответит
        # throttled, никаких клиентских блокировок.
        mins = _retry_minutes(data.get("retry_after_sec"))
        await bot.send_message(
            chat_id,
            "👍 Мы уже переключали тебя пару минут назад. Нажми 🔄 рядом с "
            "профилем и попробуй подключиться. "
            f"Если через {mins} мин. всё ещё не работает — нажми кнопку ещё раз.",
        )
    elif action == "daily_limit":
        await bot.send_message(
            chat_id,
            "Сегодня мы уже несколько раз меняли тебе серверы — дальше нужна "
            "помощь человека. Напиши в поддержку.",
            reply_markup=help_keyboard(),
        )
    elif action == "not_ready":
        # Устройство pending — ещё собирается; переносить нечего.
        await bot.send_message(
            chat_id,
            "⏳ Устройство ещё настраивается — подожди пару минут и нажми 🔄 "
            "рядом с профилем. Если через 10 минут не заработает — нажми кнопку "
            "ещё раз.",
        )
    elif action == "no_subscription":
        # Нет активной подписки / устройство отключено / бан — ядро отвечает
        # одинаково, деталей человеку не раскрываем.
        await bot.send_message(
            chat_id,
            "Чинить нечего: подписка не активна или это устройство уже "
            "отключено. Проверь подписку в кабинете.",
            reply_markup=_plans_keyboard(),
        )
    elif action == "user_not_found":
        await bot.send_message(chat_id, "Не нашли твой аккаунт — нажми /start.")
    elif action == "no_target":
        await bot.send_message(
            chat_id,
            "Не смогли автоматически подобрать другой сервер. Попробуй через "
            "10 минут — серверы освобождаются постоянно. Если срочно — напиши "
            "в поддержку.",
            reply_markup=help_keyboard(),
        )
    else:
        # Неизвестный action / сбой контракта — как транспортная ошибка, но с
        # выходом в поддержку (аудит №12: юзер со сломанным VPN и сбоящим бэком
        # не должен оставаться без единого выхода).
        logger.warning("repair outcome: unknown action=%r data=%.300s", action, data)
        await bot.send_message(chat_id, _REPAIR_RETRY_TEXT, reply_markup=help_keyboard())
    return action


async def _do_device_failover(
    bot, chat_id: int, tg_id: int, device_id: int
) -> str | None:
    """Починить ОДНО устройство (report-broken-device → ядро self_repair
    per-device): соседние устройства не трогаются, нода user-wide не банится.
    Исход показывает _send_repair_outcome; возвращает action (None при
    транспортной ошибке / не-200)."""
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/admin/client-control/report-broken-device",
            json={"telegram_id": str(tg_id), "device_id": device_id},
            headers=_admin_headers(tg_id),
        )
    except aiohttp.ClientError:
        # Аудит №12: юзер со сломанным VPN и сбоящим бэком не должен
        # оставаться без единого выхода — даём клавиатуру помощи.
        await bot.send_message(chat_id, _REPAIR_RETRY_TEXT, reply_markup=help_keyboard())
        return None
    if status_code != 200 or not isinstance(data, dict):
        # Персистентный 4xx (баг контракта/рассинхрон устройств) иначе гнал бы
        # юзера в бесконечный «попробуй через минуту» без единой строки в логах.
        logger.warning(
            "report-broken-device failed: tg_id=%s device_id=%s status=%s data=%.300s",
            tg_id, device_id, status_code, data,
        )
        await bot.send_message(chat_id, _REPAIR_RETRY_TEXT, reply_markup=help_keyboard())
        return None
    return await _send_repair_outcome(bot, chat_id, data)


async def _do_whole_sub_failover(bot, chat_id: int, tg_id: int) -> str | None:
    """«🔁 Все мои устройства» и /newconfig — whole-sub путь (report-broken →
    handle_broken_subscription): переезжает вся подписка, старая нода банится
    user-wide. Повторы ограничивает та же единая политика ядра, что и
    per-device (троттл + суточный потолок по подписке). Исход показывает
    _send_repair_outcome (scope="subscription" → «для всех устройств»)."""
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/admin/client-control/report-broken",
            json={"telegram_id": str(tg_id)},
            headers=_admin_headers(tg_id),
        )
    except aiohttp.ClientError:
        await bot.send_message(chat_id, _REPAIR_RETRY_TEXT, reply_markup=help_keyboard())
        return None
    if status_code != 200 or not isinstance(data, dict):
        logger.warning(
            "report-broken (whole-sub) failed: tg_id=%s status=%s data=%.300s",
            tg_id, status_code, data,
        )
        await bot.send_message(chat_id, _REPAIR_RETRY_TEXT, reply_markup=help_keyboard())
        return None
    return await _send_repair_outcome(bot, chat_id, data)


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
    «🔁 Все мои устройства» — whole-sub перенос всей подписки для тех, у кого
    лёг весь пул (_do_whole_sub_failover)."""
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

    # Пополнение прямо в боте (аудит 2026-08-21, паритет A, критично):
    # раньше была только кнопка ЛК, а без баланса мертвы автосписания и
    # доплаты. Пресеты создают topup-счёт и ведут в обычное меню оплаты.
    kb_rows = [[
        types.InlineKeyboardButton(text="➕ 200 ₽", callback_data="topup:20000"),
        types.InlineKeyboardButton(text="➕ 500 ₽", callback_data="topup:50000"),
        types.InlineKeyboardButton(text="➕ 1000 ₽", callback_data="topup:100000"),
    ]]
    webapp_kb = webapp_inline_keyboard()
    if webapp_kb is not None:
        kb_rows.extend(webapp_kb.inline_keyboard)
    await target.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.callback_query(F.data.startswith("topup:"))
async def topup_preset(callback_query: types.CallbackQuery):
    """Создать счёт на пополнение и показать меню способов оплаты."""
    try:
        amount_kopecks = int(callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback_query.answer("Некорректная сумма", show_alert=True)
        return
    await callback_query.answer()
    uid = callback_query.from_user.id
    chat_id = (
        callback_query.message.chat.id
        if isinstance(callback_query.message, types.Message)
        else uid
    )

    try:
        status_code, invoice = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/invoices/topup",
            json={"telegram_id": str(uid), "amount_kopecks": amount_kopecks},
            headers=_admin_headers(uid),
        )
    except aiohttp.ClientError:
        status_code, invoice = 0, None

    if status_code != 200 or not invoice:
        await callback_query.bot.send_message(
            chat_id, "Не удалось создать счёт. Попробуй позже."
        )
        return

    rub = amount_kopecks // 100
    await callback_query.bot.send_message(
        chat_id,
        f"💳 Пополнение баланса на {rub} ₽ (счёт #{invoice['id']}).\n"
        "Выбери способ оплаты 👇 Деньги упадут на баланс сразу после "
        "подтверждения.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=_payment_method_rows(invoice["id"], "top")
        ),
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


def _referral_terms(data: dict) -> str:
    """Условия рефералки для /referral: «Друг получит 6 дней VPN бесплатно
    вместо 3, а тебе начислим 10 дней подписки, когда он впервые оплатит.»

    Бесплатные дни без приглашения = ``invitee_total_days − bonus_days``. Если
    поля нет или числа не сходятся (старый бэкенд, код без подарка), «вместо N»
    не пишем: лучше без сравнения, чем с ложным.
    """
    def _int(key: str) -> int | None:
        try:
            return int(data.get(key))
        except (TypeError, ValueError):
            return None

    total = _int("invitee_total_days")
    bonus = _int("bonus_days")
    reward = _int("reward_days")
    if total and total > 0:
        friend = f"Друг получит {plural_days(total)} VPN бесплатно"
        if bonus and 0 < bonus < total:
            friend += f" вместо {total - bonus}"
    else:
        friend = "Друг получит бесплатные дни VPN"
    if reward and reward > 0:
        mine = f"тебе начислим {plural_days(reward)} подписки"
    else:
        mine = "тебе начислим дни подписки"
    return f"{friend}, а {mine}, когда он впервые оплатит."


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

    # Числа из /api/referral/code: сколько бесплатных дней получит друг (3 + 3)
    # и сколько дней подписки начислим пригласившему. Прежний текст «+50 ₽ /
    # +50 ₽» был ложным: подарок другу в днях, награда платится за первую
    # оплату друга любым способом (invoices._maybe_pay_referrer).
    await msg.answer(
        f"🎁 <b>Твоя реферальная ссылка:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"{_referral_terms(data)}\n"
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
    """/newconfig — «переселить всё»: тот же whole-sub путь, что «🔁 Все мои
    устройства» в пикере кнопки «🆘 VPN не работает» (единые тексты исходов,
    вопрос об операторе, отложенный нудж, единая политика повторов). Раньше
    команда дёргала отдельный regenerate-эндпоинт со своими текстами
    («Конфиг перегенерирован») мимо всего этого."""
    await _do_whole_sub_failover(
        message.bot, message.chat.id, message.from_user.id
    )


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
                text="✅ Счёт оплачен! Конфиг будет готов через минуту. "
                "Ссылка для подключения: в личном кабинете или по команде /config.",
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


# «О сервисе и документы»: кто мы, что продаём и где лежат оферта, политика
# возвратов и политика ПД. Сами документы — на сайте (LEGAL_BASE_URL), бот
# только ведёт к ним; реквизиты исполнителя — внутри документов, в чат их
# не дублируем (решение владельца 2026-09-22).
_HELP_ABOUT = (
    "<b>V8 VPN</b> — подписка на защищённое соединение через наши серверы "
    "за рубежом. Оплата, выдача доступа и поддержка — прямо здесь, в боте "
    "и мини-приложении.\n\n"
    "Документы сервиса — по кнопкам ниже. Вопросы и возвраты — через "
    "«Связаться с поддержкой»."
)


@router.callback_query(F.data == "help:about")
async def help_about(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.answer(
        _HELP_ABOUT, reply_markup=about_keyboard(LEGAL_BASE_URL)
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
    flags = await _fetch_user_flags(callback_query.from_user.id)
    welcome = format_welcome(
        first_name,
        is_new=False,
        trial_available=flags.trial_available,
        trial_days=flags.trial_days,
        ref_days=flags.ref_days,
    )
    await _send_welcome_pair(
        callback_query.message.answer,
        welcome=welcome,
        trial_available=flags.trial_available,
        is_new=False,
        has_devices=flags.has_devices,
        has_link=flags.has_subscription or flags.has_devices,
        trial_days=flags.trial_days + flags.ref_days,
    )


@router.callback_query(F.data == "go:plans")
async def go_plans(callback_query: types.CallbackQuery):
    """Inline shortcut to /plans.

    telegram_id инициатора передаём явно: ``message.from_user`` тут — сам
    бот, и триал-флаги считались бы по нему (ложный оффер подарка). Для
    сообщений старше 48ч (InaccessibleMessage) шлём прайс новым сообщением.
    """
    await callback_query.answer()
    msg = callback_query.message
    if isinstance(msg, types.Message):
        await list_plans(msg, user_id=callback_query.from_user.id)
    else:
        await list_plans(
            None,
            user_id=callback_query.from_user.id,
            bot=callback_query.bot,
            chat_id=callback_query.from_user.id,
        )


@router.callback_query(F.data == "go:config")
async def go_config(callback_query: types.CallbackQuery):
    """Inline-шорткат к /config («🔗 Ссылка для подключения»).

    Кнопка стоит на welcome/прайсе у юзера с устройствами — чтобы ссылку
    можно было забрать ПОТОМ, если сразу не отдало (инцидент 2026-08-25).
    telegram_id инициатора передаём явно: ``message.from_user`` тут — бот.
    """
    await callback_query.answer()
    msg = callback_query.message
    if isinstance(msg, types.Message):
        await cmd_config(msg, user_id=callback_query.from_user.id)
    else:
        await cmd_config(
            None,
            user_id=callback_query.from_user.id,
            bot=callback_query.bot,
            chat_id=callback_query.from_user.id,
        )


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
    if not isinstance(callback_query.message, types.Message):
        # Сообщение старше 48ч: edit невозможен (аудит №6, AttributeError).
        await callback_query.answer(
            "План устарел, вызови /ops заново.", show_alert=True
        )
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
    if not isinstance(callback_query.message, types.Message):
        await callback_query.answer(
            "План устарел, вызови /ops заново.", show_alert=True
        )
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
    if not isinstance(callback_query.message, types.Message):
        await callback_query.answer(
            "План устарел, вызови /ops заново.", show_alert=True
        )
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


# ── Устройства (паритет с ЛК, аудит 2026-08-21) ──

# Отдельный роутер ТОЛЬКО для стейт-хэндлеров переименования: он
# подключается в bot.py ДО главного (как support_router), иначе ранние
# команды/кнопки главного роутера бьют раньше StateFilter, стейт
# остаётся липким, и следующий свободный текст молча переименовывает
# устройство (ревью 2026-08-25).
devices_router = Router()


class DeviceStates(StatesGroup):
    waiting_rename = State()


async def _send_devices(bot: "types.Bot", chat_id: int, uid: int) -> None:
    """Экран «Мои устройства»: список + переименовать/удалить/добавить."""
    try:
        status_code, data = await _fetch_json(
            "GET",
            f"{BACKEND_URL}/api/users/by_telegram/{uid}",
            headers=_admin_headers(uid),
        )
    except aiohttp.ClientError:
        status_code, data = 0, None
    if status_code == 0:
        await bot.send_message(chat_id, "Сервис временно недоступен. Попробуй позже.")
        return
    if status_code != 200 or not data:
        await bot.send_message(
            chat_id,
            "Подписок пока нет. Выбери тариф: /plans",
        )
        return

    sub = next((s for s in data if s.get("status") == "active"), None)
    if sub is None:
        await bot.send_message(
            chat_id, "Нет активной подписки. Статус: /status, тарифы: /plans"
        )
        return

    live = [
        d
        for d in (sub.get("devices") or [])
        if d.get("status") not in ("revoked", "disabled")
    ]
    esc = html.escape
    lines = [f"📱 <b>Устройства</b> · план {esc(str(sub.get('plan_name', '')))}\n"]
    rows: list[list[types.InlineKeyboardButton]] = []
    for i, d in enumerate(live, 1):
        name = esc(str(d.get("name") or f"устройство {i}"))
        lines.append(f"{i}. {name} · {esc(str(d.get('status', '')))}")
        rows.append([
            types.InlineKeyboardButton(
                text=f"✏️ {i}", callback_data=f"devren:{d['id']}"
            ),
            types.InlineKeyboardButton(
                text=f"🗑 {i}", callback_data=f"devrm:{d['id']}"
            ),
        ])
    if not live:
        lines.append("Живых устройств нет.")
    # Не «ссылка общая, бери из /config»: /config ведёт на первое устройство,
    # и новый телефон сел бы на его логин. У каждого устройства своя ссылка
    # (разбор user 1000076, 28.09.2026).
    lines.append(
        "\nОдно устройство, одна строка. У каждого устройства своя ссылка, "
        "все они в личном кабинете."
    )
    rows.append([
        types.InlineKeyboardButton(
            text="➕ Добавить устройство", callback_data=f"devadd:{sub['id']}"
        )
    ])
    await bot.send_message(
        chat_id,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(Command("devices"))
async def cmd_devices(message: types.Message):
    await _send_devices(message.bot, message.chat.id, message.from_user.id)


@router.callback_query(F.data.startswith("devadd:"))
async def device_add_cb(callback_query: types.CallbackQuery):
    try:
        sub_id = int(callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback_query.answer("Некорректный запрос", show_alert=True)
        return
    await callback_query.answer()
    uid = callback_query.from_user.id
    chat_id = (
        callback_query.message.chat.id
        if isinstance(callback_query.message, types.Message)
        else uid
    )
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/bot/subscriptions/{sub_id}/add_device",
            json={"telegram_id": str(uid)},
            headers=_admin_headers(uid),
        )
    except aiohttp.ClientError:
        status_code, data = 0, None

    if status_code == 200 and isinstance(data, dict):
        charged = int(data.get("charged_kopecks") or 0)
        fee_note = (
            f" Списано {charged // 100} ₽ за дополнительный слот." if charged else ""
        )
        # Ссылка именно НОВОГО устройства. Раньше бот отсылал к «той же
        # ссылке из /config», но /config ведёт на первое устройство — второй
        # телефон сел бы на его логин (разбор user 1000076, 28.09.2026).
        # sub_url — ровно тот URL, что кабинет покажет в строке устройства
        # (домен тоже: у старых подписок кабинет держит один основной домен).
        # Относительный (без SUB_LINK_BASE_URL) в Telegram не кликается.
        ready = data.get("sub_url")
        new_url = (
            ready
            if isinstance(ready, str) and ready.startswith(("https://", "http://"))
            else _build_sub_url(data.get("sub_token"))
        )
        if new_url:
            await callback_query.bot.send_message(
                chat_id,
                f"✅ Устройство добавлено (всего {data.get('device_count')})."
                f"{fee_note}\nНа новом устройстве импортируй эту ссылку, "
                "она только для него:",
            )
            await callback_query.bot.send_message(
                chat_id,
                f"<code>{new_url}</code>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            await callback_query.bot.send_message(
                chat_id,
                f"✅ Устройство добавлено (всего {data.get('device_count')})."
                f"{fee_note}\nСсылка для него лежит в личном кабинете.",
            )
        await _send_devices(callback_query.bot, chat_id, uid)
        return
    if status_code == 402:
        detail = (data or {}).get("detail") or {}
        hint = detail.get("hint") if isinstance(detail, dict) else None
        need = (
            int(detail.get("suggested_topup_kopecks") or 0) // 100
            if isinstance(detail, dict)
            else 0
        )
        await callback_query.bot.send_message(
            chat_id,
            (hint or "Не хватает баланса на дополнительное устройство.")
            + (f"\nПополнить на {need} ₽ можно тут: /balance" if need else "\nПополнить: /balance"),
        )
        return
    detail = (data or {}).get("detail") if isinstance(data, dict) else None
    await callback_query.bot.send_message(
        chat_id,
        f"Не получилось добавить: {html.escape(str(detail))}"
        if detail
        else "Не получилось добавить устройство. Попробуй позже.",
    )


@router.callback_query(F.data.startswith("devrm:"))
async def device_remove_confirm(callback_query: types.CallbackQuery):
    try:
        dev_id = int(callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback_query.answer("Некорректный запрос", show_alert=True)
        return
    await callback_query.answer()
    msg = callback_query.message
    if isinstance(msg, types.Message):
        await msg.answer(
            "Удалить это устройство? VPN на нём отключится.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(
                    text="✅ Да, удалить", callback_data=f"devrmok:{dev_id}"
                ),
                types.InlineKeyboardButton(
                    text="✖️ Отмена", callback_data="devcancel"
                ),
            ]]),
        )


@router.callback_query(F.data == "devcancel")
async def device_cancel_cb(callback_query: types.CallbackQuery):
    await callback_query.answer("Ок, не трогаю")
    msg = callback_query.message
    if isinstance(msg, types.Message):
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("devrmok:"))
async def device_remove_cb(callback_query: types.CallbackQuery):
    try:
        dev_id = int(callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback_query.answer("Некорректный запрос", show_alert=True)
        return
    await callback_query.answer()
    uid = callback_query.from_user.id
    chat_id = (
        callback_query.message.chat.id
        if isinstance(callback_query.message, types.Message)
        else uid
    )
    try:
        status_code, data = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/bot/devices/{dev_id}/remove",
            json={"telegram_id": str(uid)},
            headers=_admin_headers(uid),
        )
    except aiohttp.ClientError:
        status_code, data = 0, None

    if status_code == 200:
        await callback_query.bot.send_message(chat_id, "🗑 Устройство удалено.")
        await _send_devices(callback_query.bot, chat_id, uid)
        return
    detail = (data or {}).get("detail") if isinstance(data, dict) else None
    await callback_query.bot.send_message(
        chat_id,
        f"Не получилось удалить: {html.escape(str(detail))}"
        if detail
        else "Не получилось удалить устройство. Попробуй позже.",
    )


@router.callback_query(F.data.startswith("devren:"))
async def device_rename_start(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        dev_id = int(callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback_query.answer("Некорректный запрос", show_alert=True)
        return
    await callback_query.answer()
    await state.set_state(DeviceStates.waiting_rename)
    await state.update_data(rename_device_id=dev_id)
    msg = callback_query.message
    target_chat = (
        msg.chat.id if isinstance(msg, types.Message) else callback_query.from_user.id
    )
    await callback_query.bot.send_message(
        target_chat,
        "Введи новое имя устройства (до 64 символов).\nОтменить: /cancel",
    )


@devices_router.message(StateFilter(DeviceStates.waiting_rename), Command("cancel"))
async def device_rename_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, переименование отменено.")


@devices_router.message(StateFilter(DeviceStates.waiting_rename), F.text)
async def device_rename_finish(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text.startswith("/"):
        # Любая команда в стейте (роутер стоит раньше главного) — выход
        # из переименования; юзер повторит команду уже вне стейта.
        await state.clear()
        await message.answer("Ок, переименование отменено. Повтори команду.")
        return
    # Кнопки главного меню — тоже не имя устройства: devices_router стоит
    # раньше главного, и «🆘 VPN не работает», нажатая в стейте переименования,
    # молча становилась именем устройства, а жалоба терялась. Выходим из стейта;
    # жалобу на VPN обрабатываем сразу — человек с нерабочим VPN не должен
    # жать дважды. Импорт ленивый, как SupportStates в cmd_start: модульный
    # импорт bot.support тянет aiogram.exceptions.TelegramForbiddenError,
    # которого нет в тестовых заглушках aiogram (backend/tests/_bot_stubs.py).
    from .support import _MENU_TEXTS

    if text in _MENU_TEXTS:
        await state.clear()
        if text == BTN_VPN_BROKEN:
            await self_report_vpn_broken(message)
            return
        await message.answer("Ок, переименование отменено. Нажми кнопку ещё раз.")
        return
    data = await state.get_data()
    dev_id = data.get("rename_device_id")
    await state.clear()
    if not dev_id:
        await message.answer("Сессия потеряна. Открой /devices и попробуй ещё раз.")
        return
    uid = message.from_user.id
    try:
        status_code, resp = await _fetch_json(
            "POST",
            f"{BACKEND_URL}/api/bot/devices/{dev_id}/rename",
            json={"telegram_id": str(uid), "name": text[:64]},
            headers=_admin_headers(uid),
        )
    except aiohttp.ClientError:
        status_code, resp = 0, None
    if status_code == 200 and isinstance(resp, dict):
        await message.answer(
            f"✏️ Готово: {html.escape(str(resp.get('name')))}"
        )
        await _send_devices(message.bot, message.chat.id, uid)
        return
    await message.answer("Не получилось переименовать. Попробуй позже.")


# ── Легаси-кнопки старожилов ──


@router.message(F.text.startswith(("Basic ", "Pro ")))
async def legacy_tariff_buttons(message: types.Message):
    """Персистентная reply-клавиатура «Basic 1m / Pro 12m» слалась до
    2025-11-26 (мёртвый tariff_keyboard, удалён) и живёт у старожилов в
    чатах вечно — тап по ней был тишиной (аудит 2026-08-21, №5)."""
    await list_plans(message)


# ── Глобальные страховки диалога (аудит 2026-08-21, тупики №1/№4) ──
# Регистрируются ПОСЛЕДНИМИ в файле намеренно: aiogram матчит хэндлеры в
# порядке регистрации, и всё осмысленное должно перехватиться выше.


@router.message(Command("cancel"))
async def cmd_cancel_global(message: types.Message, state: FSMContext):
    """/cancel вне FSM-стейтов: раньше он молча проглатывался.

    В стейтах поддержки /cancel обрабатывают хэндлеры support_router
    (он подключён раньше) — сюда доходит только «отменять нечего».
    """
    await state.clear()
    flags = await _fetch_user_flags(message.from_user.id)
    await message.answer(
        "Ок, отменил. Выбери действие кнопкой ниже 👇",
        reply_markup=start_keyboard(has_devices=flags.has_devices),
    )


@router.message()
async def fallback_unknown(message: types.Message, state: FSMContext):
    """Catch-all: нераспознанный текст больше не тонет в тишине.

    Главный клиент — юзер, чей диалог поддержки умер вместе с рестартом
    бота (FSM в MemoryStorage): раньше его сообщение не матчилось НИ
    ОДНИМ хэндлером, и человек писал в пустоту. Стейт на всякий случай
    чистим: живой стейт до сюда не доходит (support_router раньше).
    """
    await state.clear()
    flags = await _fetch_user_flags(message.from_user.id)
    await message.answer(
        "Не понял 🤔 Выбери действие кнопкой ниже.\n"
        "Если писал в поддержку и видишь это, нажми «❓ Помощь» и отправь "
        "сообщение ещё раз.",
        reply_markup=start_keyboard(has_devices=flags.has_devices),
    )
