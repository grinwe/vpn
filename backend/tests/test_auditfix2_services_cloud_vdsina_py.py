"""Находка 92: vdsina.wait_for_ipv4 всегда терял monthly_cost (возвращал None),
из-за чего node.monthly_cost у vdsina-нод не заполнялся и cloud-billing занижал
расходы флота. Проверяем, что стоимость извлекается из ответа сервера, а при её
отсутствии подтягивается из offerings по id тарифа."""
from app.services.cloud.vdsina import VdsinaDriver


def _driver() -> VdsinaDriver:
    # Конструктор сетей не трогает (только requests.Session), токен достаточно.
    return VdsinaDriver(token="x")


def test_cost_from_nested_server_plan():
    d = _driver()
    srv = {"id": 1, "server_plan": {"id": 42, "cost": "350"}}
    assert d._server_cost(srv) == 350.0


def test_cost_from_hyphen_key():
    d = _driver()
    srv = {"server-plan": {"id": 7, "cost": "120.5"}}
    assert d._server_cost(srv) == 120.5


def test_cost_from_top_level():
    d = _driver()
    assert d._server_cost({"cost": "99"}) == 99.0


def test_cost_fallback_to_list_plans(monkeypatch):
    d = _driver()
    # В объекте сервера цены нет — только id тарифа; подтягиваем из offerings.
    monkeypatch.setattr(
        d, "list_plans", lambda: [{"id": 42, "price": 250.0}, {"id": 99, "price": 1.0}]
    )
    assert d._server_cost({"server_plan": {"id": 42}}) == 250.0


def test_cost_none_when_unknown():
    d = _driver()
    assert d._server_cost({}) is None


def test_wait_for_ipv4_propagates_cost(monkeypatch):
    d = _driver()
    srv = {"server_plan": {"id": 5, "cost": "500"}}
    monkeypatch.setattr(d, "_wait_active", lambda eid: ("1.2.3.4", "active", srv))
    ipv4, cost, raw = d.wait_for_ipv4("5")
    assert ipv4 == "1.2.3.4"
    assert cost == 500.0
    assert raw is srv
