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
    # Пин: в тест-контейнере APP_VERSION приходит из compose-env, и без
    # пина URL кнопок «плавал» бы между окружениями (кэшбастер ?v=).
    monkeypatch.setenv("APP_VERSION", "9.9.9")
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

    # Число дней на кнопке: 3 по умолчанию, 3 + 3 по приглашению.
    assert texts[0] == "🎁 Забрать 3 дня бесплатно"
    invited = _texts(kb.welcome_action_keyboard(trial_available=True, is_new=True, trial_days=6))
    assert invited[0] == "🎁 Забрать 6 дней бесплатно"
    # Активация нативная (trial:activate), а не web_app: ЛК у части юзеров не
    # открывается вовсе (аудит 2026-08-21, паритет A).
    assert markup.inline_keyboard[0][0].callback_data == "trial:activate"
    assert markup.inline_keyboard[0][0].web_app is None
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


def test_gift_button_stays_without_https_webapp(kb, monkeypatch):
    """Подарок не зависит от ЛК: активация нативная, поэтому без валидного
    WebApp-URL кнопка подарка ОСТАЁТСЯ (раньше она была web_app и без https
    пропадала — оффер без адреса). Исчезает только кнопка самого ЛК."""
    monkeypatch.setenv("WEBAPP_BASE_URL", "")
    mod = importlib.reload(kb)
    texts = _texts(mod.welcome_action_keyboard(trial_available=True, is_new=True))
    assert "🎁 Забрать 3 дня бесплатно" in texts
    assert "🔐 Открыть личный кабинет" not in texts
    returning = _texts(mod.welcome_action_keyboard(trial_available=False, is_new=False))
    assert "🔐 Открыть личный кабинет" not in returning


# ── Троттл переноса: не предлагать выбор, которым нельзя воспользоваться ──


def test_devices_by_telegram_reports_active_throttle(client, db_session):
    """Репорт юзера 2026-07-26: бот спрашивал «какое устройство не работает?»,
    юзер выбирал — и получал «уже перекидывали недавно». Бэкенд обязан отдать
    остаток троттла, чтобы бот не показывал бесполезный выбор."""
    from datetime import timedelta

    from app import models
    from app.time_utils import utcnow

    from .factories import (
        make_config,
        make_device,
        make_node,
        make_plan,
        make_subscription,
        make_user,
    )

    user = make_user(db_session, telegram_id="tg-throttle-1")
    node = make_node(db_session, name="node-throttle")
    cfg = make_config(db_session, node, name="cfg-throttle")
    plan = make_plan(db_session, name="plan-throttle")
    sub = make_subscription(db_session, user, plan, node)
    make_device(db_session, sub, cfg, access_username="user-throttle-1")

    # Свежий перенос — троттл активен.
    db_session.add(models.OperatorNodeReport(
        user_id=user.id, subscription_id=sub.id, reported_at=utcnow(),
    ))
    db_session.commit()

    res = client.get(
        f"/api/admin/client-control/devices-by-telegram?telegram_id={user.telegram_id}"
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["devices"], "устройства должны отдаваться как и раньше"
    assert data["retry_after_sec"] and data["retry_after_sec"] > 0, (
        "активный троттл не отражён — бот покажет пикер, который ничего не сделает"
    )

    # Старый перенос — троттл истёк, перенос снова доступен.
    old = db_session.query(models.OperatorNodeReport).one()
    old.reported_at = utcnow() - timedelta(minutes=30)
    db_session.commit()

    again = client.get(
        f"/api/admin/client-control/devices-by-telegram?telegram_id={user.telegram_id}"
    )
    assert again.json()["retry_after_sec"] is None


def test_about_keyboard_links_legal_pages_only_with_base_url(monkeypatch):
    """«О сервисе»: три URL-кнопки на документы + «Назад»; без LEGAL_BASE_URL —
    только «Назад» (битых ссылок не шлём)."""
    import sys

    from tests._bot_stubs import load_keyboards

    kb_mod = load_keyboards(monkeypatch)
    try:
        kb = kb_mod.about_keyboard("https://example.test/legal/")
        rows = kb.inline_keyboard
        urls = [getattr(b, "url", None) for row in rows for b in row]
        assert [u for u in urls if u] == [
            "https://example.test/legal/terms.html",
            "https://example.test/legal/refund.html",
            "https://example.test/legal/privacy.html",
        ]
        assert rows[-1][0].callback_data == "help:back"
        only_back = kb_mod.about_keyboard("").inline_keyboard
        assert [b.callback_data for row in only_back for b in row] == ["help:back"]
    finally:
        sys.modules.pop("bot.keyboards", None)
