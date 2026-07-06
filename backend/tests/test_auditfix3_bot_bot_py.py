"""Regression tests for finding #208 — notification_poller дедуп доставки.

Баг: порядок «send_message → _ack» без ретрая и без дедупа. Если ACK на
backend падает (сетевой сбой), запись остаётся в /pending и следующий тик
поллера ОТПРАВЛЯЕТ то же сообщение повторно — юзер (или вся admin_broadcast
рассылка) получает серию дублей, пока ACK не пройдёт.

Фикс: ACK ретраится (до 3 раз с backoff); отправленные, но ещё не
подтверждённые notif_id держатся в module-level `_sent_unacked` и повторно
в Telegram не уходят до успешного ACK. TTL-очистка страхует от вечного
подавления.

Тест гоняет РЕАЛЬНЫЙ `bot.bot.notification_poller`. В CI-окружении нет
`aiogram` (это зависимость bot-, а не backend-стека) и импортировать
тяжёлые сиблинги bot.config/handlers/... нежелательно, поэтому все внешние
модули заглушаются через sys.modules (monkeypatch авто-откатывает).
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

# Корень репозитория (там лежит пакет `bot/`) должен быть на sys.path —
# backend-тесты по умолчанию видят только backend/.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# В backend-only тест-образе смонтирован только backend/, пакета bot/ на диске
# нет → импортировать реальный bot.bot нечем. Тогда проверять нечего. В полном
# чекауте (CI: корень репо на месте) тесты гоняют реальный поллер по-настоящему.
if not os.path.isfile(os.path.join(_REPO_ROOT, "bot", "bot.py")):
    pytest.skip(
        "bot/bot.py недоступен (backend-only тест-образ) — проверяется в полном "
        "чекауте/CI",
        allow_module_level=True,
    )


def _install_stubs(monkeypatch):
    """Ставит заглушки aiogram + bot.* в sys.modules и возвращает свежий
    импорт модуля bot.bot (перечитанный поверх заглушек)."""

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    # --- aiogram (только то, что импортит bot/bot.py на уровне модуля) ---
    class _Bot:  # noqa: D401
        pass

    class _Dispatcher:
        pass

    class _TelegramForbiddenError(Exception):
        pass

    class _TelegramBadRequest(Exception):
        def __init__(self, message="bad"):
            super().__init__(message)
            self.message = message

    _mod("aiogram", Bot=_Bot, Dispatcher=_Dispatcher)
    _mod("aiogram.client")
    _mod("aiogram.client.default", DefaultBotProperties=object)
    _mod(
        "aiogram.exceptions",
        TelegramBadRequest=_TelegramBadRequest,
        TelegramForbiddenError=_TelegramForbiddenError,
    )
    _mod("aiogram.fsm")
    _mod("aiogram.fsm.storage")
    _mod("aiogram.fsm.storage.memory", MemoryStorage=object)
    _mod("aiogram.types", ErrorEvent=object)

    # --- aiohttp ---
    # Поллер строит таймаут через ``__import__("aiohttp").ClientTimeout(total=5)``
    # на КАЖДОМ тике (и в get /pending, и в _ack). aiohttp — зависимость bot-,
    # а не backend-стека, поэтому в тест-образе его нет: без заглушки импорт
    # падает ВНУТРИ формирования аргументов session.get, до инкремента
    # get_calls; broad ``except Exception`` в поллере глотает это и крутит цикл
    # вечно (CancelledError от фейк-сессии не наступает) → тест виснет. Заглушка
    # делает ClientTimeout безобидным (фейк-сессия timeout игнорирует).
    _mod("aiohttp", ClientTimeout=lambda **_kw: None)

    # --- сиблинги bot.* ---
    _mod(
        "bot.config",
        BOT_TOKEN="x",
        BACKEND_URL="http://backend",
        ADMIN_API_TOKEN="tok",
        NOTIFICATION_POLL_INTERVAL=0.001,
        BOT_WEBHOOK_PORT=0,
    )
    _kb = lambda *a, **k: None  # noqa: E731
    _mod(
        "bot.handlers",
        close_session=lambda *a, **k: None,
        router=object(),
        get_session=lambda *a, **k: None,
        onboarding_keyboard=_kb,
        health_ping_keyboard=_kb,
        node_diagnosis_keyboard=_kb,
    )
    _mod("bot.keyboards", DEFAULT_COMMANDS=[])
    _mod("bot.middleware", BanGuard=object)
    _mod("bot.support", support_router=object())

    # Свежий импорт bot.bot поверх заглушек.
    monkeypatch.delitem(sys.modules, "bot.bot", raising=False)
    import importlib

    return importlib.import_module("bot.bot")


class _FakeResp:
    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json = json_data if json_data is not None else []

    async def json(self):
        return self._json

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Фейк aiohttp-сессии.

    * GET /pending каждый тик возвращает `notifications`; на `stop_after_get`
      вызове бросает CancelledError → поллер штатно выходит.
    * POST /ack: при ack_ok=False бросает ConnectionError (симуляция
      недоступного backend'а), иначе отдаёт 200.
    """

    def __init__(self, notifications, stop_after_get=3, ack_ok=False):
        self.notifications = notifications
        self.stop_after_get = stop_after_get
        self.ack_ok = ack_ok
        self.get_calls = 0
        self.post_calls = 0

    def get(self, url, **kw):
        self.get_calls += 1
        if self.get_calls >= self.stop_after_get:
            raise asyncio.CancelledError()
        # Возвращаем свежую копию — код мог бы мутировать список.
        return _FakeResp(200, list(self.notifications))

    def post(self, url, **kw):
        self.post_calls += 1
        if not self.ack_ok:
            raise ConnectionError("ack backend down")
        return _FakeResp(200)


class _FakeBot:
    def __init__(self):
        self.sends = []

    async def send_message(self, **kw):
        self.sends.append(kw)


async def _run_poller(mod, monkeypatch, session, poll_interval=0.001):
    """Гоняет поллер, пока фейк-сессия не бросит CancelledError."""
    fake_bot = _FakeBot()

    async def _fake_get_session():
        return session

    # Мгновенный sleep, чтобы не ждать реальные backoff-паузы ACK.
    orig_sleep = asyncio.sleep

    async def _fast_sleep(_d=0):
        await orig_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(mod, "get_session", _fake_get_session)
    monkeypatch.setattr(mod, "NOTIFICATION_POLL_INTERVAL", poll_interval)

    await mod.notification_poller(fake_bot)
    return fake_bot


@pytest.mark.asyncio
async def test_failed_ack_does_not_resend_next_tick(monkeypatch):
    """Главный кейс #208: ACK падает — второй тик НЕ шлёт дубль."""
    mod = _install_stubs(monkeypatch)
    mod._sent_unacked.clear()

    notif = {"telegram_id": 111, "text": "hi", "id": 42}
    # 2 тика доставки (get_calls 1 и 2), 3-й get → cancel.
    session = _FakeSession([notif], stop_after_get=3, ack_ok=False)

    fake_bot = await _run_poller(mod, monkeypatch, session)

    # Несмотря на два тика, сообщение ушло в Telegram РОВНО один раз.
    assert len(fake_bot.sends) == 1, fake_bot.sends
    # ACK так и не прошёл — id остаётся в дедуп-наборе.
    assert 42 in mod._sent_unacked
    # ACK ретраился: 3 попытки на первом тике + 3 на втором (до-ACK).
    assert session.post_calls >= 4


@pytest.mark.asyncio
async def test_successful_ack_clears_dedup(monkeypatch):
    """Успешный ACK снимает id с дедупа (штатный happy-path)."""
    mod = _install_stubs(monkeypatch)
    mod._sent_unacked.clear()

    notif = {"telegram_id": 222, "text": "hi", "id": 7}
    session = _FakeSession([notif], stop_after_get=3, ack_ok=True)

    fake_bot = await _run_poller(mod, monkeypatch, session)

    # На втором тике /pending уже не должен возвращать эту запись в реале;
    # но даже если вернёт — важно, что после успешного ACK id снят.
    assert 7 not in mod._sent_unacked
    # send_message вызывался (хотя бы раз), без залипания.
    assert len(fake_bot.sends) >= 1


@pytest.mark.asyncio
async def test_ttl_purges_stale_unacked(monkeypatch):
    """Протухшие (старше TTL) записи дедупа очищаются на следующем тике,
    иначе notif был бы подавлён навсегда при перманентном сбое ACK."""
    mod = _install_stubs(monkeypatch)
    mod._sent_unacked.clear()

    # Кладём запись с «древним» ts (заведомо старше TTL).
    loop_now = asyncio.get_event_loop().time()
    mod._sent_unacked[99] = loop_now - (mod._SENT_UNACKED_TTL + 100)

    # Пустой /pending — просто прогоняем тик, чтобы сработала TTL-чистка.
    session = _FakeSession([], stop_after_get=2, ack_ok=False)
    await _run_poller(mod, monkeypatch, session)

    assert 99 not in mod._sent_unacked
