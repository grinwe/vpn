"""E1 из онбординг-роадмапа: бот как тонкий вход.

Роадмап: docs/operations/onboarding_roadmap_2026_07_25.md.
Контекст: 75% новых юзеров за 30 дней не делали НИЧЕГО после /start.

В backend-тест-образе нет aiogram, поэтому подменяем его модулем с настоящими
(пусть и примитивными) классами клавиатур — иначе структуру кнопок не проверить,
а именно она здесь и чинится.
"""
from __future__ import annotations

import importlib
import os
import sys
import types as _pytypes

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

if not os.path.isfile(os.path.join(_REPO_ROOT, "bot", "keyboards.py")):
    pytest.skip("bot/ недоступен в этом образе", allow_module_level=True)


class _Btn:
    def __init__(self, text=None, callback_data=None, web_app=None, **kw):
        self.text = text
        self.callback_data = callback_data
        self.web_app = web_app


class _Markup:
    def __init__(self, inline_keyboard=None, keyboard=None, **kw):
        self.inline_keyboard = inline_keyboard or []
        self.keyboard = keyboard or []


class _WebAppInfo:
    def __init__(self, url=None):
        self.url = url


@pytest.fixture
def kb(monkeypatch):
    """bot.keyboards поверх минимального aiogram-стаба."""
    types_mod = _pytypes.SimpleNamespace(
        InlineKeyboardButton=_Btn,
        InlineKeyboardMarkup=_Markup,
        KeyboardButton=_Btn,
        ReplyKeyboardMarkup=_Markup,
        WebAppInfo=_WebAppInfo,
        BotCommand=lambda **kw: _pytypes.SimpleNamespace(**kw),
    )
    monkeypatch.setitem(sys.modules, "aiogram", _pytypes.ModuleType("aiogram"))
    sys.modules["aiogram"].types = types_mod
    monkeypatch.setitem(sys.modules, "aiogram.types", types_mod)
    monkeypatch.setenv("WEBAPP_BASE_URL", "https://example.test/app")
    mod = importlib.reload(importlib.import_module("bot.keyboards"))
    yield mod
    sys.modules.pop("bot.keyboards", None)


def _texts(markup) -> list[str]:
    rows = markup.inline_keyboard or markup.keyboard
    return [b.text for row in rows for b in row]


# ── E1.1 / E1.5 — первый экран новичка ───────────────────────────────────


def test_new_user_sees_single_gift_button(kb):
    """Подарок обязан быть КНОПКОЙ, а не строкой в тексте: раньше единственное
    упоминание жило в тексте, и юзер должен был сам догадаться, что «личный
    кабинет» — это кнопка Mini App."""
    markup = kb.welcome_action_keyboard(trial_available=True, is_new=True)
    texts = _texts(markup)

    assert texts[0] == "🎁 Забрать бесплатный месяц"
    assert markup.inline_keyboard[0][0].web_app.url == "https://example.test/app"
    # Один экран — одно действие: тарифы допустимы, «поломочные» пункты — нет.
    assert "❓ Проблема с ЛК" not in texts
    assert len(texts) <= 2, f"первый экран новичка перегружен: {texts}"


def test_returning_user_keeps_full_menu(kb):
    """Тому, кто уже забрал подарок, навигацию не режем."""
    texts = _texts(kb.welcome_action_keyboard(trial_available=False, is_new=False))

    assert "🔐 Открыть личный кабинет" in texts
    assert "💎 Подписка" in texts
    assert "🤝 Пригласить друга" in texts


def test_bottom_keyboard_hides_broken_button_without_devices(kb):
    """E3.3: «VPN не работает» у юзера без устройств вела в тупик
    «Оформить — /buy», а такой команды в боте нет."""
    without = _texts(kb.start_keyboard(has_devices=False))
    with_dev = _texts(kb.start_keyboard(has_devices=True))

    assert kb.BTN_VPN_BROKEN not in without
    assert kb.BTN_VPN_BROKEN in with_dev
    # Остальная навигация на месте в обоих случаях.
    assert kb.BTN_MAIN_MENU in without and kb.BTN_HELP in without


def test_gift_button_absent_without_https_webapp(kb, monkeypatch):
    """Без валидного WebApp-URL кнопки подарка быть не может — иначе оффер
    остаётся обещанием без адреса."""
    monkeypatch.setenv("WEBAPP_BASE_URL", "")
    mod = importlib.reload(kb)
    texts = _texts(mod.welcome_action_keyboard(trial_available=True, is_new=True))
    assert "🎁 Забрать бесплатный месяц" not in texts
