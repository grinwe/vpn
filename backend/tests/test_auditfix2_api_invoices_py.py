"""Аудит-волна 2 — денежные инварианты invoices.py (находки #39, #114).

* #39 — повторный вебхук по уже оплаченному инвойсу с pending-Payment:
  Payment обязан зафиксироваться в paid (раньше ранний выход из ветки
  ``invoice.status == paid`` терял отметку из-за отсутствия commit).
* #114 — mark_unpaid по topup-инвойсу запрещён (баланс уже зачислен),
  а topup-ветка ядра идемпотентна по reference=invoice:<id>.
"""
from __future__ import annotations

from app import models

from .factories import make_plan, make_user


def _make_topup_invoice(db, user, amount) -> int:
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=None,
        amount=amount,
        currency="RUB",
        kind="topup",
    )
    db.add(invoice)
    db.commit()
    return invoice.id


# ── #39: payment фиксируется на уже оплаченном инвойсе ───────────────

def test_mark_paid_on_already_paid_invoice_commits_payment(client, db_session):
    """Инвойс уже paid + payment pending → mark_paid с payment_id фиксирует
    payment в paid (сценарий: ручной mark_paid, затем ретрай вебхука)."""
    plan = make_plan(db_session)
    user = make_user(db_session)

    invoice = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        amount=plan.price,
        currency="RUB",
        status=models.InvoiceStatus.paid,
    )
    db_session.add(invoice)
    db_session.commit()

    payment = models.Payment(
        invoice_id=invoice.id,
        amount=plan.price,
        currency="RUB",
        status=models.PaymentStatus.pending,
        provider="sbp:acme",
        external_id=str(invoice.id),
    )
    db_session.add(payment)
    db_session.commit()
    invoice_id, payment_id = invoice.id, payment.id

    resp = client.post(
        f"/api/invoices/{invoice_id}/mark_paid",
        json={"payment_id": payment_id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paid"

    db_session.expire_all()
    # Ключевая проверка: отметка платежа переживает закрытие сессии.
    assert db_session.get(models.Payment, payment_id).status == models.PaymentStatus.paid


# ── #114: topup-инвойс нельзя откатить в pending ─────────────────────

def test_mark_unpaid_topup_invoice_rejected(client, db_session):
    user = make_user(db_session)
    invoice_id = _make_topup_invoice(db_session, user, 100)

    resp = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    assert db_session.get(models.User, user.id).balance_kopecks == 100 * 100

    # Откат topup-инвойса запрещён — 400, статус остаётся paid.
    resp = client.post(f"/api/invoices/{invoice_id}/mark_unpaid")
    assert resp.status_code == 400

    db_session.expire_all()
    assert db_session.get(models.Invoice, invoice_id).status == models.InvoiceStatus.paid
    assert db_session.get(models.User, user.id).balance_kopecks == 100 * 100


def test_batch_mark_unpaid_skips_topup(client, db_session):
    user = make_user(db_session)
    invoice_id = _make_topup_invoice(db_session, user, 100)

    resp = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert resp.status_code == 200

    resp = client.post(
        "/api/invoices/batch",
        json={"ids": [invoice_id], "action": "mark_unpaid"},
    )
    assert resp.status_code == 200, resp.text
    assert invoice_id in resp.json()["skipped"]

    db_session.expire_all()
    assert db_session.get(models.Invoice, invoice_id).status == models.InvoiceStatus.paid
    assert db_session.get(models.User, user.id).balance_kopecks == 100 * 100


def test_topup_credit_is_idempotent_by_reference(client, db_session):
    """Даже если topup-инвойс насильно вернуть в pending (минуя защиту) и
    повторно оплатить — баланс не докручивается: ветка topup дедупит по
    reference=invoice:<id>."""
    user = make_user(db_session)
    invoice_id = _make_topup_invoice(db_session, user, 100)

    resp = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(models.User, user.id).balance_kopecks == 100 * 100

    # Имитируем рассинхрон: инвойс снова pending (например, до фикса #114
    # его успели откатить), приходит повторный вебхук/клик mark_paid.
    inv = db_session.get(models.Invoice, invoice_id)
    inv.status = models.InvoiceStatus.pending
    db_session.commit()

    resp = client.post(f"/api/invoices/{invoice_id}/mark_paid")
    assert resp.status_code == 200

    db_session.expire_all()
    # Баланс не удвоился — вторая topup-строка не записана.
    assert db_session.get(models.User, user.id).balance_kopecks == 100 * 100
    assert db_session.get(models.Invoice, invoice_id).status == models.InvoiceStatus.paid
    tx_count = (
        db_session.query(models.BalanceTransaction)
        .filter_by(reference=f"invoice:{invoice_id}")
        .count()
    )
    assert tx_count == 1
