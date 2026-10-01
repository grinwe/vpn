"""Тест авто-сверки карточных платежей lava.top (Stage 9b).

``run_lava_reconcile_tick`` зачисляет pending-счета, чья продажа у lava
COMPLETED — на случай, когда вебхук lava не долетел (наблюдалось в проде).
Здесь провайдер мокается: проверяем, что тик находит pending-топап по
``clientUtm.utm_content`` и проводит его через ``_mark_invoice_paid_core``.

С 2026-09-19 lava — два имени провайдера (``lava_top`` карта, ``lava_top_sbp``
СБП) с общим API-ключом: сверка одна и обязана видеть Payment-строки обоих.
"""
from __future__ import annotations

import pytest

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


def _pending_payment(db, inv, *, provider, external_id, amount=100.0):
    row = models.Payment(
        invoice_id=inv.id, provider=provider, external_id=external_id,
        amount=amount, currency="RUB", status=models.PaymentStatus.pending,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_reconcile_marks_sbp_payment_row(db_session, monkeypatch):
    """СБП-платёж записан как ``lava_top_sbp``; сверка (провайдер lava_top,
    ключ общий) обязана найти и пометить ИМЕННО эту строку — иначе счёт
    зачислится с payment_id=None, а строка навсегда останется pending."""
    user = make_user(db_session, telegram_id="lava-recon-sbp")
    inv = _make_topup_invoice(db_session, user, amount=100.0)
    sbp_row = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-sbp")
    before = user.balance_kopecks

    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c-sbp", "completed": True},
    ])

    res = run_lava_reconcile_tick()
    assert res["credited"] == 1

    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.paid
    assert db_session.get(models.Payment, sbp_row.id).status == models.PaymentStatus.paid
    assert db_session.get(models.User, user.id).balance_kopecks == before + 10000


def test_reconcile_picks_family_row_by_contract_id_not_by_name(db_session, monkeypatch):
    """Человек нажал СБП, потом карту: две pending-строки (lava_top_sbp и
    lava_top) на одном счёте. Продажа с contract_id СБП-строки помечает её,
    а карточную оставляет pending — матч по contract_id внутри семейства,
    а не «последняя pending с именем lava_top»."""
    user = make_user(db_session, telegram_id="lava-recon-family")
    inv = _make_topup_invoice(db_session, user, amount=100.0)
    sbp_row = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-fam-sbp")
    card_row = _pending_payment(db_session, inv, provider="lava_top", external_id="c-fam-card")
    assert card_row.id > sbp_row.id  # слепой id DESC выбрал бы карту

    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c-fam-sbp", "completed": True},
    ])

    assert run_lava_reconcile_tick()["credited"] == 1
    db_session.expire_all()
    assert db_session.get(models.Payment, sbp_row.id).status == models.PaymentStatus.paid
    assert db_session.get(models.Payment, card_row.id).status == models.PaymentStatus.pending


def test_reconcile_alerts_double_paid_for_sbp_row(db_session, monkeypatch):
    """Счёт уже paid (карта), а lava сообщает COMPLETED-продажу по живой
    pending-строке ``lava_top_sbp`` — это второй платёж за тот же счёт, и
    алерт на возврат обязан сработать для СБП-имени так же, как для карты."""
    user = make_user(db_session, telegram_id="lava-recon-sbp-double")
    inv = _make_topup_invoice(db_session, user, amount=100.0)
    inv.status = models.InvoiceStatus.paid
    db_session.commit()
    _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-sbp-second")

    alerts: list[str] = []
    monkeypatch.setattr(
        "app.services.admin_notify.notify_admins",
        lambda db, *, kind, text, dedup_key=None, extra=None, window_sec=None,
        autocommit=False: alerts.append(kind),
    )
    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c-sbp-second", "completed": True},
    ])

    res = run_lava_reconcile_tick()
    assert res["credited"] == 0
    assert "payment_double_paid" in alerts


def _capture_alerts(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        "app.services.admin_notify.notify_admins",
        lambda db, *, kind, **kw: seen.append(kind),
    )
    return seen


def test_stale_sale_for_already_paid_contract_does_not_alert(db_session, monkeypatch):
    """(в) Счёт оплачен по СБП; рядом штатно висит брошенная pending-строка
    карты (человек нажал обе кнопки). Продажа lava по контракту УЖЕ paid
    СБП-строки — это наш же зачтённый платёж, а не двойная оплата: алерта
    быть не должно, хотя pending-сосед жив (ложный алерт раз в сутки —
    ревью 2026-09-19)."""
    user = make_user(db_session, telegram_id="lava-stale-paid")
    inv = _make_topup_invoice(db_session, user, amount=100.0)
    inv.status = models.InvoiceStatus.paid
    db_session.commit()
    sbp_row = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-v-sbp")
    sbp_row.status = models.PaymentStatus.paid
    db_session.commit()
    card_row = _pending_payment(db_session, inv, provider="lava_top", external_id="c-v-card")

    alerts = _capture_alerts(monkeypatch)
    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c-v-sbp", "completed": True},
    ])
    res = run_lava_reconcile_tick()
    assert res["credited"] == 0
    assert alerts == []
    db_session.expire_all()
    assert db_session.get(models.Payment, card_row.id).status == models.PaymentStatus.pending


@pytest.mark.parametrize(
    "contract_id",
    ["c-v-card", "c-v-none"],
    ids=["contract-of-pending-sibling", "contract-without-row"],
)
def test_stale_sale_for_unpaid_or_unknown_contract_alerts(db_session, monkeypatch, contract_id):
    """(в) Тот же оплаченный счёт, но продажа — по контракту pending-строки
    (человек реально оплатил и карту) или по контракту, которого у нас нет
    вовсе: это вторая оплата, алерт payment_double_paid обязателен."""
    user = make_user(db_session, telegram_id=f"lava-stale-{contract_id}")
    inv = _make_topup_invoice(db_session, user, amount=100.0)
    inv.status = models.InvoiceStatus.paid
    db_session.commit()
    sbp_row = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-v-sbp")
    sbp_row.status = models.PaymentStatus.paid
    db_session.commit()
    _pending_payment(db_session, inv, provider="lava_top", external_id="c-v-card")

    alerts = _capture_alerts(monkeypatch)
    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": contract_id, "completed": True},
    ])
    res = run_lava_reconcile_tick()
    assert res["credited"] == 0
    assert alerts == ["payment_double_paid"]


def test_stale_sale_without_contract_id_keeps_pending_row_heuristic(db_session, monkeypatch):
    """Без contract_id в продаже различить нечем — прежняя эвристика: живая
    pending-строка семейства = второй платёж (алерт), нет строк = наш же
    зачтённый платёж (тишина)."""
    user = make_user(db_session, telegram_id="lava-stale-nocontract")
    inv = _make_topup_invoice(db_session, user, amount=100.0)
    inv.status = models.InvoiceStatus.paid
    db_session.commit()

    alerts = _capture_alerts(monkeypatch)
    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "", "completed": True},
    ])
    run_lava_reconcile_tick()
    assert alerts == []

    _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-nc-sbp")
    run_lava_reconcile_tick()
    assert alerts == ["payment_double_paid"]


def test_reconcile_unmatched_contract_does_not_mark_foreign_row(db_session, monkeypatch):
    """(г) Продажа с contract_id, не совпадающим ни с одной строкой счёта:
    деньги пришли — счёт зачисляем, но чужую pending-строку (другой способ
    того же счёта) paid НЕ помечаем: её контракт у lava не оплачен."""
    user = make_user(db_session, telegram_id="lava-recon-unmatched")
    inv = _make_topup_invoice(db_session, user, amount=100.0)
    card_row = _pending_payment(db_session, inv, provider="lava_top", external_id="c-g-card")
    before = user.balance_kopecks

    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "c-g-other", "completed": True},
    ])
    res = run_lava_reconcile_tick()
    assert res["credited"] == 1
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.paid
    assert db_session.get(models.User, user.id).balance_kopecks == before + 10000
    assert db_session.get(models.Payment, card_row.id).status == models.PaymentStatus.pending


def test_reconcile_without_contract_id_falls_back_to_last_pending(db_session, monkeypatch):
    """Фолбэк «последняя pending по id» остался только для продажи без
    contract_id — тогда различить строки нечем."""
    user = make_user(db_session, telegram_id="lava-recon-nocontract")
    inv = _make_topup_invoice(db_session, user, amount=100.0)
    older = _pending_payment(db_session, inv, provider="lava_top", external_id="c-f-card")
    newer = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-f-sbp")

    _patch(monkeypatch, [
        {"invoice_id": inv.id, "amount": 100.0, "currency": "RUB",
         "contract_id": "", "completed": True},
    ])
    assert run_lava_reconcile_tick()["credited"] == 1
    db_session.expire_all()
    assert db_session.get(models.Payment, newer.id).status == models.PaymentStatus.paid
    assert db_session.get(models.Payment, older.id).status == models.PaymentStatus.pending
