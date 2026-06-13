"""Phase B — HAPP sub-response headers (gated auto-select + optional fallback)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import api_extensions


class _Sub:
    """Заглушка-sub: хелдер читает только .expires_at и .user_id."""

    def __init__(self, expires_at, user_id=1):
        self.expires_at = expires_at
        self.user_id = user_id


def _h(sub, token="tok"):
    return api_extensions._sub_response_headers(sub, token)


def test_autoconnect_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUB_HAPP_AUTOCONNECT", raising=False)
    monkeypatch.delenv("SUB_LINK_FALLBACK_BASE_URL", raising=False)
    h = _h(_Sub(datetime(2030, 1, 1, tzinfo=timezone.utc)))
    # ГЕЙТ выключен → autoconnect НЕ эмитим (прод-поведение не меняется для всех)
    assert "subscription-autoconnect" not in h
    assert h["profile-title"] == "V8-VPN"
    assert "subscription-userinfo" in h
    assert "fallback-url" not in h


def test_autoconnect_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUB_HAPP_AUTOCONNECT", "all")
    h = _h(_Sub(None))
    assert h["subscription-autoconnect"] == "1"
    assert h["subscription-autoconnect-type"] == "lowestdelay"


def test_autoconnect_per_user_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    # обкатка на одном (нескольких) юзере по user_id — как diverse-backfill
    monkeypatch.setenv("SUB_HAPP_AUTOCONNECT", "376, 12")
    assert "subscription-autoconnect" in _h(_Sub(None, user_id=376))
    assert "subscription-autoconnect" not in _h(_Sub(None, user_id=999))


def test_no_expiry_means_no_userinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    # раньше int(None.timestamp()) уронил бы оба саб-пути — теперь гард
    monkeypatch.delenv("SUB_HAPP_AUTOCONNECT", raising=False)
    h = _h(_Sub(None))
    assert "subscription-userinfo" not in h


def test_fallback_url_emitted_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUB_LINK_FALLBACK_BASE_URL", "https://backup.example/")
    h = _h(_Sub(None), token="tok123")
    assert h["fallback-url"] == "https://backup.example/tok123"
