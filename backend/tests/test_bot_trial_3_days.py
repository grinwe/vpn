"""Бот: тексты и ветки триала на 3 дня (6 по приглашению).

План: docs/operations/trial_3_days_plan_2026_09_30.md, раздел «Бот».

Что закрепляем:

* числа дней приходят из бэкенда (``/users/register``, ответ activate_full,
  ``/referral/code``) и попадают в приветствие, /plans, кнопку подарка и
  заголовок после активации; при недоступном бэкенде дефолт 3 + 0;
* заголовок после активации без даты (скрытые сутки не видны), на
  историческом пути (``trial_days`` = null) «Подписка активирована»;
* 409 «Trial already used» не проваливается в ветку провижининга с ложным
  «подарок зачислен», а отправляет на /renew и /plans;
* 409 «on trial» при покупке другого тарифа: свой текст с кабинетом и /help;
* /referral больше не обещает «+50 ₽ / +50 ₽»;
* в новых текстах нет длинных тире (стиль UI-текстов).
"""
from __future__ import annotations

import sys

import pytest

from tests._bot_stubs import (
    Btn,
    FakeBot,
    FakeCallback,
    FakeMessage,
    Markup,
    bot_sources_available,
    ensure_repo_root_on_path,
    install_bot_stubs,
    load_keyboards,
)

if not bot_sources_available():
    pytest.skip("bot/handlers.py недоступен в этом образе", allow_module_level=True)


_PLANS = [
    {"id": 1, "name": "Solo", "price": 150, "max_devices": 1,
     "duration_days": 30, "is_visible": True},
    {"id": 2, "name": "Family", "price": 300, "max_devices": 3,
     "duration_days": 30, "is_visible": True},
]


@pytest.fixture()
def handlers(monkeypatch):
    return install_bot_stubs(monkeypatch)


def _script_fetch(handlers, monkeypatch, routes: dict):
    """``_fetch_json`` → (status, payload) по хвосту URL; запросы пишутся."""
    calls: list[tuple[str, str, dict]] = []

    async def _fake(method, url, **kw):
        calls.append((method, url, kw))
        for key, resp in routes.items():
            if url.endswith(key):
                return resp
        raise AssertionError(f"неожиданный запрос {method} {url}")

    monkeypatch.setattr(handlers, "_fetch_json", _fake)
    return calls


def _real_buttons(handlers, monkeypatch):
    """Всеядный стаб aiogram.types не даёт заглянуть в клавиатуру."""
    monkeypatch.setattr(handlers.types, "InlineKeyboardButton", Btn)
    monkeypatch.setattr(handlers.types, "InlineKeyboardMarkup", Markup)


def _capture_welcome_kb(handlers, monkeypatch) -> list[dict]:
    seen: list[dict] = []

    def _kb(**kw):
        seen.append(kw)
        return "welcome-kb"

    monkeypatch.setattr(handlers, "welcome_action_keyboard", _kb)
    return seen


def _all_texts(bot: FakeBot, msg: FakeMessage | None = None) -> list[str]:
    texts = [t for _c, t, _k in bot.sent]
    if msg is not None:
        texts = [t for t, _k in msg.sent] + texts
    return texts


# ── склонение ──


def test_plural_days():
    ensure_repo_root_on_path()
    from bot.plural import plural_days

    assert [plural_days(n) for n in (1, 3, 5, 6, 10, 11, 14, 21, 22, 25)] == [
        "1 день", "3 дня", "5 дней", "6 дней", "10 дней", "11 дней", "14 дней",
        "21 день", "22 дня", "25 дней",
    ]


# ── приветствие ──


def test_welcome_line_three_days(handlers):
    text = handlers.format_welcome("Аня", is_new=True, trial_available=True)

    assert "🎁 3 дня VPN бесплатно, карта не нужна.\n" in text
    assert text.endswith(
        "Один тап по кнопке ниже: получишь ссылку и инструкцию, как подключиться."
    )
    assert "месяц" not in text.split("🎁")[1]


def test_welcome_line_invited_six_days(handlers):
    text = handlers.format_welcome(
        "Аня", is_new=True, trial_available=True, trial_days=3, ref_days=3
    )

    assert (
        "🎁 Тебя пригласил друг, поэтому у тебя 6 дней VPN бесплатно вместо 3. "
        "Карта не нужна.\n"
        "Один тап по кнопке ниже: получишь ссылку и инструкцию, как подключиться."
    ) in text
    assert "—" not in text.split("🎁")[1]


def test_welcome_without_trial_has_no_gift_line(handlers):
    text = handlers.format_welcome("Аня", is_new=False, trial_available=False)
    assert "🎁" not in text and "бесплатно" not in text


@pytest.mark.asyncio
async def test_cmd_start_takes_days_from_register(handlers, monkeypatch):
    """Реферал привязывается в том же /register, и дни считаются после неё:
    новичок по ссылке друга сразу видит 6 дней и в тексте, и на кнопке."""
    calls = _script_fetch(handlers, monkeypatch, {
        "/api/users/register": (200, {
            "created": True, "trial_available": True, "has_devices": False,
            "has_subscription": False, "trial_days": 3, "trial_referral_days": 3,
        }),
    })
    seen_kb = _capture_welcome_kb(handlers, monkeypatch)
    msg = FakeMessage(FakeBot())
    msg.text = "/start ref_abc"

    await handlers.cmd_start(msg, state=None)

    assert calls[0][2]["json"]["referral_code"] == "abc"
    assert "6 дней VPN бесплатно вместо 3" in msg.sent[0][0]
    assert seen_kb == [{
        "trial_available": True, "is_new": True, "has_link": False, "trial_days": 6,
    }]


@pytest.mark.asyncio
async def test_cmd_start_backend_down_defaults_to_three_days(handlers, monkeypatch):
    async def _down(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setattr(handlers, "_fetch_json", _down)
    seen_kb = _capture_welcome_kb(handlers, monkeypatch)
    msg = FakeMessage(FakeBot())
    msg.text = "/start"

    await handlers.cmd_start(msg, state=None)

    assert "🎁 3 дня VPN бесплатно" in msg.sent[0][0]
    assert seen_kb[0]["trial_days"] == 3


@pytest.mark.asyncio
async def test_go_start_passes_days_to_welcome(handlers, monkeypatch):
    async def _flags(_uid):
        return handlers.UserFlags(True, False, False, 3, 3)

    monkeypatch.setattr(handlers, "_fetch_user_flags", _flags)
    seen_kb = _capture_welcome_kb(handlers, monkeypatch)
    bot = FakeBot()
    msg = FakeMessage(bot)

    await handlers.go_start(FakeCallback(bot, msg))

    assert "6 дней VPN бесплатно вместо 3" in msg.sent[0][0]
    assert seen_kb[0]["trial_days"] == 6


# ── /plans ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ref_days", "offer", "button"),
    [
        (0, "🎁 <b>Сначала 3 дня бесплатно.</b>", "🎁 Забрать 3 дня бесплатно"),
        (
            3,
            "🎁 <b>Сначала 6 дней бесплатно: тебя пригласил друг.</b>",
            "🎁 Забрать 6 дней бесплатно",
        ),
    ],
    ids=["3", "3+3"],
)
async def test_list_plans_offer_and_button(handlers, monkeypatch, ref_days, offer, button):
    _script_fetch(handlers, monkeypatch, {"/api/plans": (200, _PLANS)})

    async def _flags(_uid):
        return handlers.UserFlags(True, False, False, 3, ref_days)

    monkeypatch.setattr(handlers, "_fetch_user_flags", _flags)
    _real_buttons(handlers, monkeypatch)
    msg = FakeMessage(FakeBot())

    await handlers.list_plans(msg)

    text, kw = msg.sent[0]
    lines = text.split("\n")
    assert lines[:5] == [
        offer,
        "Один тап по кнопке ниже, карта не нужна.",
        "",
        "Ниже тарифы, если захочешь больше устройств или сразу на год.",
        "",
    ]
    assert "месяц" not in text.lower().split("<b>месяц:</b>")[0]
    first = kw["reply_markup"].inline_keyboard[0][0]
    assert (first.text, first.callback_data) == (button, "trial:activate")


# ── активация в боте ──


def _activate(handlers, monkeypatch, resp, *, subs=None):
    routes = {"/api/trial/activate_full": resp, "/api/plans": (200, _PLANS)}
    if subs is not None:
        routes["/api/users/by_telegram/100"] = (200, subs)
    return _script_fetch(handlers, monkeypatch, routes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "intro"),
    [
        ({"trial_days": 3, "referral_days": 0}, "🎉 Готово! VPN бесплатно на 3 дня."),
        (
            {"trial_days": 3, "referral_days": 3},
            "🎉 Готово! VPN бесплатно на 6 дней: 3 дня дарим всем и ещё 3, потому "
            "что тебя пригласил друг.",
        ),
        # Исторический путь: старый бонус 150 ₽ потрачен на обычный тариф.
        ({"trial_days": None, "referral_days": None}, "🎉 Готово! Подписка активирована."),
        # Старый бэкенд без полей: тоже без ложного числа дней.
        ({}, "🎉 Готово! Подписка активирована."),
    ],
    ids=["3", "3+3", "historical", "no-fields"],
)
async def test_trial_activate_intro_has_days_and_no_date(handlers, monkeypatch, payload, intro):
    body = {"subscription_id": 7, "plan_name": "Solo",
            "expires_at": "2026-10-04T12:00:00", **payload}
    _activate(handlers, monkeypatch, (200, body), subs=[])
    bot = FakeBot()
    msg = FakeMessage(bot)

    await handlers.trial_activate_cb(FakeCallback(bot, msg))

    texts = _all_texts(bot, msg)
    # Ссылки нет (тихий режим) → заголовок ушёл сам, затем подсказка про ЛК.
    assert texts[0] == intro
    assert "04.10" not in texts[0] and "—" not in texts[0]


@pytest.mark.asyncio
async def test_trial_activate_409_already_used(handlers, monkeypatch):
    """Старая кнопка в чате после истечения триала: не «подарок зачислен» и не
    попытка выдать ссылку, а путь к продлению и тарифам."""
    calls = _activate(
        handlers, monkeypatch, (409, {"detail": "Trial already used"}), subs=[]
    )
    bot = FakeBot()
    msg = FakeMessage(bot)

    await handlers.trial_activate_cb(FakeCallback(bot, msg))

    assert _all_texts(bot, msg) == [
        "Бесплатные дни уже использованы. Продлить прежнюю подписку: /renew, "
        "все тарифы: /plans."
    ]
    urls = [u for _m, u, _k in calls]
    assert not any(u.endswith("/api/users/by_telegram/100") for u in urls)
    assert not any(u.endswith("/api/plans") for u in urls)


@pytest.mark.asyncio
async def test_trial_activate_409_provisioning_failure_offers_retry(handlers, monkeypatch):
    _activate(handlers, monkeypatch, (409, {"detail": "No available VPN nodes"}))
    bot = FakeBot()
    msg = FakeMessage(bot)

    await handlers.trial_activate_cb(FakeCallback(bot, msg))

    texts = _all_texts(bot, msg)
    assert texts == [
        "Не получилось собрать подписку 😔 Подарок остаётся за тобой: попробуй "
        "ещё раз через пару минут или напиши /help."
    ]
    assert "зачислен" not in texts[0]


@pytest.mark.asyncio
async def test_trial_activate_402_text(handlers, monkeypatch):
    _activate(handlers, monkeypatch, (402, {"detail": "Insufficient balance"}))
    bot = FakeBot()

    await handlers.trial_activate_cb(FakeCallback(bot, FakeMessage(bot)))

    assert _all_texts(bot) == [
        "Бонус уже потрачен, включить бесплатные дни с него не получилось. "
        "Выбери тариф: /plans, или загляни в баланс: /balance."
    ]


# ── покупка другого тарифа во время триала ──


async def _buy_plan(handlers, monkeypatch, resp) -> list[str]:
    _script_fetch(handlers, monkeypatch, {"/api/invoices": resp})
    bot = FakeBot()
    msg = FakeMessage(bot)
    cb = FakeCallback(bot, msg)
    cb.data = "plan:2"

    await handlers.create_invoice(cb)

    assert cb.answered == 1
    return _all_texts(bot, msg)


@pytest.mark.asyncio
async def test_create_invoice_409_on_trial(handlers, monkeypatch):
    texts = await _buy_plan(handlers, monkeypatch, (409, {"detail": "on trial"}))

    assert texts == [
        "Сейчас у тебя бесплатные дни на Solo. Другой тариф можно взять в личном "
        "кабинете, неиспользованные дни зачтутся, или напиши /help, поменяем вручную."
    ]


@pytest.mark.asyncio
async def test_create_invoice_409_other_plan_keeps_renew_text(handlers, monkeypatch):
    texts = await _buy_plan(handlers, monkeypatch, (
        409, {"detail": "user already has an active subscription on another plan"},
    ))

    assert len(texts) == 1 and "Продлить её: /renew" in texts[0]
    assert "бесплатные дни" not in texts[0]


# ── /referral ──


async def _referral_text(handlers, monkeypatch, payload: dict) -> str:
    _script_fetch(handlers, monkeypatch, {"/api/referral/code": (200, payload)})

    async def _username(_bot):
        return "test_bot"

    monkeypatch.setattr(handlers, "_get_bot_username", _username)
    bot = FakeBot()
    msg = FakeMessage(bot)

    await handlers._send_referral(msg, bot, 100)

    assert len(msg.sent) == 1
    return msg.sent[0][0]


@pytest.mark.asyncio
async def test_referral_text_uses_backend_numbers(handlers, monkeypatch):
    text = await _referral_text(handlers, monkeypatch, {
        "code": "abc", "uses": 2, "bonus_days": 3, "reward_days": 10,
        "invitee_total_days": 6,
    })

    assert "https://t.me/test_bot?start=ref_abc" in text
    assert text.endswith(
        "Друг получит 6 дней VPN бесплатно вместо 3, а тебе начислим 10 дней "
        "подписки, когда он впервые оплатит.\n"
        "Приглашено: 2 чел."
    )
    assert "50 ₽" not in text and "—" not in text


@pytest.mark.asyncio
async def test_referral_text_without_numbers_does_not_lie(handlers, monkeypatch):
    """Старый бэкенд без invitee_total_days: без чисел, но и без «+50 ₽»."""
    text = await _referral_text(handlers, monkeypatch, {"code": "abc", "uses": 0})

    assert (
        "Друг получит бесплатные дни VPN, а тебе начислим дни подписки, когда он "
        "впервые оплатит."
    ) in text
    assert "₽" not in text


# ── клавиатура ──


def test_welcome_keyboard_gift_button_days(monkeypatch):
    kb = load_keyboards(monkeypatch)
    try:
        default = kb.welcome_action_keyboard(trial_available=True, is_new=True)
        invited = kb.welcome_action_keyboard(trial_available=True, is_new=True, trial_days=6)
        assert default.inline_keyboard[0][0].text == "🎁 Забрать 3 дня бесплатно"
        assert invited.inline_keyboard[0][0].text == "🎁 Забрать 6 дней бесплатно"
        assert invited.inline_keyboard[0][0].callback_data == "trial:activate"
    finally:
        sys.modules.pop("bot.keyboards", None)


# ── ревью 30.09: таймаут /register и живая подписка ──


@pytest.mark.asyncio
async def test_cmd_start_register_timeout_defaults_to_newbie_offer(handlers, monkeypatch):
    """_fetch_json на таймауте отдаёт (0, {"message": ...}) — truthy dict без
    полей. Раньше бот читал его как «подарок недоступен»."""
    async def _timeout(*a, **k):
        return 0, {"message": "backend unreachable"}

    monkeypatch.setattr(handlers, "_fetch_json", _timeout)
    seen_kb = _capture_welcome_kb(handlers, monkeypatch)
    msg = FakeMessage(FakeBot())
    msg.text = "/start"

    await handlers.cmd_start(msg, state=None)

    assert "🎁 3 дня VPN бесплатно" in msg.sent[0][0]
    assert seen_kb[0]["trial_available"] is True


@pytest.mark.asyncio
async def test_cmd_start_live_subscription_hides_days_offer(handlers, monkeypatch):
    """Платящий с живой подпиской и незабранным триалом: тап «забрать дни»
    дал бы 409 live — предложение не показываем (подарок 15 ₽ есть в кабинете)."""
    async def _reg(*a, **k):
        return 200, {"created": False, "trial_available": True, "has_devices": True,
                     "has_subscription": True, "trial_days": 3, "referral_days": 0}

    monkeypatch.setattr(handlers, "_fetch_json", _reg)
    seen_kb = _capture_welcome_kb(handlers, monkeypatch)
    msg = FakeMessage(FakeBot())
    msg.text = "/start"

    await handlers.cmd_start(msg, state=None)

    assert "бесплатно" not in msg.sent[0][0]
    assert seen_kb[0]["trial_available"] is False


@pytest.mark.asyncio
async def test_user_flags_live_subscription_and_timeout(handlers, monkeypatch):
    async def _reg(*a, **k):
        return 200, {"trial_available": True, "has_devices": False,
                     "has_subscription": True, "trial_days": 3, "referral_days": 0}

    monkeypatch.setattr(handlers, "_fetch_json", _reg)
    flags = await handlers._fetch_user_flags(100)
    assert flags[0] is False and flags[2] is True

    async def _timeout(*a, **k):
        return 0, {"message": "backend unreachable"}

    monkeypatch.setattr(handlers, "_fetch_json", _timeout)
    flags = await handlers._fetch_user_flags(100)
    assert flags[0] is True  # fail-safe в сторону новичка
