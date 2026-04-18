"""Regression tests for Telegram initData validation in api_webapp.

Locks in the fixes for audit finding #53:
 - ``auth_date`` missing / empty / ``0`` → 401 (the old ``if auth_date
   and …`` check silently skipped TTL when the field was absent or
   zero).
 - ``auth_date`` older than ``WEBAPP_INIT_DATA_TTL_SECONDS`` → 401
   (was 24h hardcoded, now default 300s).
 - ``auth_date`` in the future beyond ~60s clock-skew → 401.
 - ``auth_date`` not parseable as an integer → 401 (was raising an
   uncaught ``ValueError`` → 500).
 - Valid, fresh initData with a real HMAC → parses cleanly.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from app.api_webapp import _verify_init_data

BOT_TOKEN = "test-bot-token-for-initdata-validation"
TTL = 300


def _sign(fields: dict[str, str], bot_token: str = BOT_TOKEN) -> str:
    """Build a validly signed Telegram initData from the given fields.

    Runs the same HMAC-SHA256 dance as Telegram and ``_verify_init_data``,
    so the returned query-string will pass the signature check. The
    auth_date / TTL assertions on top of that valid signature are what
    these tests actually exercise.
    """
    data_check_string = "\n".join(
        f"{k}={fields[k]}" for k in sorted(fields.keys())
    )
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    out = dict(fields)
    out["hash"] = h
    return urlencode(out)


def _base_fields(**overrides: str) -> dict[str, str]:
    """Return a valid field set with a fresh auth_date, plus overrides."""
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": 12345, "first_name": "Test"}),
    }
    fields.update(overrides)
    return fields


# ── Positive ──


def test_valid_fresh_init_data_parses_cleanly() -> None:
    init_data = _sign(_base_fields())
    parsed = _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert "user" in parsed
    assert "auth_date" in parsed


# ── auth_date rejection ──


def test_auth_date_zero_is_rejected() -> None:
    init_data = _sign(_base_fields(auth_date="0"))
    with pytest.raises(HTTPException) as exc_info:
        _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert exc_info.value.status_code == 401


def test_auth_date_missing_is_rejected() -> None:
    fields = _base_fields()
    del fields["auth_date"]
    init_data = _sign(fields)
    with pytest.raises(HTTPException) as exc_info:
        _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert exc_info.value.status_code == 401


def test_auth_date_empty_string_is_rejected() -> None:
    init_data = _sign(_base_fields(auth_date=""))
    with pytest.raises(HTTPException) as exc_info:
        _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert exc_info.value.status_code == 401


def test_expired_auth_date_is_rejected() -> None:
    old = str(int(time.time()) - TTL - 10)
    init_data = _sign(_base_fields(auth_date=old))
    with pytest.raises(HTTPException) as exc_info:
        _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert exc_info.value.status_code == 401


def test_future_auth_date_is_rejected() -> None:
    future = str(int(time.time()) + 120)
    init_data = _sign(_base_fields(auth_date=future))
    with pytest.raises(HTTPException) as exc_info:
        _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert exc_info.value.status_code == 401


def test_30s_clock_skew_passes() -> None:
    skewed = str(int(time.time()) + 30)
    init_data = _sign(_base_fields(auth_date=skewed))
    parsed = _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert parsed["auth_date"] == skewed


def test_non_integer_auth_date_is_rejected() -> None:
    init_data = _sign(_base_fields(auth_date="not-a-number"))
    with pytest.raises(HTTPException) as exc_info:
        _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert exc_info.value.status_code == 401


def test_negative_auth_date_is_rejected() -> None:
    init_data = _sign(_base_fields(auth_date="-1"))
    with pytest.raises(HTTPException) as exc_info:
        _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert exc_info.value.status_code == 401


# ── HMAC ──


def test_bad_hmac_is_rejected() -> None:
    init_data = _sign(_base_fields(), bot_token="wrong-token")
    with pytest.raises(HTTPException) as exc_info:
        _verify_init_data(init_data, BOT_TOKEN, TTL)
    assert exc_info.value.status_code == 401
