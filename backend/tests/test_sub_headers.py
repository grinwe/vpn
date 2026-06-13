"""Phase B — HAPP sub-response headers (gated auto-select + new-device + fallback)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import api_extensions

_SINCE = "2026-06-13T19:09:00Z"
_NEW = datetime(2026, 6, 13, 20, 0, tzinfo=timezone.utc)   # после метки
_OLD = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)     # до метки


class _Sub:
    def __init__(self, expires_at, user_id=1):
        self.expires_at = expires_at
        self.user_id = user_id


class _Dev:
    def __init__(self, created_at):
        self.created_at = created_at


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for k in ("SUB_HAPP_AUTOCONNECT", "SUB_HAPP_AUTOCONNECT_SINCE",
              "SUB_LINK_FALLBACK_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def _h(sub, token="tok", device=None):
    return api_extensions._sub_response_headers(sub, token, device)


def test_off_by_default() -> None:
    h = _h(_Sub(datetime(2030, 1, 1, tzinfo=timezone.utc)))
    assert "subscription-autoconnect" not in h   # гейт выкл — прод не меняется
    assert h["profile-title"] == "V8-VPN"
    assert "subscription-userinfo" in h
    assert "fallback-url" not in h


def test_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUB_HAPP_AUTOCONNECT", "all")
    h = _h(_Sub(None))
    assert h["subscription-autoconnect"] == "1"
    assert h["subscription-autoconnect-type"] == "lowestdelay"


def test_per_user_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUB_HAPP_AUTOCONNECT", "1, 12")
    assert "subscription-autoconnect" in _h(_Sub(None, user_id=1))
    assert "subscription-autoconnect" not in _h(_Sub(None, user_id=99))


def test_since_gates_to_new_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUB_HAPP_AUTOCONNECT", "1")
    monkeypatch.setenv("SUB_HAPP_AUTOCONNECT_SINCE", _SINCE)
    sub = _Sub(None, user_id=1)
    # новый девайс (created_at после метки) → вкл
    assert "subscription-autoconnect" in _h(sub, device=_Dev(_NEW))
    # старый/primary девайс → выкл (не путаемся)
    assert "subscription-autoconnect" not in _h(sub, device=_Dev(_OLD))
    # legacy саб-токен (девайса нет) при заданном SINCE → выкл
    assert "subscription-autoconnect" not in _h(sub, device=None)


def test_no_expiry_means_no_userinfo() -> None:
    h = _h(_Sub(None))
    assert "subscription-userinfo" not in h


def test_fallback_url_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUB_LINK_FALLBACK_BASE_URL", "https://backup.example/")
    h = _h(_Sub(None), token="tok123")
    assert h["fallback-url"] == "https://backup.example/tok123"
