"""Выдача саб-ссылки ботом: /config, триал, go:config, клавиатуры.

Инцидент 2026-08-25 21:49 МСК (user 1000054, подписка 11055): новый юзер тапнул
«🎁 Забрать бесплатный месяц», бот написал «Готово! … Сейчас пришлю ссылку» — и
ссылка не пришла. Причина: в ``cmd_config`` вложенный ``_say`` при живом
``message`` звал САМ СЕБЯ вместо ``message.answer`` (5d696f6) → RecursionError.
Ломало /config, «Мой конфиг» и выдачу после триала у ВСЕХ. Повторный тап дал
409 → «Подарок уже был использован … Тарифы:» + прайс — тупик.

Что закрепляем:

* ``cmd_config`` шлёт два сообщения (шапка + голый URL) тем же путём, что
  пришёл запрос (``message.answer`` / ``bot.send_message``), возвращает True;
  честные статусы «ещё создаётся» / «заморожена»; ``quiet_if_missing`` —
  молчит и отдаёт False. ``intro`` клеится к первому сообщению, каким бы оно
  ни было; шапка без абзаца про ЛК (28.08: 7 сообщений за один тап).
* ``trial_activate_cb``: «месяц активирован» и ссылка — ОДНИМ сообщением
  (+ голый URL), итого два; падение выдачи → фолбэк на ЛК с тем же
  заголовком, а не тишина; 409 с живой подпиской → ссылка без прайса;
  409 без подписки → прайс; 409 после провала провижининга → честный текст.
* ``go:config`` зовёт ``cmd_config`` от имени инициатора.
* Клавиатуры: «🔗 Ссылка для подключения» у юзера с подпиской (active/frozen)
  или устройствами — и у новичка с незабранным подарком тоже; ЛК первой
  строкой в ``onboarding_keyboard``.
* Структурно: ни один вложенный async-хелпер в handlers.py не зовёт сам себя.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys

import pytest

from tests._bot_stubs import (
    REPO_ROOT,
    Btn,
    FakeBot,
    FakeCallback,
    FakeMessage,
    KeyboardMarker,
    Markup,
    bot_sources_available,
    install_bot_stubs,
    load_keyboards,
)

if not bot_sources_available():
    pytest.skip("bot/handlers.py недоступен в этом образе", allow_module_level=True)


SUB_BASE = "https://sub.test/s"
LINK = f"{SUB_BASE}/tok123"
LIVE_DETAIL = "User already has a live subscription"

_ACTIVE_SUBS = [
    {
        "status": "active",
        "sub_token": "tok123",
        "credentials": [{"protocol": "vless-reality"}],
        "devices": [{"status": "active"}],
    }
]
_PENDING_SUBS = [
    {"status": "active", "sub_token": "tok123", "credentials": [],
     "devices": [{"status": "pending"}]}
]
_FROZEN_SUBS = [
    {"status": "frozen", "sub_token": "tok123",
     "credentials": [{"protocol": "vless-reality", "status": "frozen"}],
     "devices": [{"status": "frozen"}]}
]
_PLANS = [
    {"id": 1, "name": "Solo", "price": 199, "max_devices": 1,
     "duration_days": 30, "is_visible": True}
]


@pytest.fixture()
def handlers(monkeypatch):
    mod = install_bot_stubs(monkeypatch, sub_link_base_url=SUB_BASE)
    orig_sleep = asyncio.sleep

    async def _fast_sleep(_d=0):
        await orig_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    return mod


def _script_fetch(handlers, monkeypatch, routes: dict):
    """Подменяем ``_fetch_json`` целиком: (url-хвост) → (status, payload).
    Проще, чем изображать aiohttp-сессию, и не зависит от ретраев/таймаутов."""
    calls: list[tuple[str, str]] = []

    async def _fake(method, url, **kw):
        calls.append((method, url))
        for key, resp in routes.items():
            if url.endswith(key):
                return resp
        raise AssertionError(f"неожиданный запрос {method} {url}")

    monkeypatch.setattr(handlers, "_fetch_json", _fake)
    return calls


def _flags(trial_available: bool, has_devices: bool, has_subscription: bool = False):
    async def _f(_uid):
        return trial_available, has_devices, has_subscription

    return _f


def _bot_texts(bot: FakeBot) -> list[str]:
    return [t for _c, t, _k in bot.sent]


def _msg_texts(msg: FakeMessage) -> list[str]:
    return [t for t, _k in msg.sent]


# ── cmd_config ──


@pytest.mark.asyncio
async def test_cmd_config_with_message_sends_header_and_link(handlers, monkeypatch):
    _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": (200, _ACTIVE_SUBS)})
    bot = FakeBot()
    msg = FakeMessage(bot)

    assert await handlers.cmd_config(msg) is True

    assert len(msg.sent) == 2, msg.sent
    header, header_kw = msg.sent[0]
    assert header.startswith("🔗 Твоя ссылка для подключения")
    assert "Выбери платформу" in header
    # Без intro шапка начинается со ссылки, а не с «🎉».
    assert "🎉" not in header
    # Абзац про ЛК/config из шапки убран: кнопка ЛК первой строкой в
    # onboarding_keyboard, menu-button ЛК у поля ввода; текстом это повторялось
    # до четырёх раз за один тап (жалоба владельца 28.08). «Один раз, потом
    # клиент сам подтянет» уехало в платформенные инструкции.
    assert "личном кабинете" not in header and "/config" not in header
    assert "один раз" not in header
    assert isinstance(header_kw.get("reply_markup"), KeyboardMarker)
    assert header_kw["reply_markup"].name == "onboarding_keyboard"
    assert LINK in msg.sent[1][0]
    # Путь message!=None → только message.answer, bot.send_message не трогаем.
    assert bot.sent == []


@pytest.mark.asyncio
async def test_cmd_config_intro_is_glued_to_header(handlers, monkeypatch):
    _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": (200, _ACTIVE_SUBS)})
    msg = FakeMessage(FakeBot())

    assert await handlers.cmd_config(msg, intro="🎉 Заголовок.") is True

    assert len(msg.sent) == 2
    header = msg.sent[0][0]
    assert header.startswith("🎉 Заголовок.\n\n🔗 Твоя ссылка для подключения")
    assert LINK in msg.sent[1][0] and "🎉" not in msg.sent[1][0]


@pytest.mark.asyncio
async def test_cmd_config_intro_is_glued_to_pending_status(handlers, monkeypatch):
    """Cold-путь после триала: «месяц активирован» + «ещё создаётся» одним
    сообщением, а не «ещё создаётся» перед «активирован»."""
    _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": (200, _PENDING_SUBS)})
    msg = FakeMessage(FakeBot())

    assert await handlers.cmd_config(msg, quiet_if_missing=True, intro="🎉 Заголовок.") is False

    texts = _msg_texts(msg)
    assert len(texts) == 1
    assert texts[0].startswith("🎉 Заголовок.\n\n") and "ещё создаётся" in texts[0]


@pytest.mark.asyncio
async def test_cmd_config_intro_alone_when_quiet_has_nothing_to_say(handlers, monkeypatch):
    """Тихий режим без подписок: слать нечего, но факт активации юзер должен
    увидеть — заголовок уходит сам по себе."""
    _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": (200, [])})
    msg = FakeMessage(FakeBot())

    assert await handlers.cmd_config(msg, quiet_if_missing=True, intro="🎉 Заголовок.") is False

    assert _msg_texts(msg) == ["🎉 Заголовок."]


@pytest.mark.asyncio
async def test_cmd_config_without_message_uses_bot_send_message(handlers, monkeypatch):
    _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": (200, _ACTIVE_SUBS)})
    bot = FakeBot()

    assert await handlers.cmd_config(None, user_id=100, bot=bot, chat_id=555) is True

    assert [c for c, _t, _k in bot.sent] == [555, 555]
    assert LINK in bot.sent[1][1]


@pytest.mark.asyncio
async def test_cmd_config_pending_device_says_wait(handlers, monkeypatch):
    _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": (200, _PENDING_SUBS)})
    msg = FakeMessage(FakeBot())

    assert await handlers.cmd_config(msg) is False

    assert _msg_texts(msg) and "ещё создаётся" in _msg_texts(msg)[0]


@pytest.mark.asyncio
async def test_cmd_config_frozen_says_frozen_not_no_subscriptions(handlers, monkeypatch):
    """Замороженному раньше писали «нет активных подписок» — ложь, уводящая
    в прайс: подписка есть, её надо разморозить."""
    _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": (200, _FROZEN_SUBS)})
    msg = FakeMessage(FakeBot())

    assert await handlers.cmd_config(msg) is False

    texts = _msg_texts(msg)
    assert len(texts) == 1
    assert "заморожена" in texts[0] and "/status" in texts[0]
    assert "нет активных подписок" not in texts[0]


@pytest.mark.asyncio
async def test_cmd_config_quiet_if_missing_is_silent(handlers, monkeypatch):
    for subs_resp in ((404, {"detail": "not found"}), (200, [])):
        _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": subs_resp})
        bot = FakeBot()
        msg = FakeMessage(bot)

        assert await handlers.cmd_config(msg, quiet_if_missing=True) is False

        assert msg.sent == [] and bot.sent == [], subs_resp


@pytest.mark.asyncio
async def test_cmd_config_quiet_still_reports_frozen(handlers, monkeypatch):
    """Заморозка — честный статус подписки (как «ещё создаётся»), а не отсылка
    к покупке: шлём и в тихом режиме, возвращаем False. Иначе после 409 «у тебя
    уже есть подписка» замороженный получал бы только «если ссылка не пришла»."""
    _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": (200, _FROZEN_SUBS)})
    msg = FakeMessage(FakeBot())

    assert await handlers.cmd_config(msg, quiet_if_missing=True) is False

    texts = _msg_texts(msg)
    assert len(texts) == 1 and "заморожена" in texts[0] and "/status" in texts[0]


@pytest.mark.asyncio
async def test_cmd_config_quiet_still_reports_pending(handlers, monkeypatch):
    """«Ещё создаётся» — честный статус, а не отсылка к покупке: шлём и в тихом
    режиме (после «Готово!» это ровно то, что происходит)."""
    _script_fetch(handlers, monkeypatch, {"/api/users/by_telegram/100": (200, _PENDING_SUBS)})
    msg = FakeMessage(FakeBot())

    assert await handlers.cmd_config(msg, quiet_if_missing=True) is False

    assert "ещё создаётся" in _msg_texts(msg)[0]


# ── trial_activate_cb ──


@pytest.mark.asyncio
async def test_trial_activate_200_delivers_link(handlers, monkeypatch):
    _script_fetch(handlers, monkeypatch, {
        "/api/trial/activate_full": (200, {"expires_at": "2026-09-25T00:00:00"}),
        "/api/users/by_telegram/100": (200, _ACTIVE_SUBS),
    })
    bot = FakeBot()
    msg = FakeMessage(bot)
    cb = FakeCallback(bot, msg)

    await handlers.trial_activate_cb(cb)

    assert cb.answered == 1
    # Отдельного «Готово!» нет: заголовок приклеен к шапке со ссылкой.
    assert bot.sent == [], bot.sent
    assert len(msg.sent) == 2, msg.sent
    header, header_kw = msg.sent[0]
    assert header.startswith("🎉 Бесплатный месяц активирован до 25.09.2026.")
    assert "Твоя ссылка" in header
    assert header_kw["reply_markup"].name == "onboarding_keyboard"
    # Ни «около минуты» (правда только на cold-пути, там свой текст), ни
    # абзаца про ЛК/config (кнопки уже в клавиатуре).
    assert "минут" not in header and "личном кабинете" not in header
    # Обещанная ссылка ДОЛЖНА прийти (до фикса — RecursionError в _say).
    assert LINK in msg.sent[1][0]
    # Прайс после успешной активации не показываем.
    assert not any("тариф" in t.lower() for t in _msg_texts(msg) + _bot_texts(bot))


@pytest.mark.asyncio
async def test_trial_activate_200_cold_path_glues_done_to_wait_status(handlers, monkeypatch):
    """Cold-путь: девайс pending, ссылка ещё не рабочая. Юзер получает
    «активирован + ещё создаётся» одним сообщением и подсказку, где забрать."""
    _script_fetch(handlers, monkeypatch, {
        "/api/trial/activate_full": (200, {"expires_at": "2026-09-25T00:00:00"}),
        "/api/users/by_telegram/100": (200, _PENDING_SUBS),
    })
    bot = FakeBot()
    msg = FakeMessage(bot)
    cb = FakeCallback(bot, msg)

    await handlers.trial_activate_cb(cb)

    texts = _msg_texts(msg)
    assert len(texts) == 1 and texts[0].startswith("🎉 Бесплатный месяц активирован")
    assert "ещё создаётся" in texts[0]
    hint = _bot_texts(bot)
    assert len(hint) == 1 and "личном кабинете" in hint[0] and "активирован" not in hint[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activate_resp",
    [
        (200, {"expires_at": "2026-09-25T00:00:00"}),
        (409, {"detail": LIVE_DETAIL}),
    ],
    ids=["200", "409-live"],
)
async def test_successful_delivery_is_exactly_two_messages(handlers, monkeypatch, activate_resp):
    """Инвариант 28.08: за тап по подарку при успешной выдаче приходят ровно
    два сообщения (заголовок + шапка со ссылкой, голый URL). Было семь."""
    _script_fetch(handlers, monkeypatch, {
        "/api/trial/activate_full": activate_resp,
        "/api/users/by_telegram/100": (200, _ACTIVE_SUBS),
    })
    bot = FakeBot()
    msg = FakeMessage(bot)
    cb = FakeCallback(bot, msg)

    await handlers.trial_activate_cb(cb)

    total = len(bot.sent) + len(msg.sent)
    assert total == 2, (bot.sent, msg.sent)
    assert LINK in msg.sent[-1][0]


@pytest.mark.asyncio
async def test_trial_activate_200_inaccessible_message_goes_via_bot(handlers, monkeypatch):
    """Кнопка на сообщении старше 48ч: message — не Message, путь bot/chat_id."""
    _script_fetch(handlers, monkeypatch, {
        "/api/trial/activate_full": (200, {"expires_at": "2026-09-25T00:00:00"}),
        "/api/users/by_telegram/100": (200, _ACTIVE_SUBS),
    })
    bot = FakeBot()
    cb = FakeCallback(bot, object(), user_id=100)

    await handlers.trial_activate_cb(cb)

    assert any(LINK in t for t in _bot_texts(bot)), bot.sent
    assert all(c == 100 for c, _t, _k in bot.sent)
    assert len(bot.sent) == 2 and bot.sent[0][1].startswith("🎉 Бесплатный месяц активирован")


@pytest.mark.asyncio
async def test_trial_activate_200_delivery_crash_falls_back_to_cabinet(handlers, monkeypatch):
    """Обещание ссылки никогда не обрывается молча: cmd_config упал → фолбэк на
    ЛК и /config, исключение наружу не вылетает."""
    _script_fetch(handlers, monkeypatch, {
        "/api/trial/activate_full": (200, {"expires_at": "2026-09-25T00:00:00"}),
    })

    async def _boom(*a, **k):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(handlers, "cmd_config", _boom)
    bot = FakeBot()
    msg = FakeMessage(bot)
    cb = FakeCallback(bot, msg)

    await handlers.trial_activate_cb(cb)  # не должно бросить

    texts = _bot_texts(bot)
    fallback = [(t, k) for _c, t, k in bot.sent if "не удалось прислать" in t]
    assert fallback, texts
    # Заголовок «месяц активирован» едет вместе с фолбэком: cmd_config упал,
    # сам его не отправил, а факт активации юзер должен увидеть.
    assert fallback[0][0].startswith("🎉 Бесплатный месяц активирован до 25.09.2026.")
    assert "личном кабинете" in fallback[0][0] and "/config" in fallback[0][0]
    assert isinstance(fallback[0][1].get("reply_markup"), KeyboardMarker)
    assert fallback[0][1]["reply_markup"].name == "webapp_inline_keyboard"


@pytest.mark.asyncio
async def test_trial_activate_200_without_link_yet_points_to_cabinet(handlers, monkeypatch):
    """by_telegram ничего не отдал (тихий режим) → не «нет подписок / /plans»
    сразу после «Готово!», а подсказка, где ссылку забрать потом."""
    _script_fetch(handlers, monkeypatch, {
        "/api/trial/activate_full": (200, {"expires_at": "2026-09-25T00:00:00"}),
        "/api/users/by_telegram/100": (200, []),
    })
    bot = FakeBot()
    msg = FakeMessage(bot)
    cb = FakeCallback(bot, msg)

    await handlers.trial_activate_cb(cb)

    texts = _msg_texts(msg) + _bot_texts(bot)
    # Заголовок ушёл сам по себе (cmd_config в тихом режиме ничего не сказал),
    # затем подсказка про ЛК — и ничего про /plans.
    assert len(texts) == 2, texts
    assert texts[0] == "🎉 Бесплатный месяц активирован до 25.09.2026."
    assert "личном кабинете" in texts[1]
    assert not any("/plans" in t for t in texts)


@pytest.mark.asyncio
async def test_trial_activate_409_live_subscription_sends_link_not_prices(handlers, monkeypatch):
    """Ровно экран user 1000054 после провала первой выдачи: раньше — «подарок
    уже использован» + прайс, теперь — ссылка."""
    calls = _script_fetch(handlers, monkeypatch, {
        "/api/trial/activate_full": (409, {"detail": LIVE_DETAIL}),
        "/api/users/by_telegram/100": (200, _ACTIVE_SUBS),
        "/api/plans": (200, _PLANS),
    })
    bot = FakeBot()
    msg = FakeMessage(bot)
    cb = FakeCallback(bot, msg)

    await handlers.trial_activate_cb(cb)

    texts = _msg_texts(msg)
    # «Уже есть подписка» — заголовок шапки со ссылкой, не отдельное сообщение.
    assert bot.sent == [], bot.sent
    assert len(texts) == 2 and texts[0].startswith("У тебя уже есть подписка 😉\n\n🔗")
    assert LINK in texts[1]
    assert not any("тариф" in t.lower() for t in texts)
    assert not any("Подарок уже был использован" in t for t in texts)
    assert ("GET", "http://backend/api/plans") not in calls


@pytest.mark.asyncio
async def test_trial_activate_409_without_subscription_shows_prices(handlers, monkeypatch):
    """detail неизвестен, ссылки нет → прежнее поведение: текст + прайс, но
    без ложного триал-оффера и с отсылкой на ЛК вместо голого /config."""
    _script_fetch(handlers, monkeypatch, {
        "/api/trial/activate_full": (409, {"message": "not json"}),
        "/api/users/by_telegram/100": (404, {"detail": "not found"}),
        "/api/plans": (200, _PLANS),
    })
    monkeypatch.setattr(handlers, "_fetch_user_flags", _flags(False, False))
    bot = FakeBot()
    msg = FakeMessage(bot)
    cb = FakeCallback(bot, msg)

    await handlers.trial_activate_cb(cb)

    texts = _bot_texts(bot)
    assert any("личном кабинете" in t and "/config" in t for t in texts), texts
    assert msg.sent and "тариф" in msg.sent[0][0].lower()
    assert "Забрать бесплатный месяц" not in msg.sent[0][0]


@pytest.mark.asyncio
async def test_trial_activate_409_provisioning_failure_is_honest(handlers, monkeypatch):
    """409 со str(RuntimeError) — бонус зачислен, подписки нет: прайс бесполезен,
    нужен честный текст и поддержка."""
    calls = _script_fetch(handlers, monkeypatch, {
        "/api/trial/activate_full": (409, {"detail": "Device limit reached"}),
        "/api/plans": (200, _PLANS),
    })
    bot = FakeBot()
    msg = FakeMessage(bot)
    cb = FakeCallback(bot, msg)

    await handlers.trial_activate_cb(cb)

    texts = _bot_texts(bot)
    assert len(texts) == 1 and "зачислен" in texts[0] and "/help" in texts[0]
    assert msg.sent == []
    assert ("GET", "http://backend/api/plans") not in calls


# ── go:config ──


@pytest.mark.asyncio
async def test_go_config_calls_cmd_config_as_initiator(handlers, monkeypatch):
    seen: list[tuple] = []

    async def _fake_cmd_config(message, user_id=None, **kw):
        seen.append((message, user_id, kw))
        return True

    monkeypatch.setattr(handlers, "cmd_config", _fake_cmd_config)
    bot = FakeBot()
    msg = FakeMessage(bot, user_id=999)  # from_user сообщения — бот
    cb = FakeCallback(bot, msg, user_id=42)

    await handlers.go_config(cb)

    assert cb.answered == 1
    assert seen == [(msg, 42, {})]


@pytest.mark.asyncio
async def test_go_config_inaccessible_message_uses_bot_path(handlers, monkeypatch):
    seen: list[tuple] = []

    async def _fake_cmd_config(message, user_id=None, **kw):
        seen.append((message, user_id, kw))
        return True

    monkeypatch.setattr(handlers, "cmd_config", _fake_cmd_config)
    bot = FakeBot()
    cb = FakeCallback(bot, object(), user_id=42)

    await handlers.go_config(cb)

    assert seen == [(None, 42, {"bot": bot, "chat_id": 42})]


# ── list_plans: путь к ссылке у юзера с подпиской ──


@pytest.mark.asyncio
async def test_list_plans_with_devices_mentions_cabinet_before_prices(handlers, monkeypatch):
    """«💎 Подписка» у юзера с подпиской = голый прайс без пути к ссылке — тупик."""
    _script_fetch(handlers, monkeypatch, {"/api/plans": (200, _PLANS)})
    monkeypatch.setattr(handlers, "_fetch_user_flags", _flags(False, True))
    msg = FakeMessage(FakeBot())

    await handlers.list_plans(msg)

    text = msg.sent[0][0]
    assert "уже есть подписка" in text and "личном кабинете" in text
    assert text.index("уже есть подписка") < text.index("Все тарифы")


@pytest.mark.asyncio
async def test_list_plans_without_devices_has_no_cabinet_line(handlers, monkeypatch):
    _script_fetch(handlers, monkeypatch, {"/api/plans": (200, _PLANS)})
    monkeypatch.setattr(handlers, "_fetch_user_flags", _flags(False, False))
    msg = FakeMessage(FakeBot())

    await handlers.list_plans(msg)

    assert "уже есть подписка" not in msg.sent[0][0]


@pytest.mark.asyncio
async def test_list_plans_subscription_without_active_devices_still_shows_link_path(
    handlers, monkeypatch
):
    """Cold-путь (девайс ~минуту pending) и заморозка: has_devices=False, но
    подписка есть — путь к ссылке/статусу обязан быть, иначе голый прайс."""
    _script_fetch(handlers, monkeypatch, {"/api/plans": (200, _PLANS)})
    monkeypatch.setattr(
        handlers, "_fetch_user_flags", _flags(False, False, has_subscription=True)
    )
    # Всеядный стаб aiogram.types не даёт заглянуть в клавиатуру — подменяем
    # кнопку/разметку настоящими (примитивными) классами.
    monkeypatch.setattr(handlers.types, "InlineKeyboardButton", Btn)
    monkeypatch.setattr(handlers.types, "InlineKeyboardMarkup", Markup)
    msg = FakeMessage(FakeBot())

    await handlers.list_plans(msg)

    text, kw = msg.sent[0]
    assert "уже есть подписка" in text
    first_row = kw["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in first_row] == ["go:config"]


@pytest.mark.asyncio
async def test_fetch_user_flags_returns_three_flags(handlers, monkeypatch):
    _script_fetch(handlers, monkeypatch, {
        "/api/users/register": (200, {
            "trial_available": False, "has_devices": False, "has_subscription": True,
        }),
    })
    assert await handlers._fetch_user_flags(100) == (False, False, True)

    # Fail-safe при недоступном бэкенде — как раньше: оффер показать, остальное нет.
    async def _down(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(handlers, "_fetch_json", _down)
    assert await handlers._fetch_user_flags(100) == (True, False, False)


# ── keyboards ──


@pytest.fixture()
def kb(monkeypatch):
    mod = load_keyboards(monkeypatch)
    yield mod
    sys.modules.pop("bot.keyboards", None)


def _buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


def test_welcome_keyboard_link_button_only_with_link(kb):
    with_link = _buttons(kb.welcome_action_keyboard(trial_available=False, has_link=True))
    without = _buttons(kb.welcome_action_keyboard(trial_available=False, has_link=False))

    assert [b.callback_data for b in with_link if b.callback_data == "go:config"] == ["go:config"]
    # Второй строкой, сразу после ЛК.
    assert with_link[0].text == "🔐 Открыть личный кабинет"
    assert with_link[1].text == "🔗 Ссылка для подключения"
    assert not any(b.callback_data == "go:config" for b in without)


def test_welcome_keyboard_onboarding_keeps_link_after_gift(kb):
    """Платный юзер с незабранным подарком не должен терять путь к ссылке:
    в онбординг-раскладке кнопка ссылки идёт сразу после подарка. Новичку без
    подписки предлагать нечего — кнопки нет, экран остаётся «одно действие»."""
    with_link = _buttons(kb.welcome_action_keyboard(trial_available=True, has_link=True))
    assert with_link[0].callback_data == "trial:activate"
    assert with_link[1].text == "🔗 Ссылка для подключения"
    assert with_link[1].callback_data == "go:config"
    assert with_link[2].callback_data == "go:plans"

    without = _buttons(kb.welcome_action_keyboard(trial_available=True, has_link=False))
    assert not any(b.callback_data == "go:config" for b in without)
    assert len(without) == 2


def test_onboarding_keyboard_has_cabinet_first(kb, monkeypatch):
    btns = _buttons(kb.onboarding_keyboard())
    assert btns[0].text == "🔐 Открыть личный кабинет"
    assert btns[0].web_app.url == "https://example.test/app/?v=9.9.9"
    assert [b.callback_data for b in btns[1:]] == [
        "onboard:android", "onboard:ios", "onboard:windows", "onboard:macos"
    ]

    # Без https-WebApp кнопки ЛК нет — как в webapp_inline_keyboard.
    monkeypatch.setenv("WEBAPP_BASE_URL", "")
    import importlib

    mod = importlib.reload(kb)
    assert [b.callback_data for b in _buttons(mod.onboarding_keyboard())][0] == "onboard:android"


# ── Структурный тест: вложенный хелпер не зовёт сам себя ──


def test_no_nested_helper_recurses_into_itself():
    """Регрессия 2026-08-25: в cmd_config вложенный ``_say`` вместо
    ``message.answer`` звал ``_say`` → RecursionError → обещанная после триала
    ссылка не приходила. Ловим структурно (образец — test_leg_scheme.py):
    у любой вложенной функции в bot/handlers.py тело не должно содержать
    вызов её же имени. Рекурсивных вложенных хелперов в боте нет и не
    планируется — появится настоящий, снимай его здесь явным исключением."""
    src = os.path.join(REPO_ROOT, "bot", "handlers.py")
    with open(src, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    offenders = []
    for outer in ast.walk(tree):
        if not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in outer.body:
            if not isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(inner):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == inner.name
                ):
                    offenders.append(f"{outer.name}.{inner.name} (строка {call.lineno})")
    assert not offenders, "вложенный хелпер зовёт сам себя: " + ", ".join(offenders)
