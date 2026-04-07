import asyncio
import logging
from aiogram import Bot, Dispatcher
from .config import BOT_TOKEN, BACKEND_URL, ADMIN_API_TOKEN, NOTIFICATION_POLL_INTERVAL
from .handlers import close_session, router, get_session, onboarding_keyboard

logger = logging.getLogger(__name__)


async def notification_poller(bot: Bot):
    """Background task: poll backend for pending notifications and deliver them.

    Picks up config-ready notifications and migration alerts stored by the
    worker in audit_log / task results, and sends them to users via Telegram.
    """
    while True:
        try:
            await asyncio.sleep(NOTIFICATION_POLL_INTERVAL)
            if NOTIFICATION_POLL_INTERVAL <= 0:
                return

            session = await get_session()
            headers = {}
            if ADMIN_API_TOKEN:
                headers["X-Admin-Token"] = ADMIN_API_TOKEN

            async with session.get(
                f"{BACKEND_URL}/api/notifications/pending",
                headers=headers,
                timeout=__import__("aiohttp").ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    continue
                notifications = await resp.json()

            for notif in notifications:
                telegram_id = notif.get("telegram_id")
                text = notif.get("text", "")
                notif_id = notif.get("id")
                if not telegram_id or not text:
                    continue
                try:
                    keyboard = None
                    if notif.get("type") == "config_ready":
                        keyboard = onboarding_keyboard()
                    await bot.send_message(
                        chat_id=int(telegram_id),
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                    # Acknowledge delivery
                    if notif_id:
                        async with session.post(
                            f"{BACKEND_URL}/api/notifications/{notif_id}/ack",
                            headers=headers,
                        ) as ack_resp:
                            pass  # Best-effort ack
                except Exception:
                    logger.exception("Failed to deliver notification to %s", telegram_id)

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Notification poller error")


async def main():
    logging.basicConfig(level="INFO")
    bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher()
    dp.include_router(router)

    # Start notification poller as background task
    poller_task = None
    if NOTIFICATION_POLL_INTERVAL > 0:
        poller_task = asyncio.create_task(notification_poller(bot))

    try:
        await dp.start_polling(bot)
    finally:
        if poller_task:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
        await close_session()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
