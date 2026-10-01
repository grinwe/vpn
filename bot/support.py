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
from aiogram.exceptions import TelegramForbiddenError
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


_MENU_TEXTS = frozenset(
    {"🏠 Главное меню", "💎 Подписка", "💳 Пополнить", "🤝 Пригласить",
     "❓ Помощь", "🆘 VPN не работает", "Купить VPN"}
)


@support_router.message(
    StateFilter(SupportStates.waiting_user_message), ~F.successful_payment
)
async def forward_to_admin(message: types.Message, state: FSMContext):
    # Команды и кнопки меню — не текст обращения: раньше «🏠 Главное меню»
    # или /plans, нажатые в диалоге поддержки, улетали админу как тикет
    # (аудит 2026-08-21, тупик №8). Выходим из диалога и просим повторить.
    text = (message.text or "").strip()
    if text.startswith("/") or text in _MENU_TEXTS:
        await state.clear()
        if text == "🆘 VPN не работает":
            # Человек в диалоге поддержки нажал SOS — это не текст обращения,
            # а починка: запускаем её сразу, а не просим нажать ещё раз
            # (ленивый импорт: handlers импортирует этот модуль).
            from .handlers import self_report_vpn_broken

            await self_report_vpn_broken(message)
            return
        await message.answer(
            "Ок, вышел из диалога поддержки. Повтори действие ещё раз."
        )
        return
    if not ADMIN_IDS:
        await state.clear()
        await message.answer("Поддержка временно недоступна.")
        return
    admin_id = ADMIN_IDS[0]
    delivered = False
    try:
        uname = message.from_user.username
        label = f"@{uname}" if uname else f"id{message.from_user.id}"
        full_name = message.from_user.full_name or ""
        header = f"☝ Запрос в поддержку от {label}"
        if full_name:
            header += f" ({full_name})"
        # Порядок важен: сперва шлём заголовок с кнопкой «Ответить» —
        # это «якорь», по которому админ сможет ответить пользователю.
        # Только после него пересылаем контент. Иначе при сетевом сбое
        # между вызовами админ получал пересланное сообщение без кнопки
        # и физически не мог ответить.
        await message.bot.send_message(
            admin_id,
            header,
            reply_markup=_reply_button(message.from_user.id),
        )
        # Regular forward so the admin can tap the header and open the
        # user's profile / see their @username.
        await message.forward(chat_id=admin_id)
        delivered = True
    except Exception:
        logger.exception("support forward failed")

    # Подтверждение юзеру привязано к факту доставки контента админу:
    # если контент уже дошёл — не показываем «не получилось», иначе юзер
    # отправит повторно и создаст дубль у админа. Стейт чистим ТОЛЬКО при
    # доставке: раньше он чистился в finally, и «попробуй ещё раз» после
    # сбоя уходило в пустоту — ни один хэндлер не матчился (аудит
    # 2026-08-21, тупик №2).
    if delivered:
        await state.clear()
        await message.answer(
            "✅ Передали админу. Как только ответит — пришлю сюда же."
        )
    else:
        await message.answer(
            "Не получилось отправить 😔 Попробуй ещё раз через минуту "
            "или нажми /cancel."
        )


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


@support_router.message(
    StateFilter(SupportStates.admin_replying), ~F.successful_payment
)
async def relay_admin_reply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    if not target_user_id:
        # Стейт пережил рестарт наполовину (MemoryStorage) — молчаливый
        # clear оставлял админа гадать, куда делся его текст.
        await state.clear()
        await message.answer(
            "Сессия ответа потеряна. Нажми «✍ Ответить» на запросе ещё раз."
        )
        return
    try:
        # Сперва гарантированно доставляем сам контент (copy_to
        # пересобирает его как сообщение от бота, чтобы юзер не видел
        # личный аккаунт админа). Заголовок-маркер шлём ТОЛЬКО после
        # успешной доставки — иначе при сбое между двумя вызовами юзер
        # получал голый «💬 Ответ от поддержки:» без содержимого.
        await message.copy_to(chat_id=target_user_id)
    except TelegramForbiddenError:
        # Юзер заблокировал бота — это не транзиентный сбой, повтор не
        # поможет; говорим админу прямо, без «возможно».
        logger.info("support reply blocked by user %s", target_user_id)
        await message.answer(
            "Не доставлено: пользователь заблокировал бота."
        )
        await state.clear()
        return
    except Exception:
        logger.exception("support admin reply failed")
        # Стейт оставляем живым: «попробуй ещё раз» с очищенным стейтом
        # отправляло повтор в обычные хэндлеры, а не юзеру.
        await message.answer(
            "Не удалось доставить (временный сбой). Попробуй ещё раз "
            "или нажми /cancel."
        )
        return

    # Контент доставлен — маркер «от поддержки» опционален; его сбой не
    # должен показываться админу как провал доставки и провоцировать
    # повторную отправку (дубль).
    try:
        await message.bot.send_message(target_user_id, "💬 ↑ Ответ от поддержки")
    except Exception:
        logger.warning(
            "support reply marker send failed for %s",
            target_user_id,
            exc_info=True,
        )
    await message.answer("✅ Отправлено.")
    await state.clear()
