"""Тест авто-сверки карточных платежей lava.top (Stage 9b).

``run_lava_reconcile_tick`` зачисляет pending-счета, чья продажа у lava
COMPLETED — на случай, когда вебхук lava не долетел (наблюдалось в проде).
Здесь провайдер мокается: проверяем, что тик находит pending-топап по
``clientUtm.utm_content`` и проводит его через ``_mark_invoice_paid_core``.
"""
from __future__ import annotations

from app import models
from app.worker import run_lava_reconcile_tick

from .factories import make_user


class _FakeProvider:
    name = "lava_top"

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_recent_invoices(self) -> list[dict]:
        return self._rows


def _make_topup_invoice(db, user, *, amount=100.0):
    inv = models.Invoice(
        user_id=user.id,
        amount=amount,
        currency="RUB",
        kind="topup",
        status=models.InvoiceStatus.pending,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _patch(monkeypatch, rows):
    monkeypatch.setenv("LAVA_TOP_API_KEY", "x")
    monkeypatch.setattr("app.queue.schedule_tick", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.payments.get_provider", lambda name=None: _FakeProvider(rows)
    )


def test_reconcile_credits_pending_invoice_from_completed_sale(db_session, monkeypatch):
    user = make_user(db_session, telegram_id="lava-recon-1")
    inv = _make_topup_invoice(db_session, user, amount=100.0)
    before = user.balance_kopecks

    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c1", "completed": True},
    ])

    res = run_lava_reconcile_tick()
    assert res["credited"] == 1

    db_session.expire_all()
    inv2 = db_session.get(models.Invoice, inv.id)
    user2 = db_session.get(models.User, user.id)
    assert inv2.status == models.InvoiceStatus.paid
    assert user2.balance_kopecks == before + 10000  # 100 ₽


def test_reconcile_skips_non_completed_and_missing_utm(db_session, monkeypatch):
    user = make_user(db_session, telegram_id="lava-recon-2")
    inv = _make_topup_invoice(db_session, user, amount=100.0)

    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c2", "completed": False},  # не завершена
        {"invoice_id": None, "amount": 100.0, "currency": "RUB",
         "contract_id": "c3", "completed": True},    # нет нашего invoice_id
    ])

    res = run_lava_reconcile_tick()
    assert res["credited"] == 0
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.pending


def test_reconcile_skips_underpaid(db_session, monkeypatch):
    user = make_user(db_session, telegram_id="lava-recon-3")
    inv = _make_topup_invoice(db_session, user, amount=200.0)  # счёт на 200

    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c4", "completed": True},  # оплачено лишь 100
    ])

    res = run_lava_reconcile_tick()
    assert res["credited"] == 0
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.pending


def test_reconcile_idempotent_on_already_paid(db_session, monkeypatch):
    user = make_user(db_session, telegram_id="lava-recon-4")
    inv = _make_topup_invoice(db_session, user, amount=100.0)

    rows = [{"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
             "contract_id": "c5", "completed": True}]
    _patch(monkeypatch, rows)

    first = run_lava_reconcile_tick()
    assert first["credited"] == 1
    db_session.expire_all()
    user_after_first = db_session.get(models.User, user.id).balance_kopecks

    # Повторный прогон с той же продажей — счёт уже paid, второй кредит НЕ идёт.
    second = run_lava_reconcile_tick()
    assert second["credited"] == 0
    db_session.expire_all()
    assert db_session.get(models.User, user.id).balance_kopecks == user_after_first


def test_reconcile_skips_when_not_configured(db_session, monkeypatch):
    monkeypatch.delenv("LAVA_TOP_API_KEY", raising=False)
    monkeypatch.setattr("app.queue.schedule_tick", lambda *a, **k: None)
    res = run_lava_reconcile_tick()
    assert res == {"skipped": "not_configured"}
