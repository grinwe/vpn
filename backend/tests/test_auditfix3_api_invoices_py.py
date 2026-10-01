"""Аудит-волна 3 — валидация пагинации list_invoices (находка #45).

Ранее ``limit`` был голым ``int = 10`` без Query(ge/le): limit=-1 уезжал в
SQL как ``LIMIT -1`` (Postgres → 500), limit=10**9 выгружал всю таблицу.
Теперь limit ограничен ``Query(ge=1, le=200)``, добавлен offset и eager-load.
"""
from __future__ import annotations

from app import models

from .factories import make_plan, make_user


def _make_invoice(db, user, plan) -> int:
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        amount=plan.price,
        currency="RUB",
    )
    db.add(invoice)
    db.commit()
    return invoice.id


def test_list_invoices_rejects_negative_limit(client):
    # limit=-1 раньше давал 500 из Postgres, теперь 422 от валидатора.
    resp = client.get("/api/invoices", params={"limit": -1})
    assert resp.status_code == 422


def test_list_invoices_rejects_oversized_limit(client):
    # limit выше потолка le=200 отсекается валидатором, а не выгружает всё.
    resp = client.get("/api/invoices", params={"limit": 10**9})
    assert resp.status_code == 422


def test_list_invoices_rejects_negative_offset(client):
    resp = client.get("/api/invoices", params={"offset": -1})
    assert resp.status_code == 422


def test_list_invoices_limit_and_offset_paginate(client, db_session):
    plan = make_plan(db_session)
    user = make_user(db_session)
    ids = [_make_invoice(db_session, user, plan) for _ in range(3)]

    # limit=2 → две самые свежие (порядок по created_at desc).
    resp = client.get("/api/invoices", params={"limit": 2})
    assert resp.status_code == 200, resp.text
    first_page = resp.json()
    assert len(first_page) == 2

    # offset=2 сдвигает к оставшемуся хвосту.
    resp = client.get("/api/invoices", params={"limit": 2, "offset": 2})
    assert resp.status_code == 200, resp.text
    second_page = resp.json()
    assert len(second_page) == 1

    seen = {row["id"] for row in first_page} | {row["id"] for row in second_page}
    assert seen == set(ids)


def test_list_invoices_default_limit_ok(client, db_session):
    plan = make_plan(db_session)
    user = make_user(db_session)
    _make_invoice(db_session, user, plan)

    resp = client.get("/api/invoices")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) >= 1
