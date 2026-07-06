"""Outer middleware: silent-drop updates from banned users.

Motivating scenario: a DDoS burst where hundreds of throwaway Telegram
accounts spam ``/start`` and starve the handler loop. We need to shed
those updates BEFORE any backend call, otherwise the ban mechanism
itself becomes the amplifier.

Strategy — pull the ban list from backend every ``_TTL_SECONDS``,
keep it in a process-local set, and gate every incoming update against
it. One admin API call per TTL window is independent of update rate,
so the bot's own load during a burst doesn't translate into backend
load. On cache miss (first update after process start, or backend
unreachable on refresh) we fail OPEN — better to let a banned user
through for one window than to black-hole every legit user because
backend blipped.

"No ACK" is load-bearing: we intentionally don't reply/ack/react to
dropped updates. Giving feedback (even an error) trains DDoS scripts
that the account is reachable. The dropped update is simply consumed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from .config import ADMIN_API_TOKEN, BACKEND_URL

logger = logging.getLogger(__name__)

_TTL_SECONDS = 30.0
# Бэкофф после неудачного fetch: не долбим бэкенд на каждом апдейте, но
# и не ждём целое TTL-окно, если бэкенд просто моргнул.
_RETRY_SECONDS = 5.0
_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=5)


class BanGuard(BaseMiddleware):
    def __init__(self) -> None:
        self._banned: set[str] = set()
        self._loaded_at: float = 0.0
        # Когда разрешён следующий refresh. Ставится синхронно при постановке
        # задачи, чтобы параллельные апдейты не наплодили гонку fetch'ей.
        self._next_refresh_at: float = 0.0
        # Сериализует сам fetch: если refresh уже в полёте — новый не запускаем.
        self._lock = asyncio.Lock()
        # Держим ссылку на фоновую задачу, чтобы её не собрал GC до завершения.
        self._refresh_task: asyncio.Task[None] | None = None

    async def _refresh(self) -> None:
        headers: dict[str, str] = {}
        if ADMIN_API_TOKEN:
            headers["X-Admin-Token"] = ADMIN_API_TOKEN
        try:
            async with aiohttp.ClientSession(timeout=_FETCH_TIMEOUT) as s:
                async with s.get(
                    f"{BACKEND_URL}/api/users/banned-telegram-ids",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "ban list fetch got HTTP %s; keeping stale cache",
                            resp.status,
                        )
                        # Короткий бэкофф вместо ретрая на каждом апдейте.
                        self._next_refresh_at = time.monotonic() + _RETRY_SECONDS
                        return
                    data = await resp.json()
            self._banned = {str(x) for x in data if x is not None}
            self._loaded_at = time.monotonic()
            self._next_refresh_at = self._loaded_at + _TTL_SECONDS
        except Exception:
            logger.exception("ban list fetch failed; keeping stale cache")
            # Короткий бэкофф: держим stale-кэш, но не заваливаем бэкенд.
            self._next_refresh_at = time.monotonic() + _RETRY_SECONDS

    async def _run_refresh(self) -> None:
        # Второй барьер сериализации на случай гонки постановки задач.
        if self._lock.locked():
            return
        async with self._lock:
            await self._refresh()

    def _maybe_refresh(self) -> None:
        # НЕ блокирует обработку апдейта: fetch уходит в фон, текущий апдейт
        # проходит по (возможно stale) кэшу. Окно застолбливаем синхронно.
        now = time.monotonic()
        if now < self._next_refresh_at or self._lock.locked():
            return
        # Резервируем следующее окно немедленно, до всякого await, чтобы
        # соседние апдейты в этом же тике не поставили ещё один fetch.
        self._next_refresh_at = now + _TTL_SECONDS
        self._refresh_task = asyncio.create_task(self._run_refresh())

    @staticmethod
    def _extract_user_id(event: TelegramObject) -> str | None:
        if isinstance(event, Update):
            for attr in (
                "message",
                "edited_message",
                "callback_query",
                "inline_query",
                "pre_checkout_query",
                "chat_member",
                "my_chat_member",
                "chosen_inline_result",
                "shipping_query",
            ):
                obj = getattr(event, attr, None)
                if obj is not None:
                    user = getattr(obj, "from_user", None)
                    if user is not None:
                        return str(user.id)
        user = getattr(event, "from_user", None)
        if user is not None:
            return str(user.id)
        return None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        self._maybe_refresh()
        tg_id = self._extract_user_id(event)
        if tg_id and tg_id in self._banned:
            return None
        return await handler(event, data)
