"""Tests for Stage 5.5/8 webapp endpoints: /transactions, /referral, /me."""
import os
from app import models
from app.api_webapp import issue_token
from app.config import get_settings
from app.services import balance

from .factories import make_user


def _auth_headers(user_id: int) -> dict:
    settings = get_settings()
    token = issue_token(user_id, settings.webapp_jwt_secret, 600)
    return {"Authorization": f"Bearer {token}"}


def test_transactions_returns_ledger_newest_first(client, db_session):
    user = make_user(db_session, telegram_id="tg-tx")
    balance.topup(db_session, user.id, 5000, reference="seed-1")
    balance.topup(db_session, user.id, 3000, reference="seed-2")
    db_session.commit()

    res = client.get("/api/webapp/transactions", headers=_auth_headers(user.id))
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["has_more"] is False
    assert len(data["items"]) == 2
    # Newest first.
    assert data["items"][0]["reference"] == "seed-2"
    assert data["items"][0]["amount_kopecks"] == 3000
    assert data["items"][1]["reference"] == "seed-1"


def test_transactions_pagination_has_more(client, db_session):
    user = make_user(db_session, telegram_id="tg-page")
    for i in range(5):
        balance.topup(db_session, user.id, 100 + i, reference=f"r{i}")
    db_session.commit()

    res = client.get(
        "/api/webapp/transactions?limit=2&offset=0", headers=_auth_headers(user.id)
    )
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 2
    assert data["has_more"] is True

    res2 = client.get(
        "/api/webapp/transactions?limit=2&offset=4", headers=_auth_headers(user.id)
    )
    data2 = res2.json()
    assert len(data2["items"]) == 1
    assert data2["has_more"] is False


def test_transactions_requires_auth(client):
    res = client.get("/api/webapp/transactions")
    assert res.status_code == 401


def test_referral_mints_code_on_first_call(client, db_session):
    user = make_user(db_session, telegram_id="tg-ref")
    db_session.commit()

    res = client.get("/api/webapp/referral", headers=_auth_headers(user.id))
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["code"] is not None
    assert len(data["code"]) >= 4
    assert data["bonus_kopecks"] == balance.REFERRAL_BONUS_KOPECKS
    assert data["invited_count"] == 0
    assert data["earned_kopecks"] == 0

    # Second call returns the same code, doesn't mint a new one.
    res2 = client.get("/api/webapp/referral", headers=_auth_headers(user.id))
    assert res2.json()["code"] == data["code"]
    assert (
        db_session.query(models.ReferralCode)
        .filter(models.ReferralCode.owner_id == user.id)
        .count()
        == 1
    )


def test_referral_counters_reflect_invitees_and_bonuses(client, db_session):
    owner = make_user(db_session, telegram_id="tg-owner")
    invitee_a = make_user(db_session, telegram_id="tg-a")
    invitee_b = make_user(db_session, telegram_id="tg-b")
    invitee_a.referred_by_id = owner.id
    invitee_b.referred_by_id = owner.id
    db_session.add_all([invitee_a, invitee_b])
    # Two bonus credits to the owner.
    db_session.add(
        models.BalanceTransaction(
            user_id=owner.id,
            amount_kopecks=5000,
            kind=models.BalanceTxKind.bonus,
            reference="ref-a",
        )
    )
    db_session.add(
        models.BalanceTransaction(
            user_id=owner.id,
            amount_kopecks=5000,
            kind=models.BalanceTxKind.bonus,
            reference="ref-b",
        )
    )
    db_session.commit()

    res = client.get("/api/webapp/referral", headers=_auth_headers(owner.id))
    assert res.status_code == 200
    data = res.json()
    assert data["invited_count"] == 2
    assert data["earned_kopecks"] == 10000


# ── Stage 8: sub_link_base_url surfaced via /me ────────────────────


def test_me_returns_sub_link_base_url_from_env(client, db_session, monkeypatch):
    monkeypatch.setenv("SUB_LINK_BASE_URL", "https://c1.cloudfn.app/sub/")
    user = make_user(db_session, telegram_id="tg-stage8")
    db_session.commit()

    res = client.get("/api/webapp/me", headers=_auth_headers(user.id))
    assert res.status_code == 200, res.text
    # Trailing slash must be stripped so the WebApp can safely do
    # `${base}/${token}` without ending up with "//".
    assert res.json()["sub_link_base_url"] == "https://c1.cloudfn.app/sub"


def test_me_returns_empty_sub_link_base_url_when_unset(
    client, db_session, monkeypatch
):
    monkeypatch.delenv("SUB_LINK_BASE_URL", raising=False)
    user = make_user(db_session, telegram_id="tg-stage8-empty")
    db_session.commit()

    res = client.get("/api/webapp/me", headers=_auth_headers(user.id))
    assert res.status_code == 200
    assert res.json()["sub_link_base_url"] == ""
