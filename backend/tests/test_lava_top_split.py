"""Разделение lava.top на два имени провайдера (2026-09-19).

lava закрыл карту у агрегатора PAY2ME: единая кнопка «Карта РФ / СБП»
(PAY2ME без ``paymentMethod``) стала падать с 400 «Restricted payment method
type». Теперь имени два — ``lava_top`` (карта, SMART_GLOCAL) и
``lava_top_sbp`` (СБП, PAY2ME) — при ОДНОЙ интеграции: общий API-ключ, общий
секрет и один вебхук-URL ``/api/payments/webhook/lava_top``.

Здесь — сквозные проверки, что общая часть действительно общая:

* вебхук на ``/webhook/lava_top`` (настоящий ``LavaTopProvider`` из env,
  настоящая проверка ``X-Api-Key``) зачисляет счёт, чья Payment-строка
  записана как ``lava_top_sbp``, и помечает именно её;
* при двух pending-строках семейства выбирается та, чей ``external_id``
  совпал с ``contractId`` события, чужие провайдеры не трогаются;
* ретрай уже зачтённого вебхука матчит СВОЮ paid-строку по contractId, а не
  «последнюю pending» (иначе он пометил бы paid неоплаченную строку соседнего
  способа — ревью 2026-09-19) и не поднимает алерт двойной оплаты;
* вебхук по контракту, которого у нас нет, на уже оплаченный счёт — алерт
  ``payment_double_paid``; фолбэк «последняя pending» — только когда
  contractId в событии отсутствует;
* подписи кнопок в боте: СБП и карта — две разные кнопки.

Юнит-тесты драйвера — test_payments_lava_top.py, страница починки —
test_sub_fix_pay.py, сверка — test_lava_reconcile.py.
"""
from __future__ import annotations

import json

import pytest

from app import models

from .factories import make_user

WEBHOOK_SECRET = "hooksecret-split"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.rate_limit import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def alerts(monkeypatch):
    """Виды алертов, ушедших админам (notify_admins импортируется лениво —
    патч модульного атрибута ловит и вебхук, и тик сверки)."""
    seen: list[str] = []
    monkeypatch.setattr(
        "app.services.admin_notify.notify_admins",
        lambda db, *, kind, **kw: seen.append(kind),
    )
    return seen


@pytest.fixture
def lava_env(monkeypatch):
    """Настоящий get_provider("lava_top") из env — без моков драйвера."""
    monkeypatch.setenv("LAVA_TOP_API_KEY", "k")
    monkeypatch.setenv("LAVA_TOP_OFFER_ID", "offer-1")
    monkeypatch.setenv("LAVA_TOP_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("LAVA_TOP_EMAIL_DOMAIN", "d.example")
    for var in ("LAVA_TOP_CARD_PROVIDER", "LAVA_TOP_SBP_PROVIDER", "LAVA_TOP_PAYMENT_PROVIDER"):
        monkeypatch.delenv(var, raising=False)


def _topup_invoice(db, user, *, amount=100.0):
    inv = models.Invoice(
        user_id=user.id, amount=amount, currency="RUB", kind="topup",
        status=models.InvoiceStatus.pending,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _pending_payment(db, inv, *, provider, external_id, amount=100.0):
    row = models.Payment(
        invoice_id=inv.id, provider=provider, external_id=external_id,
        amount=amount, currency="RUB", status=models.PaymentStatus.pending,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _lava_webhook(invoice_id: int, contract_id: str, amount: float = 100.0) -> bytes:
    return json.dumps({
        "eventType": "payment.success",
        "contractId": contract_id,
        "status": "completed",
        "amount": amount,
        "currency": "RUB",
        "clientUtm": {"utm_content": str(invoice_id)},
        "buyer": {"email": f"inv{invoice_id}@d.example"},
    }).encode()


def _post_webhook(client, body: bytes, *, secret: str = WEBHOOK_SECRET):
    return client.post(
        "/api/payments/webhook/lava_top",
        content=body,
        headers={"X-Api-Key": secret, "content-type": "application/json"},
    )


def test_webhook_on_lava_top_url_credits_sbp_payment_row(client, db_session, lava_env):
    """Вебхук приходит на /webhook/lava_top (у lava один URL на аккаунт), а
    строка СБП-платежа записана как lava_top_sbp: счёт зачисляется, строка
    помечается paid. До семейства имён она осталась бы pending навсегда, а
    счёт зачислился бы с payment_id=None."""
    user = make_user(db_session, telegram_id="lava-split-hook")
    inv = _topup_invoice(db_session, user, amount=100.0)
    sbp_row = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-hook-sbp")
    # Чужой провайдер на том же счёте (меню Stage 9b): семейство lava его
    # не включает — строка не должна быть помечена.
    stars_row = _pending_payment(db_session, inv, provider="telegram_stars", external_id="s-1")
    before = user.balance_kopecks or 0

    resp = _post_webhook(client, _lava_webhook(inv.id, "c-hook-sbp"))
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.paid
    assert db_session.get(models.Payment, sbp_row.id).status == models.PaymentStatus.paid
    assert db_session.get(models.Payment, stars_row.id).status == models.PaymentStatus.pending
    assert (db_session.get(models.User, user.id).balance_kopecks or 0) == before + 10_000


def test_webhook_matches_family_row_by_contract_id(client, db_session, lava_env):
    """СБП и карта на одном счёте (человек передумал): две pending-строки
    семейства. Событие с contractId карточной строки помечает карту, СБП
    остаётся pending — матч по external_id внутри семейства, а не «последняя
    pending с именем из URL»."""
    user = make_user(db_session, telegram_id="lava-split-family")
    inv = _topup_invoice(db_session, user, amount=100.0)
    card_row = _pending_payment(db_session, inv, provider="lava_top", external_id="c-fam-card")
    sbp_row = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-fam-sbp")
    assert sbp_row.id > card_row.id  # слепой id DESC выбрал бы СБП

    resp = _post_webhook(client, _lava_webhook(inv.id, "c-fam-card"))
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    assert db_session.get(models.Payment, card_row.id).status == models.PaymentStatus.paid
    assert db_session.get(models.Payment, sbp_row.id).status == models.PaymentStatus.pending


def test_webhook_retry_keeps_sibling_pending_and_does_not_alert(
    client, db_session, lava_env, alerts
):
    """(а) Две pending-строки семейства (карта и СБП) на одном счёте. Вебхук
    по контракту СБП зачисляет СБП-строку, карточная остаётся pending, алерта
    нет. Ретрай того же вебхука: карточная ПО-ПРЕЖНЕМУ pending (ретрай матчит
    свою paid-строку по contractId, а не «последнюю pending»), счёт paid,
    алерта payment_double_paid НЕТ."""
    user = make_user(db_session, telegram_id="lava-split-retry")
    inv = _topup_invoice(db_session, user, amount=100.0)
    card_row = _pending_payment(db_session, inv, provider="lava_top", external_id="c-a-card")
    sbp_row = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-a-sbp")
    body = _lava_webhook(inv.id, "c-a-sbp")

    first = _post_webhook(client, body)
    assert first.status_code == 200, first.text
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.paid
    assert db_session.get(models.Payment, sbp_row.id).status == models.PaymentStatus.paid
    assert db_session.get(models.Payment, card_row.id).status == models.PaymentStatus.pending
    assert alerts == []
    balance_after_first = db_session.get(models.User, user.id).balance_kopecks

    retry = _post_webhook(client, body)
    assert retry.status_code == 200, retry.text
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.paid
    assert db_session.get(models.Payment, sbp_row.id).status == models.PaymentStatus.paid
    assert db_session.get(models.Payment, card_row.id).status == models.PaymentStatus.pending
    assert "payment_double_paid" not in alerts
    assert alerts == []
    # Идемпотентность денег: второй раз баланс не растёт.
    assert db_session.get(models.User, user.id).balance_kopecks == balance_after_first


def test_webhook_unknown_contract_on_paid_invoice_alerts(client, db_session, lava_env, alerts):
    """(б) Счёт уже paid (СБП-строка зачтена), приходит вебхук по контракту,
    которого нет ни в одной нашей строке, — это вторая оплата: алерт
    payment_double_paid обязан уйти, а чужая pending-строка (карта) не
    должна быть помечена paid «за компанию»."""
    user = make_user(db_session, telegram_id="lava-split-unknown")
    inv = _topup_invoice(db_session, user, amount=100.0)
    card_row = _pending_payment(db_session, inv, provider="lava_top", external_id="c-b-card")
    sbp_row = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-b-sbp")
    assert _post_webhook(client, _lava_webhook(inv.id, "c-b-sbp")).status_code == 200
    assert alerts == []

    resp = _post_webhook(client, _lava_webhook(inv.id, "c-b-unknown"))
    assert resp.status_code == 200, resp.text
    assert alerts == ["payment_double_paid"]
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.paid
    assert db_session.get(models.Payment, sbp_row.id).status == models.PaymentStatus.paid
    assert db_session.get(models.Payment, card_row.id).status == models.PaymentStatus.pending


def test_webhook_sibling_contract_on_paid_invoice_alerts(client, db_session, lava_env, alerts):
    """Счёт оплачен по СБП, потом долетает вебхук по карточному контракту с
    ещё живой pending-строкой — человек реально заплатил дважды: строка
    помечается paid (деньги пришли), и уходит алерт на возврат."""
    user = make_user(db_session, telegram_id="lava-split-sibling")
    inv = _topup_invoice(db_session, user, amount=100.0)
    card_row = _pending_payment(db_session, inv, provider="lava_top", external_id="c-s-card")
    _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-s-sbp")
    assert _post_webhook(client, _lava_webhook(inv.id, "c-s-sbp")).status_code == 200
    assert alerts == []

    resp = _post_webhook(client, _lava_webhook(inv.id, "c-s-card"))
    assert resp.status_code == 200, resp.text
    assert alerts == ["payment_double_paid"]
    db_session.expire_all()
    assert db_session.get(models.Payment, card_row.id).status == models.PaymentStatus.paid


def test_webhook_without_contract_id_falls_back_to_last_pending(client, db_session, lava_env, alerts):
    """Фолбэк «последняя pending» остался только для события БЕЗ contractId:
    тогда различить строки нечем, и берём последнюю pending семейства."""
    user = make_user(db_session, telegram_id="lava-split-nocontract")
    inv = _topup_invoice(db_session, user, amount=100.0)
    older = _pending_payment(db_session, inv, provider="lava_top", external_id="c-n-card")
    newer = _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-n-sbp")

    payload = json.loads(_lava_webhook(inv.id, "unused"))
    del payload["contractId"]
    resp = _post_webhook(client, json.dumps(payload).encode())
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.paid
    assert db_session.get(models.Payment, newer.id).status == models.PaymentStatus.paid
    assert db_session.get(models.Payment, older.id).status == models.PaymentStatus.pending
    assert alerts == []


def test_webhook_secret_is_shared_and_still_enforced(client, db_session, lava_env):
    """Секрет один на оба имени, и он по-прежнему обязателен: неверный
    X-Api-Key → 401, счёт не тронут (вебхук — единственная неаутентифицированная
    точка, через которую можно пометить чужой счёт оплаченным)."""
    user = make_user(db_session, telegram_id="lava-split-secret")
    inv = _topup_invoice(db_session, user, amount=100.0)
    _pending_payment(db_session, inv, provider="lava_top_sbp", external_id="c-bad")

    resp = _post_webhook(client, _lava_webhook(inv.id, "c-bad"), secret="nope")
    assert resp.status_code == 401
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.pending

    # Тот же секрет принимает и провайдер под СБП-именем (если бы вебхук был
    # настроен на /webhook/lava_top_sbp — ничего бы не сломалось).
    resp = client.post(
        "/api/payments/webhook/lava_top_sbp",
        content=_lava_webhook(inv.id, "c-bad"),
        headers={"X-Api-Key": WEBHOOK_SECRET, "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(models.Invoice, inv.id).status == models.InvoiceStatus.paid


def test_bot_labels_sbp_and_card_as_two_buttons(monkeypatch):
    """В боте СБП и карта — две разные подписи; ни одна не обещает «карта /
    СБП» разом (той кнопки больше нет)."""
    from tests._bot_stubs import Btn, bot_sources_available, install_bot_stubs

    if not bot_sources_available():
        pytest.skip("bot/handlers.py недоступен в этом образе")
    handlers = install_bot_stubs(monkeypatch)

    assert handlers._provider_label("lava_top_sbp") == "🏦 СБП"
    assert handlers._provider_label("lava_top") == "💳 Карта РФ"
    assert "СБП" not in handlers._provider_label("lava_top")
    assert "/" not in handlers._provider_label("lava_top")

    # Ряды кнопок выбора способа: порядок из PAYMENT_PROVIDER_CHOICES, имя
    # провайдера уходит в callback verbatim (checkout по нему пинит имя).
    monkeypatch.setattr(handlers.types, "InlineKeyboardButton", Btn)
    monkeypatch.setattr(
        handlers, "PAYMENT_PROVIDER_CHOICES", ["telegram_stars", "lava_top_sbp", "lava_top"]
    )
    rows = handlers._payment_method_rows(7, "ren")
    buttons = [b for row in rows for b in row]
    assert [b.text for b in buttons] == ["⭐ Telegram Stars", "🏦 СБП", "💳 Карта РФ"]
    assert [b.callback_data for b in buttons] == [
        "payvia:ren:7:telegram_stars", "payvia:ren:7:lava_top_sbp", "payvia:ren:7:lava_top",
    ]
