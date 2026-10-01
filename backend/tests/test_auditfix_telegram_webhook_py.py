"""Аудит-фиксы #2 и #209 — Telegram Stars вебхук (app/telegram_webhook.py).

#2  — pre_checkout_query подтверждается только для существующего
      pending-инвойса с совпадающей суммой в Stars; иначе ok=False и
      Telegram не списывает звёзды.
#209 — сбой _mark_invoice_paid_core в successful_payment больше не
      глотается: пишется error-лог, админам уходит алерт
      (admin_alert_stars_payment_failed), а на 5xx вебхук отвечает
      не-200, чтобы Telegram повторил апдейт.

Гоняется на реальном Postgres из conftest; Bot API замокан.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app import models

from .factories import make_plan, make_user

SECRET = "auditfix-webhook-secret"
BOT_TOKEN = "123456:ABC-auditfix"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


@pytest.fixture()
def _settings():
    settings = MagicMock()
    settings.telegram_webhook_secret_token = SECRET
    settings.telegram_webhook_url = "https://example.com/tg-webhook"
    settings.bot_internal_webhook_url = None
    settings.bot_token = BOT_TOKEN
    with patch("app.telegram_webhook.get_settings", return_value=settings):
        yield settings


@pytest.fixture(autouse=True)
def _stars_rate(monkeypatch: pytest.MonkeyPatch):
    """Пиним курс 1 ⭐ = 1 ₽, чтобы ожидаемые суммы были детерминированы."""
    monkeypatch.setenv("WEBAPP_STARS_PER_RUB", "1.0")


def _make_invoice(
    db,
    *,
    status: models.InvoiceStatus = models.InvoiceStatus.pending,
    amount: float = 100,
    currency: str = "RUB",
    kind: str = "subscription",
) -> models.Invoice:
    user = make_user(db, telegram_id=f"tg-audit-{status.value}-{kind}")
    plan = make_plan(db, name=f"plan-audit-{status.value}-{kind}")
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        amount=amount,
        currency=currency,
        status=status,
        kind=kind,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def _pcq_update(invoice_id, total_amount: int = 100) -> dict:
    return {
        "update_id": 100,
        "pre_checkout_query": {
            "id": "pcq-audit",
            "from": {"id": 555},
            "currency": "XTR",
            "total_amount": total_amount,
            "invoice_payload": str(invoice_id),
        },
    }


def _sp_update(invoice_id, total_amount: int = 100) -> dict:
    return {
        "update_id": 101,
        "message": {
            "from": {"id": 555},
            "successful_payment": {
                "currency": "XTR",
                "total_amount": total_amount,
                "invoice_payload": str(invoice_id),
                "telegram_payment_charge_id": "tpc-audit",
            },
        },
    }


def _post_pcq(client, update):
    with patch("app.telegram_webhook._answer_pre_checkout_query") as ans:
        resp = client.post("/tg-webhook", json=update, headers=HEADERS)
    assert resp.status_code == 200
    ans.assert_called_once()
    return ans.call_args


# ── #2: pre_checkout валидация инвойса ──


def test_pre_checkout_rejects_missing_invoice(client, _settings):
    call = _post_pcq(client, _pcq_update(999_999))
    assert call.kwargs["ok"] is False
    assert "не найден" in call.kwargs["error_message"]


def test_pre_checkout_rejects_failed_invoice(client, _settings, db_session):
    invoice = _make_invoice(db_session, status=models.InvoiceStatus.failed)
    call = _post_pcq(client, _pcq_update(invoice.id))
    assert call.kwargs["ok"] is False


def test_pre_checkout_rejects_amount_mismatch(client, _settings, db_session):
    # 100 ₽ при курсе 1.0 = 100 ⭐, а Telegram шлёт 1 ⭐ — стухшая ссылка.
    invoice = _make_invoice(db_session, amount=100, currency="RUB")
    call = _post_pcq(client, _pcq_update(invoice.id, total_amount=1))
    assert call.kwargs["ok"] is False
    assert "устарела" in call.kwargs["error_message"]


def test_pre_checkout_accepts_pending_rub_invoice(client, _settings, db_session):
    invoice = _make_invoice(db_session, amount=100, currency="RUB")
    call = _post_pcq(client, _pcq_update(invoice.id, total_amount=100))
    assert call.kwargs["ok"] is True


def test_pre_checkout_accepts_webapp_xtr_invoice(client, _settings, db_session):
    # WebApp-чекаут кладёт amount уже в звёздах (currency=XTR).
    invoice = _make_invoice(db_session, amount=67, currency="XTR")
    call = _post_pcq(client, _pcq_update(invoice.id, total_amount=67))
    assert call.kwargs["ok"] is True


def test_pre_checkout_topup_amount_is_rub_even_with_xtr(client, _settings, db_session):
    # topup-инвойс: amount всегда в рублях (webapp_topup), даже при XTR.
    invoice = _make_invoice(
        db_session, amount=100, currency="XTR", kind="topup"
    )
    call = _post_pcq(client, _pcq_update(invoice.id, total_amount=100))
    assert call.kwargs["ok"] is True


# ── #209: сбой mark_paid → алерт + ретрай для 5xx ──


def _alert_rows(db):
    return (
        db.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_stars_payment_failed")
        .all()
    )


def test_successful_payment_4xx_alerts_admins_and_keeps_status(
    client, _settings, db_session, monkeypatch
):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    invoice = _make_invoice(db_session, status=models.InvoiceStatus.failed)

    resp = client.post("/tg-webhook", json=_sp_update(invoice.id), headers=HEADERS)

    # 4xx — детерминированная ошибка состояния: ретрай не поможет,
    # отвечаем 200, но алерт админам обязан быть.
    assert resp.status_code == 200
    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.failed
    rows = _alert_rows(db_session)
    assert len(rows) == 1
    extra = rows[0].extra
    assert extra["invoice_id"] == invoice.id
    assert extra["telegram_id"] == "111"  # получатель-админ, не юзер
    assert extra["telegram_id_user"] == 555


def test_successful_payment_5xx_returns_500_for_telegram_retry(
    client, _settings, db_session, monkeypatch
):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    invoice = _make_invoice(db_session)

    with patch(
        "app.telegram_webhook._mark_invoice_paid_core",
        side_effect=HTTPException(
            status_code=500, detail="Failed to create or update subscription"
        ),
    ):
        resp = client.post(
            "/tg-webhook", json=_sp_update(invoice.id), headers=HEADERS
        )

    # Не-200 → Telegram повторит апдейт; инвойс остался pending.
    assert resp.status_code == 500
    db_session.expire_all()
    assert invoice.status == models.InvoiceStatus.pending
    assert len(_alert_rows(db_session)) == 1


def test_successful_payment_alert_deduped_on_retry(
    client, _settings, db_session, monkeypatch
):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")
    invoice = _make_invoice(db_session, status=models.InvoiceStatus.failed)

    for _ in range(2):
        resp = client.post(
            "/tg-webhook", json=_sp_update(invoice.id), headers=HEADERS
        )
        assert resp.status_code == 200

    # Дедуп по invoice_id в окне — повтор вебхука не спамит админов.
    assert len(_alert_rows(db_session)) == 1
