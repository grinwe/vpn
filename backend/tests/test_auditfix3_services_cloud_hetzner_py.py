"""Auditfix #93 — Hetzner: месячная цена берётся из фактической локации сервера,
а не из произвольного первого элемента prices.
"""
from app.services.cloud.hetzner import _monthly_price


def _server(location: str) -> dict:
    """Мок ответа Hetzner /servers/{id} с ценами по разным локациям."""
    return {
        "datacenter": {"location": {"name": location}},
        "server_type": {
            "prices": [
                {"location": "fsn1", "price_monthly": {"gross": "4.50"}},
                {"location": "ash", "price_monthly": {"gross": "6.30"}},
                {"location": "hil", "price_monthly": {"gross": "6.10"}},
            ]
        },
    }


def test_price_matches_ordered_location_ashburn():
    # Сервер в ashburn — берём его цену, а не первую (fsn1).
    assert _monthly_price(_server("ash")) == 6.30


def test_price_matches_ordered_location_fsn1():
    assert _monthly_price(_server("fsn1")) == 4.50


def test_falls_back_to_first_when_location_unknown():
    # Локация не совпала ни с одной ценой → страховка prices[0].
    assert _monthly_price(_server("nbg1")) == 4.50


def test_none_when_no_prices():
    assert _monthly_price({"server_type": {"prices": []}}) is None
    assert _monthly_price({}) is None


def test_none_when_gross_zero_or_missing():
    srv = {
        "datacenter": {"location": {"name": "fsn1"}},
        "server_type": {"prices": [{"location": "fsn1", "price_monthly": {}}]},
    }
    assert _monthly_price(srv) is None
