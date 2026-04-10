"""Stage 9a (provider rotation) + 9c (generic SBP adapter) tests."""
from __future__ import annotations

import hashlib
import hmac
import json
import random

import pytest

from app.services.payments.base import (
    ProviderError,
    get_provider,
    list_available_providers,
    pick_provider_name,
)
from app.services.payments.generic_sbp import GenericSBPProvider, load_sbp_instance


# ── 9a: rotation ────────────────────────────────────────────────────


def test_list_available_providers_reads_plural_env(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROVIDERS", "cryptobot, yookassa ,telegram_stars")
    monkeypatch.delenv("PAYMENT_PROVIDER", raising=False)
    assert list_available_providers() == ["cryptobot", "yookassa", "telegram_stars"]


def test_list_available_providers_falls_back_to_singular(monkeypatch):
    monkeypatch.delenv("PAYMENT_PROVIDERS", raising=False)
    monkeypatch.setenv("PAYMENT_PROVIDER", "yookassa")
    assert list_available_providers() == ["yookassa"]


def test_list_available_providers_dedupes(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROVIDERS", "cryptobot,cryptobot,yookassa")
    assert list_available_providers() == ["cryptobot", "yookassa"]


def test_pick_provider_name_deterministic_with_seeded_rng(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROVIDERS", "cryptobot,yookassa,telegram_stars")
    rng = random.Random(0)
    picks = {pick_provider_name(rng) for _ in range(50)}
    # All three names appear at least once across 50 picks → rotation works.
    assert picks == {"cryptobot", "yookassa", "telegram_stars"}


def test_pick_provider_name_single_pool_no_rng(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROVIDERS", "cryptobot")
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "tok")
    assert pick_provider_name() == "cryptobot"


def test_get_provider_with_no_name_picks_from_pool(monkeypatch):
    monkeypatch.setenv("PAYMENT_PROVIDERS", "cryptobot")
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "tok")
    prov = get_provider(None)
    assert prov.name == "cryptobot"


# ── 9c: GenericSBPProvider ──────────────────────────────────────────


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_generic_sbp_template_create_invoice():
    prov = GenericSBPProvider(
        slug="acme",
        hmac_secret="topsecret",
        pay_url_template="https://pay.acme.example/{invoice_id}?amt={amount}",
        display_name="sbp:acme",
    )
    inv = prov.create_invoice(invoice_id=77, amount=150.0, currency="RUB")
    assert inv.pay_url == "https://pay.acme.example/77?amt=150.00"
    assert inv.external_id == "77"


def test_generic_sbp_webhook_paid_happy_path():
    prov = GenericSBPProvider(
        slug="acme",
        hmac_secret="topsecret",
        pay_url_template="https://pay.acme.example/{invoice_id}",
        display_name="sbp:acme",
    )
    body = json.dumps(
        {"invoice_id": 77, "status": "paid", "amount": 150.0, "currency": "RUB"}
    ).encode("utf-8")
    sig = _sign("topsecret", body)

    evt = prov.verify_webhook(body, {"X-Sbp-Signature": sig})
    assert evt.external_id == "77"
    assert evt.status == "paid"
    assert evt.amount == 150.0
    assert evt.currency == "RUB"


def test_generic_sbp_webhook_rejects_bad_signature():
    prov = GenericSBPProvider(
        slug="acme",
        hmac_secret="topsecret",
        pay_url_template="https://x/{invoice_id}",
        display_name="sbp:acme",
    )
    body = json.dumps({"invoice_id": 1, "status": "paid"}).encode("utf-8")
    bad_sig = _sign("WRONG", body)

    with pytest.raises(ProviderError, match="invalid HMAC"):
        prov.verify_webhook(body, {"x-sbp-signature": bad_sig})


def test_generic_sbp_webhook_missing_signature_header():
    prov = GenericSBPProvider(
        slug="acme",
        hmac_secret="topsecret",
        pay_url_template="https://x/{invoice_id}",
        display_name="sbp:acme",
    )
    with pytest.raises(ProviderError, match="missing"):
        prov.verify_webhook(b"{}", {})


def test_generic_sbp_webhook_status_mapping():
    prov = GenericSBPProvider(
        slug="acme",
        hmac_secret="s",
        pay_url_template="https://x/{invoice_id}",
        display_name="sbp:acme",
    )
    for raw, expected in [
        ("paid", "paid"),
        ("success", "paid"),
        ("declined", "failed"),
        ("expired", "expired"),
        ("pending", "other"),
    ]:
        body = json.dumps({"invoice_id": 1, "status": raw}).encode("utf-8")
        sig = _sign("s", body)
        evt = prov.verify_webhook(body, {"x-sbp-signature": sig})
        assert evt.status == expected, f"{raw} → {evt.status}"


def test_load_sbp_instance_reads_env(monkeypatch):
    monkeypatch.setenv("SBP_ACME_HMAC_SECRET", "topsecret")
    monkeypatch.setenv(
        "SBP_ACME_PAY_URL_TEMPLATE", "https://pay.acme.example/{invoice_id}"
    )
    monkeypatch.setenv("SBP_ACME_DISPLAY_NAME", "ACME SBP")
    cfg = load_sbp_instance("acme")
    assert cfg["hmac_secret"] == "topsecret"
    assert cfg["display_name"] == "ACME SBP"
    assert cfg["pay_url_template"] == "https://pay.acme.example/{invoice_id}"


def test_load_sbp_instance_requires_secret(monkeypatch):
    monkeypatch.delenv("SBP_GHOST_HMAC_SECRET", raising=False)
    with pytest.raises(ProviderError, match="HMAC_SECRET"):
        load_sbp_instance("ghost")


def test_load_sbp_instance_requires_url_or_template(monkeypatch):
    monkeypatch.setenv("SBP_GHOST_HMAC_SECRET", "s")
    monkeypatch.delenv("SBP_GHOST_PAY_URL_TEMPLATE", raising=False)
    monkeypatch.delenv("SBP_GHOST_CREATE_URL", raising=False)
    with pytest.raises(ProviderError, match="PAY_URL_TEMPLATE"):
        load_sbp_instance("ghost")


def test_get_provider_dispatches_sbp_slug(monkeypatch):
    monkeypatch.setenv("SBP_ACME_HMAC_SECRET", "topsecret")
    monkeypatch.setenv(
        "SBP_ACME_PAY_URL_TEMPLATE", "https://pay.acme.example/{invoice_id}"
    )
    prov = get_provider("sbp:acme")
    assert isinstance(prov, GenericSBPProvider)
    assert prov.name == "sbp:acme"
