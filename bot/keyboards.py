from aiogram import types


def start_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Купить VPN"), types.KeyboardButton(text="Мой конфиг")],
            [types.KeyboardButton(text="Статус"), types.KeyboardButton(text="Продлить")],
            [types.KeyboardButton(text="Реферальная ссылка")],
        ],
        resize_keyboard=True,
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
