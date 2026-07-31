import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, MenuButtonWebApp, WebAppInfo
from .config import BOT_TOKEN, BACKEND_URL, ADMIN_API_TOKEN, NOTIFICATION_POLL_INTERVAL, BOT_WEBHOOK_PORT
from .handlers import close_session, router, get_session, onboarding_keyboard, health_ping_keyboard, node_diagnosis_keyboard
from .keyboards import DEFAULT_COMMANDS, WEBAPP_URL
from .middleware import BanGuard
from .support import support_router

logger = logging.getLogger(__name__)

# Рейт-лимит для лога не-200 ответов /pending: не чаще раза в 60с,
# чтобы при стабильном 401/500 не сыпать warning каждый poll-interval.
# Состояние вынесено на уровень модуля (поллер — единственный писатель).
_PENDING_ERR_LOG_INTERVAL = 60.0
_pending_err_last_log = 0.0
_pending_err_last_status: int | None = None

# Дедуп доставки (at-least-once → эффективно at-most-once для юзера).
# notif_id → ts успешной отправки в Telegram, по которой ещё НЕ прошёл
# ACK на backend. Пока ACK не подтверждён, запись остаётся в /pending и
# вернётся на следующем тике — но повторно слать её в Telegram нельзя,
# иначе при мигающем backend'е юзер (а для admin_broadcast — вся
# рассылка) получит серию дублей. TTL-очистка страхует от вечного роста
# и вечного подавления, если ACK так и не пройдёт.
_sent_unacked: dict[int, float] = {}
_SENT_UNACKED_TTL = 600.0  # 10 минут

# Голова-очереди (head-of-line): в один тик /pending может вернуть и
# срочный config_ready (после оплаты), и массовую admin_broadcast на
# сотни юзеров. Чтобы рассылка не тормозила срочную доставку, срочные
# типы сортируются в начало тика, а число admin_broadcast за один тик
# ограничено — хвост сверх лимита вернётся из /pending на следующем
# тике (запись не ACK-ается, пока не отправлена). Настраивается через env.
_BROADCAST_PER_TICK = int(os.getenv("NOTIFICATION_BROADCAST_PER_TICK", "50"))

# Реестр фоновых ACK-тасков: notif_id → Task. ACK делаем НЕ блокирующим
# основной проход (иначе залипший ACK бэкенда добавляет до ~4.5с на
# каждую запись и держит отправку следующих сообщений). Реестр держит
# ссылку на таск (иначе GC), а также дедуплицирует: пока ACK для notif_id
# в полёте, повторный не спавним.
_ack_tasks: dict[int, asyncio.Task] = {}


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
                    # Логируем не-200 с дедупом: сразу при смене статуса,
                    # иначе не чаще раза в _PENDING_ERR_LOG_INTERVAL секунд.
                    # Иначе рассинхрон ADMIN_API_TOKEN (401) или 500 гасят
                    # ВСЮ доставку (конфиги после оплаты, health-пинги,
                    # админ-алерты) абсолютно молча — инцидент виден только
                    # по жалобам, а причина не восстановима по логам.
                    global _pending_err_last_log, _pending_err_last_status
                    now = asyncio.get_event_loop().time()
                    if (
                        resp.status != _pending_err_last_status
                        or now - _pending_err_last_log >= _PENDING_ERR_LOG_INTERVAL
                    ):
                        body = (await resp.text())[:200]
                        logger.warning(
                            "notifications/pending returned %s (delivery "
                            "stalled): %s",
                            resp.status, body,
                        )
                        _pending_err_last_log = now
                        _pending_err_last_status = resp.status
                    continue
                # Успешный ответ — сбрасываем дедуп, чтобы следующий сбой
                # залогировался сразу, а не «проглотился» окном 60с.
                if _pending_err_last_status is not None:
                    _pending_err_last_status = None
                notifications = await resp.json()

            async def _ack(notif_id_inner: int, sent: dict | None = None) -> None:
                """ACK helper: помечает запись в audit_logs как `:delivered`,
                чтобы /pending её больше не возвращал. Ретраим до 3 раз с
                backoff: если ACK не пройдёт, запись останется в /pending и
                на следующем тике вернётся снова, но id держится в
                _sent_unacked, поэтому повторно в Telegram не уйдёт. При
                успешном ACK снимаем id с дедупа.
                """
                if not notif_id_inner:
                    return
                for attempt in range(3):
                    try:
                        async with session.post(
                            f"{BACKEND_URL}/api/notifications/{notif_id_inner}/ack",
                            headers=headers,
                            # message_id — чтобы отправленное можно было
                            # ОТОЗВАТЬ: Bot API удаляет свои сообщения 48 часов,
                            # но только по id. Пока мы его не сохраняли,
                            # ошибочная рассылка была необратима.
                            json=(sent or {}),
                            timeout=__import__("aiohttp").ClientTimeout(total=5),
                        ) as ack_resp:
                            if ack_resp.status == 200:
                                _sent_unacked.pop(notif_id_inner, None)
                                return
                            # не-200 (401/500) — временная рассинхронизация,
                            # ретраим ниже
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "ACK failed for notif=%s (attempt %d/3)",
                            notif_id_inner, attempt + 1,
                        )
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
                # Все попытки исчерпаны — id остаётся в _sent_unacked, чтобы
                # не переотправить сообщение; следующий тик до-ACKнет его.
                logger.warning(
                    "ACK not confirmed for notif=%s after 3 attempts; "
                    "will retry next tick (no re-send)",
                    notif_id_inner,
                )

            def _spawn_ack(notif_id_inner: int, sent: dict | None = None) -> None:
                """Запускает _ack в фоне, не блокируя основной проход.

                Дедуп: если ACK для notif_id уже в полёте — не спавним
                второй (иначе на каждом тике до подтверждения плодились бы
                параллельные ACK одной записи). Ссылку на таск держим в
                _ack_tasks до завершения, чтобы его не собрал GC.
                """
                if not notif_id_inner or notif_id_inner in _ack_tasks:
                    return
                task = asyncio.create_task(_ack(notif_id_inner, sent))
                _ack_tasks[notif_id_inner] = task
                task.add_done_callback(
                    lambda _t, _nid=notif_id_inner: _ack_tasks.pop(_nid, None)
                )

            # Чистим протухшие записи дедупа: если ACK так и не прошёл за
            # TTL, отпускаем id — лучше редкий дубль, чем вечная блокировка.
            _now_ts = asyncio.get_event_loop().time()
            for _stale_id in [
                _nid for _nid, _ts in _sent_unacked.items()
                if _now_ts - _ts > _SENT_UNACKED_TTL
            ]:
                _sent_unacked.pop(_stale_id, None)

            # Приоритизация в пределах тика: срочные типы (config_ready
            # после оплаты, health_ping_request, admin_alert_*) — вперёд,
            # массовая admin_broadcast — в хвост. sort стабильный, поэтому
            # внутри каждой группы порядок backend'а сохраняется. Это
            # снимает head-of-line: срочный конфиг не ждёт завершения
            # рассылки, попавшей в тот же ответ /pending.
            notifications.sort(
                key=lambda n: 1 if n.get("type") == "admin_broadcast" else 0
            )

            broadcast_sent = 0
            for notif in notifications:
                telegram_id = notif.get("telegram_id")
                text = notif.get("text", "")
                notif_id = notif.get("id")
                if not telegram_id or not text:
                    continue
                # Уже отправлено в Telegram, но ACK ещё не подтверждён —
                # НЕ слать повторно (иначе дубль у юзера/рассылки), только
                # до-ACKнуть запись на backend'е (в фоне, не блокируя проход).
                if notif_id and notif_id in _sent_unacked:
                    _spawn_ack(notif_id)
                    continue
                notif_type = notif.get("type")
                # Лимит рассылок на тик: срочные типы уже отсортированы в
                # начало, поэтому хвост admin_broadcast сверх лимита просто
                # переносим на следующий тик (запись не ACK-нута → вернётся
                # из /pending). Так одна большая рассылка не занимает весь
                # тик и не голодит срочную доставку следующих тиков.
                if notif_type == "admin_broadcast" and broadcast_sent >= _BROADCAST_PER_TICK:
                    continue
                try:
                    keyboard = None
                    if notif_type == "config_ready":
                        keyboard = onboarding_keyboard()
                    elif notif_type == "health_ping_request":
                        keyboard = health_ping_keyboard(notif.get("subscription_id"))
                    elif notif_type == "admin_alert_node_diagnosis":
                        keyboard = node_diagnosis_keyboard(
                            notif.get("target_kind"), notif.get("target_id")
                        )
                    sent_msg = await bot.send_message(
                        chat_id=int(telegram_id),
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                    # Помечаем как отправленное ДО ACK: если ACK упадёт и
                    # запись вернётся на следующем тике, дубля в TG не будет.
                    if notif_id:
                        _sent_unacked[notif_id] = asyncio.get_event_loop().time()
                    # ACK — в фоне: залипший ACK бэкенда не должен держать
                    # отправку следующих (в т.ч. срочных) уведомлений тика.
                    _spawn_ack(
                        notif_id,
                        {
                            "message_id": getattr(sent_msg, "message_id", None),
                            "chat_id": int(telegram_id),
                        },
                    )
                    # Для admin_broadcast спим между сообщениями, чтобы не
                    # упереться в Telegram rate-limit ~30 msg/sec. При
                    # лимите _BROADCAST_PER_TICK тик отпустится за ~2.5s. Для
                    # остальных типов (config_ready, health_ping_request,
                    # admin_alert_*) задержка не нужна — их мало.
                    if notif_type == "admin_broadcast":
                        broadcast_sent += 1
                        await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    # Юзер заблокировал бота / удалил чат / деактивировал
                    # аккаунт. Без ACK эта запись будет возвращаться из
                    # /pending каждые NOTIFICATION_POLL_INTERVAL секунд
                    # и поллер залипнет в loop, забивая весь cluster
                    # (Failed to deliver спамом в логах, /pending в DoS).
                    # Терминальная ошибка — ACK-аем и идём дальше.
                    logger.warning(
                        "TG forbidden for chat=%s (blocked/deactivated), "
                        "marking notif=%s as delivered",
                        telegram_id, notif_id,
                    )
                    _spawn_ack(notif_id)
                except TelegramBadRequest as e:
                    # «chat not found», «user not found», «message is too
                    # long» и пр. — все терминальные с точки зрения именно
                    # этой записи. Ретрай не поможет, нужен ACK.
                    logger.warning(
                        "TG bad request for chat=%s: %s, ack notif=%s",
                        telegram_id, e.message, notif_id,
                    )
                    _spawn_ack(notif_id)
                except (ValueError, TypeError):
                    # Нечисловой telegram_id (напр. плейсхолдер-юзер
                    # ``__recovery_orphans__``, id=999999) — int() падает, а
                    # доставить такую запись нельзя НИКОГДА. Терминально, как
                    # TelegramBadRequest: ACK-аем, иначе с переходом очереди
                    # на FIFO (oldest-first) она висит в голове и спамит
                    # «Failed to deliver» каждый тик, забивая логи.
                    logger.warning(
                        "Non-numeric telegram_id %r for notif=%s — "
                        "undeliverable, marking delivered",
                        telegram_id, notif_id,
                    )
                    _spawn_ack(notif_id)
                except Exception:
                    # Сетевые/временные ошибки — НЕ ACK-аем, поллер
                    # попробует снова на следующем тике.
                    logger.exception("Failed to deliver notification to %s", telegram_id)

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Notification poller error")


async def on_dispatch_error(event: ErrorEvent) -> None:
    """Глобальная страховка от необработанных исключений в хендлерах.

    Без неё юзер при падении хендлера остаётся ни с чем: для message —
    «бот молчит», для callback — кнопка крутит спиннер ~30 секунд,
    потому что callback_query.answer() так и не был вызван. Здесь
    логируем стектрейс и best-effort отвечаем пользователю.
    """
    logger.exception(
        "Unhandled error while processing update %s",
        getattr(event.update, "update_id", None),
        exc_info=event.exception,
    )
    # Ответ юзеру оборачиваем в try/except, чтобы обработчик ошибок
    # не упал вторично (например, TelegramForbiddenError, если юзер
    # заблокировал бота, или протухший callback_query).
    try:
        if event.update.callback_query:
            # Гасим спиннер на кнопке коротким тостом.
            await event.update.callback_query.answer(
                "Что-то пошло не так, попробуйте ещё раз позже"
            )
        elif event.update.message:
            await event.update.message.answer(
                "Произошла ошибка, попробуйте позже."
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to notify user about handler error")


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
    # Глобальный errors-хендлер: ловит всё, что не поймали сами
    # хендлеры (таймауты бэкенда, KeyError на неожиданном JSON и пр.).
    dp.errors.register(on_dispatch_error)

    # Register the slash-command menu so the "/" button appears next to
    # the text input. Telegram caches this list client-side, so one call
    # on startup is enough. Best-effort: network hiccups shouldn't keep
    # the bot from booting.
    try:
        await bot.set_my_commands(DEFAULT_COMMANDS)
    except Exception:
        logger.exception("set_my_commands failed")

    # Menu-кнопка «Личный кабинет» перезаписывается программно (изначально
    # была задана в BotFather): URL несёт кэшбастер ?v=<версия выката>, и
    # меняться он должен при каждом деплое — руками в BotFather это не
    # прожить. Best-effort по той же причине, что и set_my_commands.
    if WEBAPP_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Личный кабинет",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            )
        except Exception:
            logger.exception("set_chat_menu_button failed")

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
