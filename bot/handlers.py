import aiohttp
from aiogram import F, Router, types
from aiogram.filters import Command

from .config import ADMIN_API_TOKEN, ADMIN_IDS, BACKEND_URL
from .keyboards import start_keyboard

router = Router()


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _format_plan_button(plan: dict) -> str:
    price = plan.get("price", "?")
    duration = plan.get("duration_days", "?")
    return f"{plan['name']} — {price}$ / {duration}d"


async def _fetch_json(method: str, url: str, **kwargs):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                try:
                    payload = await resp.json()
                except Exception:  # noqa: BLE001
                    payload = {"message": text}
                return resp.status, payload
        except aiohttp.ClientError as exc:
            return 0, {"message": f"backend unreachable: {exc}"}


def _admin_headers(actor_id: int) -> dict[str, str]:
    headers: dict[str, str] = {}
    if ADMIN_API_TOKEN:
        headers["X-Admin-Token"] = ADMIN_API_TOKEN
    headers["X-Admin-Actor"] = str(actor_id)
    return headers


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Это VPN-сервис с ShadowTLS+SS и VLESS Reality. "
        "Нажми 'Купить VPN' или вызови /plans, чтобы выбрать тариф.",
        reply_markup=start_keyboard(),
    )


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

    text_lines = [
        "Счет создан!", 
        f"Номер счета: {invoice['id']}",
        "Реквизиты для оплаты: реквизиты будут отправлены тут.",
        "После оплаты админ отметит его как оплаченный, и вы получите конфиги.",
    ]
    await callback_query.message.answer("\n".join(text_lines))
    await callback_query.answer()


@router.message(Command("status"))
@router.message(F.text == "Статус")
async def status(message: types.Message):
    try:
        status_code, data = await _fetch_json(
            "GET", f"{BACKEND_URL}/api/users/by_telegram/{message.from_user.id}"
        )
        if status_code != 200:
            await message.answer("Подписок не найдено")
            return
    except aiohttp.ClientError:
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
        return

    lines = ["Твои подписки:"]
    for sub in data:
        lines.append(
            f"План: {sub['plan_name']}\n"
            f"Сервер: {sub.get('node') or sub.get('server', 'n/a')} ({sub.get('region', '??')})\n"
            f"Истекает: {sub['expires_at']}\nСтатус: {sub['status']}"
        )
    await message.answer("\n\n".join(lines))


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
        await message.answer("Бэкенд недоступен. Попробуйте позже.")
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
            f"#{inv['id']} — план: {inv['plan_name']}, пользователь: {inv['user_telegram_id']}, сумма: {inv['amount']} {inv['currency']}"
        )
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f"Отметить как оплаченный #{inv['id']}",
                    callback_data=f"invoice_paid:{inv['id']}",
                )
            ]
        )

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

    user_id = invoice.get("user_telegram_id")
    credentials = invoice.get("credentials", [])
    if user_id and credentials:
        configs_text = ["Твои конфиги:"]
        for cred in credentials:
            configs_text.append(f"{cred['proto']}:\n{cred['config_text']}")
        await callback_query.message.bot.send_message(
            chat_id=user_id, text="\n\n".join(configs_text)
        )
    elif user_id:
        await callback_query.message.bot.send_message(
            chat_id=user_id,
            text="Счет оплачен, но конфиги пока не готовы. Свяжитесь с поддержкой.",
        )
