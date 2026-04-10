import os

from aiogram import types

# Public HTTPS URL of the Telegram WebApp. Used to build the inline
# "Личный кабинет" button on /start. The persistent entry point is the
# Menu Button configured in BotFather (Bot Settings → Menu Button), so
# this URL is only needed for the one-tap launch right after onboarding.
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "").rstrip("/")


# Text labels used by the persistent reply-keyboard buttons. Exported so
# handlers can match on them via F.text == BTN_MAIN_MENU without drifting
# strings between files.
BTN_MAIN_MENU = "🏠 Главное меню"
BTN_INVITE = "🤝 Пригласить"
BTN_HELP = "❓ Помощь"


def start_keyboard() -> types.ReplyKeyboardMarkup:
    """Minimal always-on reply keyboard: menu, invite, help.

    The old «Купить VPN / Мой конфиг / Статус / Продлить» buttons
    duplicated functionality now handled by the WebApp and the slash
    command menu, so they're gone. These three are the only entry
    points users need to see at all times.
    """
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text=BTN_MAIN_MENU), types.KeyboardButton(text=BTN_INVITE)],
            [types.KeyboardButton(text=BTN_HELP)],
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
    types.BotCommand(command="referral", description="Пригласить друга"),
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
                text="🌐 VPN не подключается / медленный",
                callback_data="help:vpn",
            )],
            [types.InlineKeyboardButton(
                text="💬 Связаться с поддержкой",
                callback_data="help:support",
            )],
        ]
    )


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
                    web_app=types.WebAppInfo(url=WEBAPP_BASE_URL),
                )
            ]
        ]
    )


def welcome_action_keyboard() -> types.InlineKeyboardMarkup:
    """Rich inline action menu under the /start welcome message.

    Mirrors the competitor layout (hitvpnbot): big WebApp button on top,
    then quick actions — "проблема с ЛК" (reuses help:cabinet callback),
    "помощь" and "пригласить друга". The WebApp row is only added when
    WEBAPP_BASE_URL is HTTPS; in dev we fall back to just the actions.
    """
    rows: list[list[types.InlineKeyboardButton]] = []
    if WEBAPP_BASE_URL.startswith("https://"):
        rows.append([
            types.InlineKeyboardButton(
                text="🔐 Открыть личный кабинет",
                web_app=types.WebAppInfo(url=WEBAPP_BASE_URL),
            )
        ])
    rows.append([
        types.InlineKeyboardButton(
            text="❓ Проблема с ЛК", callback_data="help:cabinet"
        ),
        types.InlineKeyboardButton(
            text="🆘 Помощь", callback_data="go:help"
        ),
    ])
    rows.append([
        types.InlineKeyboardButton(
            text="🤝 Пригласить друга", callback_data="go:referral"
        ),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def tariff_keyboard():
    keyboard = [
        [types.KeyboardButton(text="Basic 1m"), types.KeyboardButton(text="Basic 3m"), types.KeyboardButton(text="Basic 12m")],
        [types.KeyboardButton(text="Pro 1m"), types.KeyboardButton(text="Pro 12m")],
    ]
    return types.ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def onboarding_keyboard() -> types.InlineKeyboardMarkup:
    """Inline keyboard with setup instructions per platform."""
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
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
