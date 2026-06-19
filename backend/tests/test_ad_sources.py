"""Ad-source attribution + funnel (docs/operations/ad_source_attribution.md).

Метка рекламного источника из deep-link старт-параметра: first-touch на
``User.source``, воронка started→trial→paid по ней. Тесты гоняют alembic с нуля
(conftest), так что заодно валидируют миграцию 0054.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import models
from app.api_extensions import _clean_source
from app.time_utils import utcnow


def test_clean_source_validation() -> None:
    assert _clean_source("tg_blogger1") == "tg_blogger1"
    assert _clean_source("tt-ceprem_test1") == "tt-ceprem_test1"
    assert _clean_source("  spaced  ") == "spaced"
    assert _clean_source(None) is None
    assert _clean_source("") is None
    assert _clean_source("bad source!!") is None  # пробел/спецсимволы
    assert _clean_source("x" * 65) is None  # > 64
    assert _clean_source("support") is None  # служебный start-ключ, не реклама
    assert _clean_source("ref_abc") is None  # реферал, не метка
    assert _clean_source("Support") is None  # регистронезависимо
    assert _clean_source("REF_abc") is None  # регистронезависимо


def _auth(tg_id: str) -> dict:
    return {"X-Admin-Actor": tg_id}


def test_register_sets_source_first_touch(client, db_session: Session) -> None:
    # Первый /start с меткой → source проставлен.
    r = client.post("/api/users/register", json={"telegram_id": "ad-1", "source": "tg_blogger1"})
    assert r.status_code == 200
    u = db_session.query(models.User).filter_by(telegram_id="ad-1").one()
    assert u.source == "tg_blogger1"

    # Повторный /start с ДРУГОЙ меткой → не перетираем (first-touch).
    client.post("/api/users/register", json={"telegram_id": "ad-1", "source": "other_ad"})
    db_session.refresh(u)
    assert u.source == "tg_blogger1"

    # Кривая метка → не пишем (None).
    client.post("/api/users/register", json={"telegram_id": "ad-2", "source": "bad src!!"})
    u2 = db_session.query(models.User).filter_by(telegram_id="ad-2").one()
    assert u2.source is None


def _user_with_source(db: Session, tg: str, source: str | None, *, trial: bool = False) -> models.User:
    u = models.User(telegram_id=tg, source=source)
    if trial:
        u.trial_activated_at = utcnow()
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _topup(db: Session, user_id: int, amount: int, ref: str) -> None:
    db.add(models.BalanceTransaction(
        user_id=user_id, amount_kopecks=amount,
        kind=models.BalanceTxKind.topup, reference=ref,
    ))
    db.commit()


def test_ad_sources_funnel(client, db_session: Session) -> None:
    # s1: u1 (trial + 2 topup'а), u2 (без триала/оплаты)
    u1 = _user_with_source(db_session, "f-1", "s1", trial=True)
    _topup(db_session, u1.id, 10000, "t-1a")
    _topup(db_session, u1.id, 10000, "t-1b")  # второй topup НЕ должен раздуть started/paid
    _user_with_source(db_session, "f-2", "s1")
    # s2: u3 (trial + 1 topup)
    u3 = _user_with_source(db_session, "f-3", "s2", trial=True)
    _topup(db_session, u3.id, 5000, "t-3")
    # без метки — не попадает в воронку
    _user_with_source(db_session, "f-4", None, trial=True)

    res = client.get("/api/admin/ad-sources")
    assert res.status_code == 200
    data = res.json()
    by = {row["source"]: row for row in data["sources"]}

    assert by["s1"]["started"] == 2  # u1+u2, НЕ 3 (join-fanout по 2 topup'ам u1)
    assert by["s1"]["trial"] == 1
    assert by["s1"]["paid"] == 1  # только u1
    assert by["s1"]["revenue_kopecks"] == 20000  # сумма обоих topup'ов u1

    assert by["s2"]["started"] == 1
    assert by["s2"]["paid"] == 1
    assert by["s2"]["revenue_kopecks"] == 5000

    assert "s1" not in {None} and None not in by  # source=None не в выдаче
    assert data["total_started"] == 3
    assert data["total_paid"] == 2
    assert data["total_revenue_kopecks"] == 25000
