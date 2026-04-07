import os

from aiogram import types

# Public HTTPS URL of the Telegram WebApp. Used to build the inline
# "Личный кабинет" button on /start. The persistent entry point is the
# Menu Button configured in BotFather (Bot Settings → Menu Button), so
# this URL is only needed for the one-tap launch right after onboarding.
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "").rstrip("/")


def start_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Купить VPN"), types.KeyboardButton(text="Мой конфиг")],
            [types.KeyboardButton(text="Статус"), types.KeyboardButton(text="Продлить")],
            [types.KeyboardButton(text="Реферальная ссылка")],
        ],
        resize_keyboard=True,
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
