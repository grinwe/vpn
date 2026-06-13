"""Phase B — HAPP sub-response headers (auto-select + optional fallback)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import api_extensions


class _Sub:
    """Минимальный заглушка-sub: хелдер читает только .expires_at."""

    def __init__(self, expires_at):
        self.expires_at = expires_at


def test_autoconnect_headers_always_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUB_LINK_FALLBACK_BASE_URL", raising=False)
    h = api_extensions._sub_response_headers(
        _Sub(datetime(2030, 1, 1, tzinfo=timezone.utc)), "tok"
    )
    # HAPP «авто»: best-ping на (пере)коннекте
    assert h["subscription-autoconnect"] == "1"
    assert h["subscription-autoconnect-type"] == "lowestdelay"
    assert h["profile-title"] == "V8-VPN"
    assert "subscription-userinfo" in h          # expires задан
    assert "fallback-url" not in h               # env не задан → не эмитим


def test_no_expiry_means_no_userinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    # раньше int(None.timestamp()) уронил бы оба саб-пути — теперь гард
    monkeypatch.delenv("SUB_LINK_FALLBACK_BASE_URL", raising=False)
    h = api_extensions._sub_response_headers(_Sub(None), "tok")
    assert "subscription-userinfo" not in h
    assert h["subscription-autoconnect"] == "1"


def test_fallback_url_emitted_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUB_LINK_FALLBACK_BASE_URL", "https://backup.example/")
    h = api_extensions._sub_response_headers(_Sub(None), "tok123")
    # запасной саб-домен + токен (хвостовой слеш базы срезан)
    assert h["fallback-url"] == "https://backup.example/tok123"
