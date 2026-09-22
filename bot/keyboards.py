import os

from aiogram import types

# Public HTTPS URL of the Telegram WebApp. Used to build the inline
# "Личный кабинет" button on /start. The persistent entry point is the
# Menu Button: изначально задана в BotFather, но с появлением кэшбастера
# бот на старте перезаписывает её программно (bot.py:set_chat_menu_button),
# чтобы URL нёс актуальную версию.
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "").rstrip("/")

# Кэшбастер мини-аппа: query с версией выката. HTTP-кэш Telegram-webview
# ключуется полным URL (включая query), поэтому смена версии гарантированно
# приводит клиента за свежим index.html — эвристически закэшированный старый
# URL просто не участвует. Слэш перед query обязателен: /app?v=… ловит
# лишний 301 на /app/?v=…. Во все WebAppInfo должен идти WEBAPP_URL;
# WEBAPP_BASE_URL остаётся для https-гейтов и построения origin-ссылок.
APP_VERSION = os.getenv("APP_VERSION", "").strip()
WEBAPP_URL = (
    f"{WEBAPP_BASE_URL}/?v={APP_VERSION}"
    if WEBAPP_BASE_URL and APP_VERSION
    else f"{WEBAPP_BASE_URL}/" if WEBAPP_BASE_URL else ""
)


# Text labels used by the persistent reply-keyboard buttons. Exported so
# handlers can match on them via F.text == BTN_MAIN_MENU without drifting
# strings between files.
BTN_MAIN_MENU = "🏠 Главное меню"
BTN_BUY = "💎 Подписка"
BTN_TOPUP = "💳 Пополнить"
BTN_INVITE = "🤝 Пригласить"
BTN_HELP = "❓ Помощь"
# Self-report VPN breakage — отдельная всегда-доступная кнопка в нижней
# reply-клавиатуре. Плановый health-ping воркера приходит юзеру не чаще
# раза в сутки и только в обеденное окно МСК (см. USER_HEALTH_PING_*
# в worker.py). Эта кнопка позволяет юзеру пожаловаться прямо сейчас,
# не дожидаясь следующего окна.
BTN_VPN_BROKEN = "🆘 VPN не работает"


def start_keyboard(*, has_devices: bool = True) -> types.ReplyKeyboardMarkup:
    """Always-on reply keyboard at the bottom of the chat.

    ``has_devices=False`` прячет «🆘 VPN не работает» (роадмап E3.3): чинить
    юзеру нечего, а кнопка вела в тупик «У тебя нет активной подписки.
    Оформить — /buy» — команды ``/buy`` в боте не существует. Заодно это
    убирает с первого экрана новичка один из трёх «поломочных» элементов.
    """
    last_row = [types.KeyboardButton(text=BTN_HELP)]
    if has_devices:
        last_row.append(types.KeyboardButton(text=BTN_VPN_BROKEN))
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text=BTN_MAIN_MENU), types.KeyboardButton(text=BTN_BUY)],
            [types.KeyboardButton(text=BTN_TOPUP), types.KeyboardButton(text=BTN_INVITE)],
            last_row,
        ],
        resize_keyboard=True,
    )


# Slash-command menu registered via bot.set_my_commands() on startup.
# This is what makes the "/" button appear next to the text input and
# open a scrollable command list from the bottom of the screen. Without
# this call Telegram doesn't render that button at all.
DEFAULT_COMMANDS: list[types.BotCommand] = [
    types.BotCommand(command="start", description="Главное меню"),
    types.BotCommand(command="plans", description="Тарифы"),
    types.BotCommand(command="balance", description="Баланс и подписки"),
    types.BotCommand(command="config", description="Получить конфиг"),
    types.BotCommand(command="devices", description="Мои устройства"),
    types.BotCommand(command="referral", description="Пригласить друга"),
    types.BotCommand(command="settings", description="Настройки уведомлений"),
    types.BotCommand(command="help", description="Помощь и поддержка"),
]


def help_keyboard() -> types.InlineKeyboardMarkup:
    """Top-level /help menu: two FAQ entries + live support contact.

    The "Связаться с поддержкой" button triggers the support-ticket
    flow in bot/support.py (forward-to-admin + reply-back). It's
    always shown when ADMIN_IDS is configured — the callback handler
    itself guards against the empty-admins edge case.
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(
                text="🔐 Личный кабинет не открывается",
                callback_data="help:cabinet",
            )],
            [types.InlineKeyboardButton(
                text="🇷🇺 Не открываются Яндекс / банки",
                callback_data="help:ru_sites",
            )],
            [types.InlineKeyboardButton(
                text="🌐 VPN не подключается / медленный",
                callback_data="help:vpn",
            )],
            [types.InlineKeyboardButton(
                text="💬 Связаться с поддержкой",
                callback_data="help:support",
            )],
            # Оферта / возвраты / ПД + реквизиты: документы лежат на сайте,
            # бот только ведёт к ним (их запросила СБ lava.top, 22.09.2026).
            [types.InlineKeyboardButton(
                text="ℹ️ О сервисе и документы",
                callback_data="help:about",
            )],
        ]
    )


def about_keyboard(legal_base_url: str) -> types.InlineKeyboardMarkup:
    """«О сервисе»: ссылки на публичные документы + назад. Без базового URL
    (env LEGAL_BASE_URL пуст) — только «назад», чтобы не слать битые ссылки."""
    rows: list[list[types.InlineKeyboardButton]] = []
    if legal_base_url:
        base = legal_base_url.rstrip("/")
        rows.extend([
            [types.InlineKeyboardButton(
                text="📄 Пользовательское соглашение", url=f"{base}/terms.html",
            )],
            [types.InlineKeyboardButton(
                text="↩️ Политика возвратов", url=f"{base}/refund.html",
            )],
            [types.InlineKeyboardButton(
                text="🔒 Политика персональных данных", url=f"{base}/privacy.html",
            )],
        ])
    rows.append([types.InlineKeyboardButton(text="← Назад", callback_data="help:back")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def help_back_keyboard() -> types.InlineKeyboardMarkup:
    """Action row attached to each FAQ answer.

    Kept intentionally thick: юзер, который попал в FAQ и не нашёл
    ответа, должен иметь два выхода в один тап — написать в поддержку
    прямо отсюда или вернуться в главное меню, не ныряя назад по
    иерархии.
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(
                text="💬 Связаться с поддержкой",
                callback_data="help:support",
            )],
            [
                types.InlineKeyboardButton(
                    text="⬅ Назад",
                    callback_data="help:back",
                ),
                types.InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="go:start",
                ),
            ],
        ]
    )


def webapp_inline_keyboard() -> types.InlineKeyboardMarkup | None:
    """Inline button that opens the WebApp in a single tap.

    Returns None if WEBAPP_BASE_URL is unset or non-HTTPS so callers can
    fall back to text-only replies in dev.
    """
    if not WEBAPP_BASE_URL.startswith("https://"):
        return None
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="🔐 Открыть личный кабинет",
                    web_app=types.WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )


def welcome_action_keyboard(
    *, trial_available: bool = False, is_new: bool = False, has_link: bool = False
) -> types.InlineKeyboardMarkup:
    """Inline-меню под приветствием.

    ``has_link=True`` (есть подписка active/frozen ИЛИ живые устройства —
    handlers считает из ``has_subscription or has_devices``) добавляет
    «🔗 Ссылка для подключения» (``go:config``): у возвращающегося — второй
    строкой после ЛК, у новичка с незабранным подарком — после кнопки подарка.
    Это способ забрать ссылку ПОТОМ, если сразу её не отдало (инцидент
    2026-08-25: выдача после триала упала, а с главного экрана к ссылке не
    вело ничего). Гейт по подписке, а не только по девайсам: на cold-пути
    девайс ~минуту pending, у замороженного девайсов нет — путь к ссылке и
    статусу им всё равно нужен. Платный юзер с незабранным подарком тоже не
    должен терять путь к ссылке. ЛК остаётся основным местом, нативная
    выдача — фолбэк для тех, у кого ЛК не открывается.

    Онбординг-роадмап E1.1/E1.5. Раньше здесь всегда висели 6 кнопок, и вместе
    с нижней reply-клавиатурой первый экран давал 12 кликабельных вариантов без
    единого выделенного — при том что 75% новых юзеров уходили, не сделав
    ничего. Теперь у новичка с неотобранным подарком ОДНА главная кнопка
    (забрать месяц), а «поломочные» пункты («Проблема с ЛК») ему не
    показываются: до первого действия они читаются как «тут всё ломается».

    Возвращающийся юзер видит прежний набор — он уже знает продукт, и урезать
    ему навигацию незачем.
    """
    rows: list[list[types.InlineKeyboardButton]] = []
    has_webapp = WEBAPP_BASE_URL.startswith("https://")
    # Подарок больше НЕ зависит от ЛК: активация нативная, прямо в боте
    # (аудит 2026-08-21, паритет A: web_app-кнопка была единственным путём
    # к триалу, а ЛК у части юзеров не открывается вовсе).
    onboarding = trial_available

    if onboarding:
        rows.append([
            types.InlineKeyboardButton(
                # Подарок должен быть НАЗВАН на кнопке: раньше он жил одной
                # строкой в тексте, а кнопка называлась «Открыть личный
                # кабинет» — связь между офером и действием юзер должен был
                # додумать сам.
                text="🎁 Забрать бесплатный месяц",
                callback_data="trial:activate",
            )
        ])
    elif has_webapp:
        rows.append([
            types.InlineKeyboardButton(
                text="🔐 Открыть личный кабинет",
                web_app=types.WebAppInfo(url=WEBAPP_URL),
            )
        ])
    if has_link:
        rows.append([
            types.InlineKeyboardButton(
                text="🔗 Ссылка для подключения", callback_data="go:config"
            )
        ])
    if onboarding:
        # Один экран — одно действие. Тарифы оставляем вторым, ненавязчивым
        # рядом: кому подарок не нужен, тот всё равно найдёт цены.
        rows.append([
            types.InlineKeyboardButton(text="💎 Тарифы", callback_data="go:plans"),
        ])
        return types.InlineKeyboardMarkup(inline_keyboard=rows)

    rows.append([
        types.InlineKeyboardButton(
            text="💎 Подписка", callback_data="go:plans"
        ),
        types.InlineKeyboardButton(
            text="💳 Пополнить", callback_data="go:topup"
        ),
    ])
    help_row = [
        types.InlineKeyboardButton(text="🆘 Помощь", callback_data="go:help"),
    ]
    if not is_new:
        help_row.insert(0, types.InlineKeyboardButton(
            text="❓ Проблема с ЛК", callback_data="help:cabinet"
        ))
    rows.append(help_row)
    rows.append([
        types.InlineKeyboardButton(
            text="🤝 Пригласить друга", callback_data="go:referral"
        ),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def onboarding_keyboard() -> types.InlineKeyboardMarkup:
    """Inline keyboard with setup instructions per platform.

    Первой строкой — ЛК (если WebApp по https, как в webapp_inline_keyboard).
    Клавиатура висит на всех экранах выдачи ссылки: /config, «Оплата
    получена» после Stars, пуш ``config_ready`` в поллере (bot.py) — и это
    единый «акцент на личный кабинет»: там ссылка лежит всегда, даже если в
    чат её прислать не удалось (инцидент 2026-08-25).
    """
    rows: list[list[types.InlineKeyboardButton]] = []
    if WEBAPP_BASE_URL.startswith("https://"):
        rows.append([types.InlineKeyboardButton(
            text="🔐 Открыть личный кабинет",
            web_app=types.WebAppInfo(url=WEBAPP_URL),
        )])
    return types.InlineKeyboardMarkup(
        inline_keyboard=rows + [
            [types.InlineKeyboardButton(
                text="Android (v2rayNG)",
                callback_data="onboard:android",
            )],
            [types.InlineKeyboardButton(
                text="iOS (Hiddify / Streisand)",
                callback_data="onboard:ios",
            )],
            [types.InlineKeyboardButton(
                text="Windows (Hiddify / Nekoray)",
                callback_data="onboard:windows",
            )],
            [types.InlineKeyboardButton(
                text="macOS (Hiddify)",
                callback_data="onboard:macos",
            )],
        ]
    )
