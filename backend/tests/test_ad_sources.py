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


# ── AdLink (управляемые рекламные ссылки) ──


def test_ad_link_crud_and_stats(client, db_session: Session) -> None:
    r = client.post("/api/admin/ad-links", json={"name": "Блогер Вася", "tag": "tg_vasya"})
    assert r.status_code == 200, r.text
    link = r.json()
    assert link["tag"] == "tg_vasya"
    assert link["is_active"] is True
    assert link["started"] == 0
    lid = link["id"]

    # дубликат метки → 409; невалидная/служебная → 400
    assert client.post("/api/admin/ad-links", json={"name": "x", "tag": "tg_vasya"}).status_code == 409
    assert client.post("/api/admin/ad-links", json={"name": "x", "tag": "bad tag!!"}).status_code == 400
    assert client.post("/api/admin/ad-links", json={"name": "x", "tag": "ref_x"}).status_code == 400

    # юзер с этой меткой + триал + topup → статистика подтянулась
    u = models.User(telegram_id="al-1", source="tg_vasya", trial_activated_at=utcnow())
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    db_session.add(models.BalanceTransaction(
        user_id=u.id, amount_kopecks=10000, kind=models.BalanceTxKind.topup, reference="al-t",
    ))
    db_session.commit()

    row = next(x for x in client.get("/api/admin/ad-links").json() if x["id"] == lid)
    assert row["started"] == 1 and row["trial"] == 1 and row["paid"] == 1
    assert row["revenue_kopecks"] == 10000

    # выключаем → новый заход по этой метке НЕ атрибутируется
    assert client.patch(f"/api/admin/ad-links/{lid}", json={"is_active": False}).status_code == 200
    client.post("/api/users/register", json={"telegram_id": "al-2", "source": "tg_vasya"})
    u2 = db_session.query(models.User).filter_by(telegram_id="al-2").one()
    assert u2.source is None

    assert client.delete(f"/api/admin/ad-links/{lid}").status_code == 204


def test_ad_link_unknown_tag_still_attributes(client, db_session: Session) -> None:
    # ad-hoc метка без управляемой AdLink → атрибутируется (backward compat)
    client.post("/api/users/register", json={"telegram_id": "al-3", "source": "adhoc_promo"})
    u = db_session.query(models.User).filter_by(telegram_id="al-3").one()
    assert u.source == "adhoc_promo"


def test_ad_link_auto_tag_when_blank(client, db_session: Session) -> None:
    r = client.post("/api/admin/ad-links", json={"name": "Без метки"})
    assert r.status_code == 200
    assert r.json()["tag"].startswith("ad_")
