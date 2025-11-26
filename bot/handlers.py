from aiogram import Router, types, F
from aiogram.filters import Command
import aiohttp
from .config import BACKEND_URL
from .keyboards import start_keyboard, tariff_keyboard

router = Router()


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Это VPN-сервис с ShadowTLS+SS и VLESS Reality. Нажми 'Купить VPN' чтобы выбрать тариф.",
        reply_markup=start_keyboard(),
    )


@router.message(F.text == "Купить VPN")
async def buy_vpn(message: types.Message):
    await message.answer("Выбери тариф:", reply_markup=tariff_keyboard())


@router.message(F.text.startswith("Basic"))
@router.message(F.text.startswith("Pro"))
async def choose_plan(message: types.Message):
    # простая маппинг таблица, позже заменить на запрос к backend
    plan_map = {
        "Basic 1m": 1,
        "Basic 3m": 2,
        "Basic 12m": 3,
        "Pro 1m": 4,
        "Pro 12m": 5,
    }
    plan_id = plan_map.get(message.text)
    if not plan_id:
        await message.answer("Неизвестный тариф")
        return
    await message.answer(
        "Оплати тариф любым способом и отправь /confirm после подтверждения админом."
        "\n(В реальности здесь будет интеграция с платежами.)"
    )
    message.conf["selected_plan"] = plan_id  # hint for future FSM


@router.message(Command("status"))
async def status(message: types.Message):
    async with aiohttp.ClientSession() as session:
        resp = await session.get(f"{BACKEND_URL}/api/users/{message.from_user.id}")
        if resp.status != 200:
            await message.answer("Подписок не найдено")
            return
        data = await resp.json()
    lines = ["Твои подписки:"]
    for sub in data:
        lines.append(
            f"План: {sub['plan_name']}\nСервер: {sub['server']}\nИстекает: {sub['expires_at']}\nСтатус: {sub['status']}"
        )
    await message.answer("\n\n".join(lines))


@router.message(Command("confirm"))
async def confirm_payment(message: types.Message):
    plan_id = 1  # TODO: брать из FSM/контекста
    payload = {"telegram_id": str(message.from_user.id), "plan_id": plan_id}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BACKEND_URL}/api/subscriptions", json=payload) as resp:
            if resp.status != 200:
                await message.answer("Ошибка создания подписки")
                return
            data = await resp.json()
    text_lines = ["Твои конфиги:"]
    for c in data:
        text_lines.append(f"{c['proto']}:\n{c['config_text']}\n")
    await message.answer("\n".join(text_lines))
