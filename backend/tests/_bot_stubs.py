"""Общие стабы aiogram/aiohttp/bot.* для тестов бота в backend-образе.

В backend-тест-образе нет ни aiogram, ни aiohttp (это зависимости bot-стека),
а импорт ``bot.handlers`` на уровне модуля выполняет десятки
``@router``-декораторов, аннотаций и ``class DeviceStates(StatesGroup)``.
Поэтому внешние модули подменяются через ``sys.modules`` (monkeypatch
авто-откатывает), а ``bot.handlers`` перечитывается поверх заглушек.

Раньше каждый bot-тест носил свою копию этих ~80 строк, и они разъезжались:
``test_netfix_bot_handlers_py.py`` отстал от импортов handlers.py после
9cec853 (StateFilter, aiogram.fsm.state) и 8e0eff0 (SUB_LINK_ALT_SHARE) —
семь ERROR на сборе, весь bot-срез CI красный. Один стаб — одно место для
синхрона со списком импортов ``bot/handlers.py``.

Не ``test_*.py`` — pytest его не собирает; импортировать как
``from tests._bot_stubs import ...`` (как ``tests.factories``).
"""
from __future__ import annotations

import importlib
import os
import sys
import types as _pytypes

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def bot_sources_available() -> bool:
    return os.path.isfile(os.path.join(REPO_ROOT, "bot", "handlers.py"))


def ensure_repo_root_on_path() -> None:
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)


class Any:
    """Всеядная заглушка: поддерживает произвольный доступ к атрибутам, вызовы,
    сравнения, ``|`` (для аннотаций ``X | None``) и ``.startswith`` — этого
    хватает, чтобы ``@router``-декораторы и magic-фильтры aiogram
    (``F.text == ...``) прошли на импорте."""

    __hash__ = object.__hash__

    def __getattr__(self, _n):
        return self

    def __call__(self, *a, **k):
        return self

    def __eq__(self, _o):
        return self

    def __or__(self, _o):
        return self

    def __ror__(self, _o):
        return self

    def startswith(self, *a, **k):
        return self


ANY = Any()


# ── Фейки Telegram-объектов ──


class FakeMessage:
    """Минимальный ``types.Message``: ``.answer`` пишет в ``.sent``, есть
    ``.bot`` / ``.chat`` / ``.from_user``. Это НАСТОЯЩИЙ класс в
    types-неймспейсе стаба — handlers делает
    ``isinstance(callback_query.message, types.Message)``."""

    def __init__(self, bot, chat_id: int = 100, user_id: int = 100):
        self.bot = bot
        self.chat = _pytypes.SimpleNamespace(id=chat_id)
        self.from_user = _pytypes.SimpleNamespace(id=user_id, first_name="Тест")
        self.text = ""
        self.sent: list[tuple[str, dict]] = []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


class FakeCallback:
    def __init__(self, bot, message, user_id: int = 100):
        self.bot = bot
        self.message = message
        self.from_user = _pytypes.SimpleNamespace(id=user_id, first_name="Тест")
        self.data = ""
        self.answered = 0

    async def answer(self, *a, **k):
        self.answered += 1


class KeyboardMarker:
    """Что вернул стаб ``bot.keyboards.<name>()`` — чтобы тест мог проверить,
    что к сообщению приложили именно эту клавиатуру."""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover — только для диагностики
        return f"<kb {self.name}>"


class ClientTimeout:
    def __init__(self, total=None, connect=None):
        self.total = total
        self.connect = connect


class ClientError(Exception):
    pass


class TelegramBadRequest(Exception):
    pass


def install_bot_stubs(
    monkeypatch,
    *,
    real_message_class: bool = True,
    sub_link_base_url: str = "",
    webapp_base_url: str = "",
):
    """Поставить стабы в ``sys.modules`` и вернуть свежий ``bot.handlers``.

    ``real_message_class`` — класть ``FakeMessage`` в ``types.Message`` (иначе
    ``isinstance(..., types.Message)`` в handlers упадёт на всеядном стабе).
    ``sub_link_base_url`` — ``bot.config.SUB_LINK_BASE_URL`` (нужен, чтобы
    ``_build_sub_url`` собирал ссылку без WEBAPP_BASE_URL из env).
    ``webapp_base_url`` — ``bot.keyboards.WEBAPP_BASE_URL`` стаба.
    """
    ensure_repo_root_on_path()

    def _mod(name, **attrs):
        m = _pytypes.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    class _Router:
        def _dec(self, *a, **k):
            def wrap(fn):
                return fn

            return wrap

        message = _dec
        callback_query = _dec
        pre_checkout_query = _dec

    class _StatesGroup:
        """Должен быть НАСТОЯЩИМ классом: handlers объявляет
        ``class DeviceStates(StatesGroup)`` (9cec853)."""

    types_ns = Any()
    if real_message_class:
        types_ns.Message = FakeMessage

    _mod("aiogram", F=ANY, Router=_Router, types=types_ns)
    _mod("aiogram.exceptions", TelegramBadRequest=TelegramBadRequest)
    _any_factory = lambda *a, **k: ANY  # noqa: E731
    _mod(
        "aiogram.filters",
        Command=_any_factory,
        CommandObject=ANY,
        CommandStart=_any_factory,
        StateFilter=_any_factory,  # 9cec853 (экран «Устройства»)
    )
    _mod("aiogram.fsm")
    _mod("aiogram.fsm.context", FSMContext=ANY)
    _mod("aiogram.fsm.state", State=_any_factory, StatesGroup=_StatesGroup)  # 9cec853
    _mod(
        "aiohttp",
        ClientTimeout=ClientTimeout,
        ClientError=ClientError,
        ClientSession=object,
    )

    # Держать в синхроне со списком ``from .config import (...)`` в handlers.py:
    # отставание = ImportError на сборе всего bot-среза.
    _mod(
        "bot.config",
        ADMIN_API_TOKEN="tok",
        ADMIN_IDS=set(),
        BACKEND_URL="http://backend",
        PAYMENT_PROVIDER="stub",
        PAYMENT_PROVIDER_CHOICES=(),  # Stage 9b (61217f7)
        SUB_LINK_ALT_SHARE="0",  # 8e0eff0 (два фронта саб-ссылки)
        SUB_LINK_BASE_URL=sub_link_base_url,
        SUB_LINK_BASE_URL_ALT="",  # 8e0eff0
        LEGAL_BASE_URL="",  # «О сервисе и документы» (2026-09-22)
        TELEGRAM_STARS_WEBHOOK_SECRET="secret",
    )

    def _kb(name):
        return lambda *a, **k: KeyboardMarker(name)

    _mod(
        "bot.keyboards",
        BTN_BUY="Купить",
        BTN_HELP="Помощь",
        BTN_INVITE="Пригласить",
        BTN_MAIN_MENU="Меню",
        BTN_TOPUP="Пополнить",
        BTN_VPN_BROKEN="VPN не работает",
        WEBAPP_BASE_URL=webapp_base_url,
        WEBAPP_URL=f"{webapp_base_url}/" if webapp_base_url else "",
        help_back_keyboard=_kb("help_back_keyboard"),
        about_keyboard=_kb("about_keyboard"),
        help_keyboard=_kb("help_keyboard"),
        onboarding_keyboard=_kb("onboarding_keyboard"),
        start_keyboard=_kb("start_keyboard"),
        webapp_inline_keyboard=_kb("webapp_inline_keyboard"),
        welcome_action_keyboard=_kb("welcome_action_keyboard"),
    )

    monkeypatch.delitem(sys.modules, "bot.handlers", raising=False)
    return importlib.import_module("bot.handlers")


# ── Стаб для тестов bot.keyboards (структура кнопок нужна настоящая) ──


class Btn:
    def __init__(self, text=None, callback_data=None, web_app=None, url=None, **kw):
        self.text = text
        self.callback_data = callback_data
        self.web_app = web_app
        # URL-кнопки (ссылки на документы в «О сервисе», 2026-09-22).
        self.url = url


class Markup:
    def __init__(self, inline_keyboard=None, keyboard=None, **kw):
        self.inline_keyboard = inline_keyboard or []
        self.keyboard = keyboard or []


class WebAppInfo:
    def __init__(self, url=None):
        self.url = url


def load_keyboards(monkeypatch, *, webapp_base_url: str = "https://example.test/app",
                   app_version: str = "9.9.9"):
    """``bot.keyboards`` поверх минимального aiogram-стаба с настоящими (пусть и
    примитивными) классами кнопок — иначе структуру клавиатур не проверить.
    Вызывающий обязан снять модуль после теста:
    ``sys.modules.pop("bot.keyboards", None)``."""
    ensure_repo_root_on_path()
    types_mod = _pytypes.SimpleNamespace(
        InlineKeyboardButton=Btn,
        InlineKeyboardMarkup=Markup,
        KeyboardButton=Btn,
        ReplyKeyboardMarkup=Markup,
        WebAppInfo=WebAppInfo,
        BotCommand=lambda **kw: _pytypes.SimpleNamespace(**kw),
    )
    monkeypatch.setitem(sys.modules, "aiogram", _pytypes.ModuleType("aiogram"))
    sys.modules["aiogram"].types = types_mod
    monkeypatch.setitem(sys.modules, "aiogram.types", types_mod)
    monkeypatch.setenv("WEBAPP_BASE_URL", webapp_base_url)
    # Пин: в тест-контейнере APP_VERSION приходит из compose-env, без пина
    # URL кнопок «плавал» бы между окружениями (кэшбастер ?v=).
    monkeypatch.setenv("APP_VERSION", app_version)
    return importlib.reload(importlib.import_module("bot.keyboards"))
