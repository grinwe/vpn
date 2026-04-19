import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from .config import BOT_TOKEN, BACKEND_URL, ADMIN_API_TOKEN, NOTIFICATION_POLL_INTERVAL, BOT_WEBHOOK_PORT
from .handlers import close_session, router, get_session, onboarding_keyboard, health_ping_keyboard
from .keyboards import DEFAULT_COMMANDS
from .middleware import BanGuard
from .support import support_router

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
                    notif_type = notif.get("type")
                    if notif_type == "config_ready":
                        keyboard = onboarding_keyboard()
                    elif notif_type == "health_ping_request":
                        keyboard = health_ping_keyboard(notif.get("subscription_id"))
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
                        ):
                            pass  # Best-effort ack
                    # Для admin_broadcast спим между сообщениями, чтобы не
                    # упереться в Telegram rate-limit ~30 msg/sec. При
                    # батче в 50 рассылок тик отпустится за ~2.5s. Для
                    # остальных типов (config_ready, health_ping_request,
                    # admin_alert_*) задержка не нужна — их мало.
                    if notif_type == "admin_broadcast":
                        await asyncio.sleep(0.05)
                except Exception:
                    logger.exception("Failed to deliver notification to %s", telegram_id)

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Notification poller error")


async def main():
    logging.basicConfig(level="INFO")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    # MemoryStorage powers the support-ticket FSM in support.py. In-
    # memory is fine for a single-instance bot; if we go multi-instance
    # this becomes RedisStorage.
    dp = Dispatcher(storage=MemoryStorage())
    # BanGuard runs as outer middleware on `update` so it fires BEFORE
    # any router/handler dispatch — a banned user's update is consumed
    # silently without touching FSM state, backend, or router chain.
    # See bot/middleware.py for the no-ACK rationale.
    dp.update.outer_middleware(BanGuard())
    # Support router must be included FIRST so its StateFilter handlers
    # get priority over the generic text/command matchers in the main
    # handlers router — otherwise admin's typed reply would hit /start
    # or other button handlers before reaching the support relay.
    dp.include_router(support_router)
    dp.include_router(router)

    # Register the slash-command menu so the "/" button appears next to
    # the text input. Telegram caches this list client-side, so one call
    # on startup is enough. Best-effort: network hiccups shouldn't keep
    # the bot from booting.
    try:
        await bot.set_my_commands(DEFAULT_COMMANDS)
    except Exception:
        logger.exception("set_my_commands failed")

    # Start notification poller as background task
    poller_task = None
    if NOTIFICATION_POLL_INTERVAL > 0:
        poller_task = asyncio.create_task(notification_poller(bot))

    try:
        if BOT_WEBHOOK_PORT > 0:
            # #62 — Webhook mode: the backend forwards Telegram updates to
            # this internal aiohttp server. Payment updates are handled by
            # the backend directly and never reach here.
            from aiohttp import web
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler

            wh_app = web.Application()
            handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
            handler.register(wh_app, path="/webhook")

            runner = web.AppRunner(wh_app)
            await runner.setup()
            site = web.TCPSite(runner, host="0.0.0.0", port=BOT_WEBHOOK_PORT)
            await site.start()
            logger.info("Bot webhook server listening on 0.0.0.0:%d", BOT_WEBHOOK_PORT)

            try:
                await asyncio.Event().wait()  # run until cancelled
            finally:
                await runner.cleanup()
        else:
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
