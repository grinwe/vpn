"""Support ticket flow: user → admin DM forwarding with reply-back.

Option (b) from the help-section design: user taps "Связаться с
поддержкой" in /help, writes one message, and we forward it to the
primary admin as a regular Telegram forward (so the admin sees the
original author and can open their profile). The admin gets a
companion message with an inline "Ответить" button; when clicked, the
admin types a reply and it's relayed back to the user as a message
from the bot — we intentionally don't forward the admin's DM so the
user never sees who the admin is personally.

State lives in MemoryStorage, so it's per-process and lost on
restart. Fine for a single-instance bot; if we ever scale out we'll
need Redis. Worst case of a restart mid-reply: admin re-clicks
"Ответить" on the same forward and tries again.

This router is included BEFORE the main handlers router in bot.py so
its StateFilter handlers take priority over the generic text/button
matchers — otherwise an admin typing a reply would hit /start, /help
or other button handlers first and never reach this code.
"""
import logging

from aiogram import F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from .config import ADMIN_IDS

logger = logging.getLogger(__name__)
support_router = Router()


class SupportStates(StatesGroup):
    waiting_user_message = State()
    admin_replying = State()


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _reply_button(user_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(
                text="✍ Ответить",
                callback_data=f"support_reply:{user_id}",
            )
        ]]
    )


@support_router.callback_query(F.data == "help:support")
async def start_support(callback_query: types.CallbackQuery, state: FSMContext):
    if not ADMIN_IDS:
        await callback_query.answer("Поддержка не настроена", show_alert=True)
        return
    await callback_query.answer()
    await state.set_state(SupportStates.waiting_user_message)
    await callback_query.message.answer(
        "💬 Опиши проблему одним сообщением — текст, фото или голосовое. "
        "Мы передадим админу, ответ придёт сюда же.\n\n"
        "Отменить — /cancel."
    )


@support_router.message(
    StateFilter(SupportStates.waiting_user_message), Command("cancel")
)
async def cancel_user_support(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, отменил. Если что — снова жми /help.")


@support_router.message(StateFilter(SupportStates.waiting_user_message))
async def forward_to_admin(message: types.Message, state: FSMContext):
    if not ADMIN_IDS:
        await state.clear()
        await message.answer("Поддержка временно недоступна.")
        return
    admin_id = ADMIN_IDS[0]
    try:
        # Regular forward so the admin can tap the header and open the
        # user's profile / see their @username.
        await message.forward(chat_id=admin_id)
        uname = message.from_user.username
        label = f"@{uname}" if uname else f"id{message.from_user.id}"
        full_name = message.from_user.full_name or ""
        header = f"☝ Запрос в поддержку от {label}"
        if full_name:
            header += f" ({full_name})"
        await message.bot.send_message(
            admin_id,
            header,
            reply_markup=_reply_button(message.from_user.id),
        )
        await message.answer(
            "✅ Передали админу. Как только ответит — пришлю сюда же."
        )
    except Exception:
        logger.exception("support forward failed")
        await message.answer(
            "Не получилось отправить 😔 Попробуй ещё раз через минуту."
        )
    finally:
        await state.clear()


@support_router.callback_query(F.data.startswith("support_reply:"))
async def start_admin_reply(callback_query: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback_query.from_user.id):
        await callback_query.answer("Недостаточно прав", show_alert=True)
        return
    try:
        target_user_id = int(callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback_query.answer("Битый callback", show_alert=True)
        return
    await callback_query.answer()
    await state.set_state(SupportStates.admin_replying)
    await state.update_data(target_user_id=target_user_id)
    await callback_query.message.answer(
        f"✍ Напиши ответ пользователю id{target_user_id} одним сообщением "
        "(текст, фото, голос — что угодно).\n\n"
        "Отменить — /cancel."
    )


@support_router.message(
    StateFilter(SupportStates.admin_replying), Command("cancel")
)
async def cancel_admin_reply(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Ответ отменён.")


@support_router.message(StateFilter(SupportStates.admin_replying))
async def relay_admin_reply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    if not target_user_id:
        await state.clear()
        return
    try:
        # Header first so the user knows this is the support reply and
        # not a random notification or promo message.
        await message.bot.send_message(
            target_user_id,
            "💬 Ответ от поддержки:",
        )
        # copy_to re-sends the content as if it came from the bot, so
        # the user never sees the admin's personal account as author.
        await message.copy_to(chat_id=target_user_id)
        await message.answer("✅ Отправлено.")
    except Exception:
        logger.exception("support admin reply failed")
        await message.answer(
            "Не удалось доставить. Возможно, пользователь заблокировал бота."
        )
    finally:
        await state.clear()
