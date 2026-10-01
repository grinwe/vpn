"""Устойчивость создания счёта (2026-09-19).

Секундный провал балансировщика lava («no available server» текстом вместо
JSON) превращался в «Не удалось создать счёт: 502: {...}» у человека, а админ
о сбое не узнавал. Здесь фиксируем: один повтор на транзиент, без повтора
на 4xx; человеку — понятная фраза, админу — пуш с сырой ошибкой.
"""
from __future__ import annotations

import json

import pytest
import requests

from app import models
from app.api_webapp import issue_token
from app.config import get_settings
from app.services import balance
from app.services.payments import ProviderError
from app.services.payments.checkout import (
    PROVIDER_UNAVAILABLE_MESSAGE,
    ProviderApiError,
)
from app.services.payments.lava_top import LavaTopProvider

from .factories import make_user


class _Resp:
    def __init__(self, status: int, body, *, text: str | None = None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _lava(monkeypatch, responses):
    """Провайдер с подменённым POST: отдаёт ответы по очереди, без пауз."""
    prov = LavaTopProvider(
        api_key="k", offer_id="o", webhook_secret="s", email_domain="d.test",
        payment_provider="PAY2ME", payment_method="SBP", name="lava_top_sbp",
    )
    calls: list[dict] = []
    queue = list(responses)

    def post(url, json=None, headers=None, timeout=None):  # noqa: A002
        calls.append({"url": url, "json": json})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(prov._session, "post", post)
    monkeypatch.setattr("app.services.payments.lava_top.time.sleep", lambda s: None)
    return prov, calls


_OK = {"id": "c-1", "paymentUrl": "https://app.lava.top/x"}


def test_lava_create_retries_once_on_non_json_5xx(monkeypatch):
    prov, calls = _lava(monkeypatch, [
        _Resp(503, ValueError("no json"), text="no available server\n"),
        _Resp(201, _OK),
    ])
    inv = prov.create_invoice(invoice_id=7, amount=100, currency="RUB")
    assert inv.external_id == "c-1"
    assert len(calls) == 2
    assert calls[1]["json"]["paymentMethod"] == "SBP"


def test_lava_create_retries_once_on_connection_error(monkeypatch):
    prov, calls = _lava(monkeypatch, [
        requests.ConnectionError("reset"),
        _Resp(201, _OK),
    ])
    assert prov.create_invoice(invoice_id=7, amount=100, currency="RUB").pay_url
    assert len(calls) == 2


def test_lava_create_gives_up_after_second_failure(monkeypatch):
    prov, calls = _lava(monkeypatch, [
        _Resp(502, ValueError("no json"), text="no available server\n"),
        _Resp(502, ValueError("no json"), text="no available server\n"),
    ])
    with pytest.raises(ProviderError, match="non-JSON response: no available server"):
        prov.create_invoice(invoice_id=7, amount=100, currency="RUB")
    assert len(calls) == 2


def test_lava_create_does_not_retry_4xx(monkeypatch):
    """«Restricted payment method type» — не транзиент: второй раз не ходим."""
    prov, calls = _lava(monkeypatch, [
        _Resp(400, {"error": "Restricted payment method type"}),
        _Resp(201, _OK),
    ])
    with pytest.raises(ProviderError, match="HTTP 400"):
        prov.create_invoice(invoice_id=7, amount=100, currency="RUB")
    assert len(calls) == 1


# ── Кабинет: человеку фраза, админу пуш ──────────────────────────────────


def _auth(user_id: int) -> dict:
    token = issue_token(user_id, get_settings().webapp_jwt_secret, 600)
    return {"Authorization": f"Bearer {token}"}


def _alerts(db) -> list[models.AuditLog]:
    return (
        db.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_payment_provider")
        .all()
    )


def test_webapp_topup_provider_failure_is_human_and_alerts_admin(
    client, db_session, monkeypatch
):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "1")

    def boom(db, invoice, **kwargs):
        raise ProviderApiError("lava_top: non-JSON response: no available server")

    monkeypatch.setattr("app.api_webapp.checkout_pending_invoice", boom)
    user = make_user(db_session, telegram_id="tg-prov-fail")
    db_session.commit()

    res = client.post(
        "/api/webapp/topup",
        json={"amount_kopecks": balance.MIN_TOPUP_KOPECKS, "provider": "lava_top_sbp"},
        headers=_auth(user.id),
    )
    assert res.status_code == 502, res.text
    detail = res.json()["detail"]
    assert detail == PROVIDER_UNAVAILABLE_MESSAGE
    assert "no available server" not in detail

    db_session.expire_all()
    alerts = _alerts(db_session)
    assert len(alerts) == 1
    assert "lava_top_sbp" in alerts[0].extra.get("text", "") or "lava_top_sbp" in json.dumps(
        alerts[0].extra, ensure_ascii=False
    )
    assert "no available server" in json.dumps(alerts[0].extra, ensure_ascii=False)

    # Второй сбой того же провайдера в течение часа — дедуп, пуша нет.
    res = client.post(
        "/api/webapp/topup",
        json={"amount_kopecks": balance.MIN_TOPUP_KOPECKS, "provider": "lava_top_sbp"},
        headers=_auth(user.id),
    )
    assert res.status_code == 502
    db_session.expire_all()
    assert len(_alerts(db_session)) == 1


def test_bot_checkout_provider_failure_is_human(client, db_session, monkeypatch):
    """Бот-путь /api/invoices/{id}/checkout — тот же текст и тот же пуш."""
    from app.api import payments as payments_api

    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "1")

    def boom(db, invoice, **kwargs):
        raise ProviderApiError("lava_top: HTTP 400: Restricted payment method type")

    monkeypatch.setattr(payments_api, "checkout_pending_invoice", boom)
    user = make_user(db_session, telegram_id="tg-bot-prov-fail")
    invoice = models.Invoice(
        user_id=user.id, amount=100, currency="RUB",
        action=models.InvoiceAction.new_subscription,
    )
    db_session.add(invoice)
    db_session.commit()

    res = client.post(
        f"/api/invoices/{invoice.id}/checkout",
        json={"provider": "lava_top"},
        headers={"X-Admin-Token": "x", "X-Admin-Actor": user.telegram_id},
    )
    assert res.status_code == 502, res.text
    assert res.json()["detail"] == PROVIDER_UNAVAILABLE_MESSAGE
    db_session.expire_all()
    assert len(_alerts(db_session)) == 1
