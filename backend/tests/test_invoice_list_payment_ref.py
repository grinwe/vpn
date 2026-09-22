"""Список счетов в админке несёт платёж: провайдер + внешний id (contractId
lava.top) + момент оплаты — для сверки с кабинетом партнёра и доказательств
оказания услуги (проверка СБ lava.top, 2026-09-22)."""
from __future__ import annotations

from app import models

from .factories import make_plan, make_user


def _invoice(db, user, amount=100):
    inv = models.Invoice(
        user_id=user.id, amount=amount, currency="RUB",
        action=models.InvoiceAction.new_subscription, kind="topup",
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _payment(db, inv, *, provider, external_id, status):
    p = models.Payment(
        invoice_id=inv.id, amount=inv.amount, currency="RUB",
        status=status, provider=provider, external_id=external_id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_invoice_list_shows_paid_payment_ref(client, db_session):
    make_plan(db_session)
    user = make_user(db_session, telegram_id="tg-inv-ref")
    inv = _invoice(db_session, user)
    # Сначала pending по СБП, потом оплачен картой — в списке должен быть paid.
    _payment(db_session, inv, provider="lava_top_sbp", external_id="c-sbp-1",
             status=models.PaymentStatus.pending)
    _payment(db_session, inv, provider="lava_top", external_id="c-card-2",
             status=models.PaymentStatus.paid)
    inv.status = models.InvoiceStatus.paid
    db_session.commit()

    res = client.get("/api/invoices?limit=50", headers={"X-Admin-Token": "x"})
    assert res.status_code == 200, res.text
    row = next(r for r in res.json() if r["id"] == inv.id)
    assert row["payment_provider"] == "lava_top"
    assert row["payment_external_id"] == "c-card-2"
    assert row["payment_status"] == "paid"
    assert row["paid_at"] is not None


def test_invoice_list_without_payment_has_nulls(client, db_session):
    make_plan(db_session)
    user = make_user(db_session, telegram_id="tg-inv-nopay")
    inv = _invoice(db_session, user)
    res = client.get("/api/invoices?limit=50", headers={"X-Admin-Token": "x"})
    row = next(r for r in res.json() if r["id"] == inv.id)
    assert row["payment_provider"] is None and row["payment_external_id"] is None
    assert row["paid_at"] is None
