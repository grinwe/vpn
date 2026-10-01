"""Аудит-волна 2 — out-схемы schemas.py (находки #128, #132).

* #128 — cancel топап-инвойса (plan_id=None) обязан вернуть 200. Раньше
  ``InvoiceOut.plan_id: int`` (обязательное) роняло ``from_orm`` в
  ValidationError уже ПОСЛЕ ``db.commit()`` → статус в БД поменялся, а
  админ получал 500 (рассинхрон UI/БД).
* #132 — datetime-поля out-схем должны сериализоваться с хвостовым ``Z``
  (UTCDateTime), иначе браузер парсит naive-ISO как local time и admin-UI
  ошибается на TZ-offset.
"""
from __future__ import annotations

from datetime import datetime

from app import models, schemas

from .factories import make_user


def _make_topup_invoice(db, user) -> int:
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=None,  # топап не привязан к плану
        amount=500,
        currency="RUB",
        kind="topup",
    )
    db.add(invoice)
    db.commit()
    return invoice.id


# ── #128: cancel топап-инвойса не роняет сериализацию ────────────────

def test_cancel_topup_invoice_returns_200(client, db_session):
    """Отмена pending топап-инвойса (plan_id=None) → 200, plan_id=null в ответе."""
    user = make_user(db_session)
    invoice_id = _make_topup_invoice(db_session, user)

    resp = client.post(f"/api/invoices/{invoice_id}/cancel")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] is None
    assert body["status"] == "failed"


def test_invoice_out_from_orm_accepts_null_plan_id(db_session):
    """Прямой from_orm по топап-инвойсу не кидает ValidationError."""
    user = make_user(db_session)
    invoice_id = _make_topup_invoice(db_session, user)
    invoice = db_session.get(models.Invoice, invoice_id)

    out = schemas.InvoiceOut.from_orm(invoice)
    assert out.plan_id is None


# ── #132: out-datetime сериализуется с хвостовым Z ───────────────────

def test_out_schema_datetime_serialized_with_z():
    """naive-UTC datetime в out-схеме отдаётся с суффиксом ``Z`` (UTCDateTime)."""
    naive = datetime(2026, 7, 4, 12, 0, 0)  # без tzinfo, как в БД
    out = schemas.UserOut(id=1, telegram_id="1", created_at=naive)
    js = out.model_dump_json()
    # created_at обязан заканчиваться на Z, а не на голый ISO без зоны
    assert '"created_at":"2026-07-04T12:00:00Z"' in js


def test_invoice_out_created_at_has_z(client, db_session):
    """Интеграция: ответ cancel'а содержит created_at с суффиксом Z."""
    user = make_user(db_session)
    invoice_id = _make_topup_invoice(db_session, user)

    resp = client.post(f"/api/invoices/{invoice_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["created_at"].endswith("Z")
