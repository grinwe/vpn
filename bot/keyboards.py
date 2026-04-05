from aiogram import types


def start_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="Купить VPN")], [types.KeyboardButton(text="Статус")]],
        resize_keyboard=True,
    )


def tariff_keyboard():
    keyboard = [
        [types.KeyboardButton(text="Basic 1m"), types.KeyboardButton(text="Basic 3m"), types.KeyboardButton(text="Basic 12m")],
        [types.KeyboardButton(text="Pro 1m"), types.KeyboardButton(text="Pro 12m")],
    ]
    return types.ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
