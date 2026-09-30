"""Триал 3 дня: кабинет (webapp) — статические проверки исходников.

tsx из pytest не запустить, поэтому, как test_auditfix3_webapp_pages_Home_tsx,
проверяем исходник грепами. Сборку, tsc и eslint гоняет отдельный шаг.

Что держим:
- активация одним вызовом ``activateTrial()``: второй шаг «купить самый
  дешёвый тариф» из браузера удалён (сервер сам выдаёт подписку на N дней);
- 409 «уже забран» молча, 503/402 со своими текстами;
- баннер без «месяца», с честным вариантом бонус-онли;
- первый пресет пополнения не меньше цены продления Solo (150 ₽);
- справка про заморозку «после первой оплаты».
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOME_TSX = ROOT / "webapp" / "src" / "pages" / "Home.tsx"
PLANS_TSX = ROOT / "webapp" / "src" / "pages" / "Plans.tsx"
API_TS = ROOT / "webapp" / "src" / "api.ts"

# В backend-only тест-образе webapp/ не смонтирован — проверять нечего.
if not HOME_TSX.is_file():
    pytest.skip(
        "webapp/ недоступен (backend-only тест-образ) — проверяется в полном "
        "чекауте/CI",
        allow_module_level=True,
    )


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _handler_body(src: str) -> str:
    m = re.search(
        r"const handleActivateTrial = async \(\) => \{(.*?)\n  \};\n",
        src,
        re.DOTALL,
    )
    assert m, "не найдено тело handleActivateTrial"
    return m.group(1)


def test_trial_activation_is_single_server_call() -> None:
    src = _read(HOME_TSX)
    body = _handler_body(src)
    assert body.count("activateTrial()") == 1
    # Второй шаг из браузера удалён: он покупал Solo за 150 ₽ на бонус 15 ₽.
    assert "activateSubscription" not in body
    assert "fetchPlans" not in body
    assert "trial_autoactivate_allowed" not in src
    # Импорты тоже убраны (иначе eslint no-unused-vars).
    imports = src[: src.index('from "../api"')]
    assert "activateSubscription" not in imports
    assert "fetchPlans" not in imports
    # Экран «Готово» — только когда в ответе есть ссылка.
    assert "res.sub_token || res.sub_url" in body


def test_trial_activation_error_branches() -> None:
    body = _handler_body(_read(HOME_TSX))
    # Код берём из текста ошибки request() ("409: {...}"), поля status нет.
    assert "httpStatus(err)" in body
    assert "err as { status?: number }" not in body
    assert "TRIAL_TAKEN_409.test" in body
    assert "Сейчас много желающих, попробуй через пару минут." in body
    assert (
        "Не получилось включить бесплатные дни. Напиши в поддержку из раздела «Помощь»."
        in body
    )
    assert "Не получилось забрать подарок. Проверь связь и попробуй ещё раз." in body


def test_trial_taken_409_matches_backend_details() -> None:
    """Молчим ровно на detail-ах «уже забран» из api_webapp.py, и не молчим
    на 409 от сбоя сборки подписки (там str(RuntimeError))."""
    src = _read(HOME_TSX)
    m = re.search(r"const TRIAL_TAKEN_409 =\s*/(.*?)/;", src, re.DOTALL)
    assert m, "не найден TRIAL_TAKEN_409"
    rx = re.compile(m.group(1))
    for detail in (
        "Trial already activated",
        "Trial already used",
        "User already has a live subscription",
    ):
        assert rx.search(f'409: {{"detail":"{detail}"}}'), detail
    for detail in (
        "No healthy VPN nodes available for plan",
        "warm bundle is empty",
    ):
        assert not rx.search(f'409: {{"detail":"{detail}"}}'), detail


def test_trial_banner_texts() -> None:
    src = _read(HOME_TSX)
    assert "бесплатный месяц" not in src
    assert "Забери {trialTotalDays} {pluralDays(trialTotalDays)} бесплатно" in src
    assert "Тебя пригласил друг, поэтому дней не ${trialDays}, а ${trialTotalDays}. " in src
    assert "Карта не нужна. Один тап, и получишь ссылку с инструкцией," in src
    # Бонус-онли (живая подписка): честный баннер «Подарок: N ₽ на баланс».
    assert "me.balance.trial_bonus_only" in src
    assert "🎁 Подарок: {trialGiftRub} ₽ на баланс, это {trialTotalDays}" in src
    assert "Зачтётся при следующем продлении." in src
    assert '"Забрать подарок"' in src
    assert "Подписки пока нет. Забери бесплатные дни выше." in src
    # Экран «Готово» говорит, на сколько дней.
    assert "Бесплатно на {freeDays} {pluralDays(freeDays)}" in src


def test_referral_block_promises_first_payment() -> None:
    src = _read(HOME_TSX)
    assert "когда друг впервые оплатит" in src
    assert "когда друг пополнит счёт впервые" not in src
    assert "referral.invitee_total_days" in src


def test_new_ui_texts_have_no_long_dash() -> None:
    src = _read(HOME_TSX)
    for text in (
        "Сейчас много желающих",
        "Не получилось включить бесплатные дни",
        "Зачтётся при следующем продлении.",
        "Подписки пока нет. Забери бесплатные дни выше.",
        "Карта не нужна. Один тап, и получишь ссылку",
    ):
        line = next(ln for ln in src.splitlines() if text in ln)
        assert "—" not in line, line


def test_first_topup_preset_covers_renewal() -> None:
    src = _read(HOME_TSX)
    m = re.search(r"const TOPUP_PRESETS = \[([^\]]*)\]", src)
    assert m, "не найден TOPUP_PRESETS"
    presets = [int(x) for x in m.group(1).split(",")]
    # Первый пресет не меньше продления Solo (150 ₽), иначе после триала
    # пополнение «на 100 ₽» молча не хватит на автопродление.
    assert presets[0] >= 15000
    assert presets == sorted(presets)


def test_freeze_error_shows_backend_text() -> None:
    src = _read(HOME_TSX)
    assert "Не удалось заморозить: ${humanError(e)}" in src


def test_plans_help_freeze_after_first_payment() -> None:
    src = _read(PLANS_TSX)
    assert "После первой оплаты тариф можно <b>заморозить</b> на 7 дней" in src


def test_api_types_have_trial_fields() -> None:
    src = _read(API_TS)
    balance = src[src.index("export interface BalanceInfo"):]
    balance = balance[: balance.index("}")]
    for field in ("trial_days?", "trial_referral_days?", "trial_bonus_only?"):
        assert field in balance, field
    resp = src[src.index("export interface TrialActivateResponse"):]
    resp = resp[: resp.index("}")]
    for field in (
        "subscription_id?",
        "sub_token?",
        "sub_url?",
        "expires_at?",
        "trial_days?",
        "referral_days?",
    ):
        assert field in resp, field
    assert "trial_expires_at: string | null" in resp
    referral = src[src.index("export interface ReferralInfo"):]
    referral = referral[: referral.index("}")]
    assert "invitee_total_days?" in referral
