"""Раскладка саб-ссылок по двум фронтам.

Главное, что здесь проверяется — что бэкенд и бот считают домен ОДИНАКОВО.
Реализации намеренно продублированы (разные контейнеры, общего пакета нет),
и разъехаться им нельзя: человек увидит в боте одну ссылку, а кнопка
«что-то не работает» внутри клиента поведёт на другой домен.
"""

from __future__ import annotations

import hashlib
import secrets

import pytest

from app.services import sub_links

PRIMARY = "https://grn-ssync.pro"
ALT = "https://grwr.ink"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("SUB_LINK_BASE_URL", "SUB_LINK_BASE_URL_ALT", "SUB_LINK_ALT_SHARE"):
        monkeypatch.delenv(name, raising=False)


def _bot_base(token: str, primary: str, alt: str, share_raw: str) -> str:
    """Копия bot/handlers._sub_base_for — держать синхронной с оригиналом."""
    primary = (primary or "").strip().rstrip("/")
    alt = (alt or "").strip().rstrip("/")
    try:
        share = max(0, min(100, int((share_raw or "0").strip())))
    except ValueError:
        share = 0
    if not alt or share <= 0:
        return primary
    if share >= 100:
        return alt
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return alt if int(digest[:8], 16) % 100 < share else primary


def test_disabled_by_default_keeps_primary(monkeypatch):
    """Без SUB_LINK_ALT_SHARE поведение обязано быть как до фичи."""
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    monkeypatch.setenv("SUB_LINK_BASE_URL_ALT", ALT)
    for _ in range(50):
        assert sub_links.sub_base_for(secrets.token_urlsafe(32)) == PRIMARY


def test_alt_ignored_when_not_configured(monkeypatch):
    """Доля есть, а запасного домена нет — остаёмся на основном, не падаем."""
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    monkeypatch.setenv("SUB_LINK_ALT_SHARE", "50")
    assert sub_links.sub_base_for("sometoken") == PRIMARY


@pytest.mark.parametrize("share,expected", [("100", ALT), ("0", PRIMARY)])
def test_extremes(monkeypatch, share, expected):
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    monkeypatch.setenv("SUB_LINK_BASE_URL_ALT", ALT)
    monkeypatch.setenv("SUB_LINK_ALT_SHARE", share)
    assert sub_links.sub_base_for(secrets.token_urlsafe(32)) == expected


def test_garbage_share_is_not_fatal(monkeypatch):
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    monkeypatch.setenv("SUB_LINK_BASE_URL_ALT", ALT)
    monkeypatch.setenv("SUB_LINK_ALT_SHARE", "пятьдесят")
    assert sub_links.sub_base_for("sometoken") == PRIMARY


def test_stable_for_same_token(monkeypatch):
    """Один токен — всегда один домен: иначе ссылка «прыгает» между показами."""
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    monkeypatch.setenv("SUB_LINK_BASE_URL_ALT", ALT)
    monkeypatch.setenv("SUB_LINK_ALT_SHARE", "50")
    token = secrets.token_urlsafe(32)
    first = sub_links.sub_base_for(token)
    assert all(sub_links.sub_base_for(token) == first for _ in range(20))


def test_split_is_roughly_the_requested_share(monkeypatch):
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    monkeypatch.setenv("SUB_LINK_BASE_URL_ALT", ALT)
    monkeypatch.setenv("SUB_LINK_ALT_SHARE", "50")
    tokens = [secrets.token_urlsafe(32) for _ in range(2000)]
    on_alt = sum(sub_links.sub_base_for(t) == ALT for t in tokens)
    # Допуск широкий намеренно: тест ловит перекос вроде «всё на одном
    # домене», а не проверяет качество sha256.
    assert 800 < on_alt < 1200, f"перекос раскладки: {on_alt}/2000 на запасном"


def test_backend_and_bot_agree(monkeypatch):
    """Ради этого тест и написан: две реализации обязаны совпасть на всех долях."""
    monkeypatch.setenv("SUB_LINK_BASE_URL", PRIMARY)
    monkeypatch.setenv("SUB_LINK_BASE_URL_ALT", ALT)
    tokens = [secrets.token_urlsafe(32) for _ in range(300)]
    for share in ("0", "1", "25", "50", "99", "100", "мусор"):
        monkeypatch.setenv("SUB_LINK_ALT_SHARE", share)
        for token in tokens:
            assert sub_links.sub_base_for(token) == _bot_base(token, PRIMARY, ALT, share), (
                f"бэкенд и бот разошлись на share={share}, token={token[:12]}…"
            )


def test_sub_url_falls_back_to_relative_path(monkeypatch):
    """Без основного домена URL остаётся относительным, как и раньше."""
    assert sub_links.sub_url_for("tok123") == "/api/sub/tok123"
