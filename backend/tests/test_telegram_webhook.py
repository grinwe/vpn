"""Tests for the Telegram native webhook endpoint (audit #62).

Verifies:
 - Valid secret_token header -> 200
 - Missing/wrong secret_token -> 401
 - Unconfigured endpoint -> 503
 - pre_checkout_query (XTR) is answered OK via Bot API
 - pre_checkout_query (non-XTR) is rejected
 - successful_payment round-trip -> calls _mark_invoice_paid_core
 - Non-payment updates are forwarded to bot internal endpoint
 - Malformed JSON -> 400

No database or real HTTP calls — all external I/O is monkeypatched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

SECRET = "test-webhook-secret-token-62"
BOT_TOKEN = "123456:ABC-test"
BOT_INTERNAL_URL = "http://bot:8081/webhook"


@pytest.fixture()
def _settings():
    """Patch Settings so the webhook endpoint is enabled."""
    settings = MagicMock()
    settings.telegram_webhook_secret_token = SECRET
    settings.telegram_webhook_url = "https://example.com/tg-webhook"
    settings.bot_internal_webhook_url = BOT_INTERNAL_URL
    settings.bot_token = BOT_TOKEN
    with patch("app.telegram_webhook.get_settings", return_value=settings):
        yield settings


@pytest.fixture()
def _settings_disabled():
    """Patch Settings so the webhook is NOT configured."""
    settings = MagicMock()
    settings.telegram_webhook_secret_token = None
    settings.telegram_webhook_url = None
    settings.bot_internal_webhook_url = None
    settings.bot_token = None
    with patch("app.telegram_webhook.get_settings", return_value=settings):
        yield settings


@pytest.fixture()
def client():
    """Create a TestClient that skips the real startup (no DB, no migrations)."""
    from app.telegram_webhook import router

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── Auth ──


def test_missing_secret_returns_401(client, _settings):
    resp = client.post("/tg-webhook", json={"update_id": 1})
    assert resp.status_code == 401


def test_wrong_secret_returns_401(client, _settings):
    resp = client.post(
        "/tg-webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_not_configured_returns_503(client, _settings_disabled):
    resp = client.post(
        "/tg-webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    assert resp.status_code == 503


def test_valid_secret_passes_auth(client, _settings):
    """A non-payment update with valid auth should return 200."""
    with patch("app.telegram_webhook._forward_to_bot") as fwd:
        resp = client.post(
            "/tg-webhook",
            json={"update_id": 1, "message": {"text": "hello"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
    assert resp.status_code == 200
    fwd.assert_called_once()


# ── pre_checkout_query ──


def test_pre_checkout_xtr_answers_ok(client, _settings):
    update = {
        "update_id": 2,
        "pre_checkout_query": {
            "id": "pcq-123",
            "currency": "XTR",
            "total_amount": 10,
            "invoice_payload": "42",
        },
    }
    with patch("app.telegram_webhook._answer_pre_checkout_query") as ans:
        resp = client.post(
            "/tg-webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
    assert resp.status_code == 200
    ans.assert_called_once_with(BOT_TOKEN, "pcq-123", ok=True)


def test_pre_checkout_non_xtr_rejects(client, _settings):
    update = {
        "update_id": 3,
        "pre_checkout_query": {
            "id": "pcq-456",
            "currency": "USD",
            "total_amount": 100,
            "invoice_payload": "43",
        },
    }
    with patch("app.telegram_webhook._answer_pre_checkout_query") as ans:
        resp = client.post(
            "/tg-webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
    assert resp.status_code == 200
    ans.assert_called_once_with(
        BOT_TOKEN, "pcq-456", ok=False, error_message="Unsupported currency"
    )


# ── successful_payment ──


def test_successful_payment_calls_mark_paid(client, _settings):
    update = {
        "update_id": 4,
        "message": {
            "successful_payment": {
                "currency": "XTR",
                "total_amount": 5,
                "invoice_payload": "99",
                "telegram_payment_charge_id": "tpc-1",
            }
        },
    }
    with (
        patch("app.telegram_webhook.get_db") as mock_get_db,
        patch("app.telegram_webhook._mark_invoice_paid_core") as mark,
    ):
        fake_db = MagicMock()
        fake_db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        mock_get_db.return_value = fake_db

        resp = client.post(
            "/tg-webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
    assert resp.status_code == 200
    mark.assert_called_once()
    call_kwargs = mark.call_args
    assert call_kwargs[0][1] == 99  # invoice_id
    assert call_kwargs[1]["actor"] == "telegram_stars:native_webhook"


def test_successful_payment_non_int_payload_is_safe(client, _settings):
    update = {
        "update_id": 5,
        "message": {
            "successful_payment": {
                "currency": "XTR",
                "total_amount": 5,
                "invoice_payload": "not-a-number",
            }
        },
    }
    resp = client.post(
        "/tg-webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    assert resp.status_code == 200


# ── Forwarding ──


def test_non_payment_update_forwarded_to_bot(client, _settings):
    update = {"update_id": 6, "message": {"text": "/start"}}
    with patch("app.telegram_webhook._forward_to_bot") as fwd:
        resp = client.post(
            "/tg-webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
    assert resp.status_code == 200
    fwd.assert_called_once()
    call_args = fwd.call_args[0]
    assert call_args[1] == BOT_INTERNAL_URL


def test_callback_query_forwarded_to_bot(client, _settings):
    update = {
        "update_id": 7,
        "callback_query": {"id": "cq-1", "data": "pay_123"},
    }
    with patch("app.telegram_webhook._forward_to_bot") as fwd:
        resp = client.post(
            "/tg-webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )
    assert resp.status_code == 200
    fwd.assert_called_once()


# ── Malformed input ──


def test_invalid_json_returns_400(client, _settings):
    resp = client.post(
        "/tg-webhook",
        content=b"not-json{{{",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": SECRET,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
