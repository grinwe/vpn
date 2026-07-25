"""Регресс-тесты сетевого аудита для bot/handlers.py.

Покрывают точечные фиксы:

* Finding #2 — `_fetch_json`: короткий per-attempt таймаут для ретраящихся
  GET (иначе 3 попытки по total=10с = ~30с тишины); ретраи ограничены,
  недоступный бэк отдаёт status=0 быстро. Явный timeout от caller уважается.
* Finding #3 — `/start` register: не-GET (POST) без явного timeout не получает
  короткий таймаут и делает ОДНУ попытку (не плодит дублей), а явный timeout
  (3с у register) пробрасывается в запрос.
* Finding #6 — `_send_referral`/`_get_bot_username`: username бота кэшируется
  (один get_me), а сетевой блип отдаёт None вместо необработанного исключения.
* Finding #7 — `_poll_ops_exec`: N подряд сетевых провалов → маркер
  `unreachable` вместо оптимистичного «executing»; успех сбрасывает счётчик.

В backend-тест-образе нет ни aiogram, ни aiohttp (это зависимости bot-стека),
а импорт bot.handlers на уровне модуля выполняет десятки @router-декораторов и
аннотаций. Поэтому все внешние модули заглушаются через sys.modules
(monkeypatch авто-откатывает), а bot.handlers перечитывается поверх заглушек.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types as _pytypes

import pytest

# Корень репозитория (там лежит пакет `bot/`) на sys.path.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

if not os.path.isfile(os.path.join(_REPO_ROOT, "bot", "handlers.py")):
    pytest.skip(
        "bot/handlers.py недоступен (backend-only тест-образ) — проверяется в "
        "полном чекауте/CI",
        allow_module_level=True,
    )


class _Any:
    """Всеядная заглушка: поддерживает произвольный доступ к атрибутам, вызовы,
    сравнения, `|` (для аннотаций `X | None`) и `.startswith` — этого хватает,
    чтобы @router-декораторы и magic-фильтры aiogram (`F.text == ...`) прошли на
    импорте."""

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


_ANY = _Any()


def _install_stubs(monkeypatch):
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

    class _ClientTimeout:
        def __init__(self, total=None, connect=None):
            self.total = total
            self.connect = connect

    class _ClientError(Exception):
        pass

    class _TelegramBadRequest(Exception):
        pass

    _mod("aiogram", F=_ANY, Router=_Router, types=_ANY)
    _mod("aiogram.exceptions", TelegramBadRequest=_TelegramBadRequest)
    _any_factory = lambda *a, **k: _ANY  # noqa: E731
    _mod(
        "aiogram.filters",
        Command=_any_factory,
        CommandObject=_ANY,
        CommandStart=_any_factory,
    )
    _mod("aiogram.fsm")
    _mod("aiogram.fsm.context", FSMContext=_ANY)
    _mod(
        "aiohttp",
        ClientTimeout=_ClientTimeout,
        ClientError=_ClientError,
        ClientSession=object,
    )

    _kb = lambda *a, **k: None  # noqa: E731
    _mod(
        "bot.config",
        ADMIN_API_TOKEN="tok",
        ADMIN_IDS=set(),
        BACKEND_URL="http://backend",
        PAYMENT_PROVIDER="stub",
        # Stage 9b (61217f7) добавил этот импорт в handlers.py, а стаб не
        # обновили — тест валился ImportError'ом на СБОРЕ, т.е. падал весь
        # bot-срез в CI. Держим стаб в синхроне со списком импортов handlers.
        PAYMENT_PROVIDER_CHOICES=(),
        SUB_LINK_BASE_URL="",
        TELEGRAM_STARS_WEBHOOK_SECRET="secret",
    )
    _mod(
        "bot.keyboards",
        BTN_BUY="Купить",
        BTN_HELP="Помощь",
        BTN_INVITE="Пригласить",
        BTN_MAIN_MENU="Меню",
        BTN_TOPUP="Пополнить",
        BTN_VPN_BROKEN="VPN не работает",
        WEBAPP_BASE_URL="",
        help_back_keyboard=_kb,
        help_keyboard=_kb,
        onboarding_keyboard=_kb,
        start_keyboard=_kb,
        webapp_inline_keyboard=_kb,
        welcome_action_keyboard=_kb,
    )

    monkeypatch.delitem(sys.modules, "bot.handlers", raising=False)
    import importlib

    return importlib.import_module("bot.handlers")


# ── Фейки сессий/ответов ──


class _Resp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status = status
        self._json = json_data if json_data is not None else {}
        self._text = text

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _ScriptSession:
    """request() проигрывает сценарий: элемент-Exception → raise, иначе трактуем
    как готовый _Resp (или json-dict со статусом 200). Пишет переданный timeout."""

    def __init__(self, script):
        self.script = script
        self.i = 0
        self.calls = 0
        self.timeouts: list = []

    def request(self, method, url, **kw):
        self.calls += 1
        self.timeouts.append(kw.get("timeout"))
        item = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, _Resp):
            return item
        return _Resp(200, item)


@pytest.fixture()
def handlers(monkeypatch):
    mod = _install_stubs(monkeypatch)
    # Мгновенный sleep — не ждём реальные backoff/poll паузы.
    orig_sleep = asyncio.sleep

    async def _fast_sleep(_d=0):
        await orig_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    return mod


# ── Finding #2 / #3 — _fetch_json таймаут и ретраи ──


@pytest.mark.asyncio
async def test_fetch_json_get_retries_bounded_and_short_timeout(handlers, monkeypatch):
    session = _ScriptSession([asyncio.TimeoutError()])  # всегда таймаут

    async def _fake_get_session():
        return session

    monkeypatch.setattr(handlers, "get_session", _fake_get_session)

    status, payload = await handlers._fetch_json("GET", "http://backend/api/plans")

    # Недоступный бэк → быстрый честный status=0, а не исключение наружу.
    assert status == 0
    # GET ретраится ровно _RETRIES+1 раз (не больше — ретраи ограничены).
    assert session.calls == handlers._RETRIES + 1
    # На идемпотентный GET навешивается КОРОТКИЙ per-attempt таймаут (не total=10).
    assert session.timeouts[0] is not None
    assert session.timeouts[0].total == handlers._RETRY_ATTEMPT_TIMEOUT_S


@pytest.mark.asyncio
async def test_fetch_json_post_single_attempt_no_injected_timeout(handlers, monkeypatch):
    session = _ScriptSession([asyncio.TimeoutError()])

    async def _fake_get_session():
        return session

    monkeypatch.setattr(handlers, "get_session", _fake_get_session)

    status, _ = await handlers._fetch_json("POST", "http://backend/api/invoices")

    # Неидемпотентный POST по умолчанию НЕ ретраится (без дублей) …
    assert status == 0
    assert session.calls == 1
    # … и не получает короткий таймаут — остаётся на общем таймауте сессии.
    assert session.timeouts[0] is None


@pytest.mark.asyncio
async def test_fetch_json_explicit_timeout_is_honored(handlers, monkeypatch):
    # /start передаёт register короткий 3с таймаут — он должен доехать до запроса.
    session = _ScriptSession([asyncio.TimeoutError()])

    async def _fake_get_session():
        return session

    monkeypatch.setattr(handlers, "get_session", _fake_get_session)
    ct = sys.modules["aiohttp"].ClientTimeout(total=3)

    status, _ = await handlers._fetch_json(
        "POST", "http://backend/api/users/register", timeout=ct
    )

    assert status == 0
    assert session.calls == 1
    assert session.timeouts[0] is ct
    assert session.timeouts[0].total == 3


# ── Finding #6 — _get_bot_username кэш + устойчивость ──


class _FakeBotGetMe:
    def __init__(self, username="mybot", fail=False):
        self.username = username
        self.fail = fail
        self.calls = 0

    async def get_me(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("network blip to telegram")

        class _Me:
            pass

        me = _Me()
        me.username = self.username
        return me


@pytest.mark.asyncio
async def test_get_bot_username_is_cached(handlers):
    handlers._bot_username = None
    bot = _FakeBotGetMe(username="grn_bot")

    assert await handlers._get_bot_username(bot) == "grn_bot"
    assert await handlers._get_bot_username(bot) == "grn_bot"
    # get_me дёрнут лишь однажды — username бота неизменен в рамках процесса.
    assert bot.calls == 1


@pytest.mark.asyncio
async def test_get_bot_username_returns_none_on_network_error(handlers):
    handlers._bot_username = None
    bot = _FakeBotGetMe(fail=True)

    # Сетевой блип к Telegram не должен пробрасываться наружу — вернём None,
    # чтобы вызывающий деградировал, а не отдал юзеру generic-ошибку.
    assert await handlers._get_bot_username(bot) is None


# ── Finding #7 — _poll_ops_exec обнаруживает недоступность ──


@pytest.mark.asyncio
async def test_poll_ops_exec_reports_unreachable_after_consecutive_failures(handlers):
    # Бэк недоступен весь поллинг → после порога подряд провалов явный маркер.
    session = _ScriptSession([asyncio.TimeoutError()])

    result = await handlers._poll_ops_exec(session, 7, 42, tries=20, delay=0)

    assert result.get("status") == "unreachable"
    assert result.get("plan_id") == 7
    # Вышли ровно на пороге, а не крутили все 20 итераций.
    assert session.calls == handlers._OPS_POLL_MAX_CONSEC_FAIL


@pytest.mark.asyncio
async def test_poll_ops_exec_transient_failures_do_not_mask_terminal(handlers):
    # Провалы НИЖЕ порога сбрасываются успешным ответом и не прячут терминал.
    n = handlers._OPS_POLL_MAX_CONSEC_FAIL - 1
    script = [asyncio.TimeoutError()] * n + [{"status": "executed", "plan_id": 7}]
    session = _ScriptSession(script)

    result = await handlers._poll_ops_exec(session, 7, 42, tries=20, delay=0)

    assert result.get("status") == "executed"
