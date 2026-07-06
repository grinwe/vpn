"""Unit tests for the VDSina cloud driver (mocked HTTP).

Гоняем драйвер против фейкового ``_session.request`` — конверт
``{status,status_msg,data}``, инжект/авто-регистрация ssh-ключа, извлечение IPv4
из МАССИВА ``ip``, валидация числовых id, баланс, lifecycle (create/reinstall/
destroy/autoprolong), offerings-degrade.
"""
from __future__ import annotations

import pytest

from app.services.cloud.base import DriverError
from app.services.cloud.vdsina import VdsinaDriver, _extract_ipv4


class _Resp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok(data):
    return {"status": "ok", "status_msg": "", "data": data}


def _err(msg):
    return {"status": "error", "status_msg": msg, "data": None}


def _driver(handler, ssh_key_ids=None) -> VdsinaDriver:
    d = VdsinaDriver(token="tok", ssh_key_ids=ssh_key_ids)
    d._session.request = handler  # type: ignore[assignment]
    return d


def _path(url: str) -> str:
    return url.split("/v1", 1)[1] if "/v1" in url else url


def test_auth_header_is_bare_token():
    d = VdsinaDriver(token="  abc123  ")
    # ГОЛЫЙ токен, без "Bearer" (офиц. дока; terraform-провайдер ошибочно Bearer)
    assert d._session.headers["Authorization"] == "abc123"


def test_extract_ipv4_array_object_and_ipv6_only():
    arr = {"ip": [{"ip": "1.2.3.4", "type": "ipv4"}, {"ip": "2001:db8::1", "type": "ipv6"}]}
    assert _extract_ipv4(arr) == "1.2.3.4"
    assert _extract_ipv4({"ip": {"ip": "5.6.7.8"}}) == "5.6.7.8"
    assert _extract_ipv4({"ip": [{"ip": "2001:db8::1", "type": "ipv6"}]}) == ""  # ipv6-only
    assert _extract_ipv4({"ip": []}) == ""
    assert _extract_ipv4({}) == ""


def test_order_server_injects_explicit_ssh_key():
    captured: list = []

    def handler(method, url, **kwargs):
        captured.append((method, _path(url), kwargs.get("json")))
        if method == "POST" and _path(url) == "/server":
            return _Resp(_ok({"id": 777}))
        raise AssertionError(f"unexpected {method} {url}")

    d = _driver(handler, ssh_key_ids=["42"])
    sid, pw = d.order_server(name="ru-x", region="1", plan="10", image="20")
    assert sid == "777"
    assert pw == ""  # бокс с ключом → пароль не нужен
    method, path, body = captured[0]
    assert (method, path) == ("POST", "/server")
    assert body == {
        "datacenter": 1, "server-plan": 10, "template": 20,
        # host — валидный FQDN (VDSina требует домен), name — свободный лейбл;
        # ip4 — обязательное кол-во IPv4 (без него POST /server = Validation Error).
        "name": "ru-x", "host": "ru-x.example.com", "ssh-key": 42, "ip4": 1,
    }


def test_order_server_autoregisters_provisioning_key(monkeypatch):
    # нет явного ключа → авто-регистрируем наш provisioning-pubkey на VDSina.
    monkeypatch.setattr(
        "app.services.ssh_bootstrap.provisioning_pubkey",
        lambda: "ssh-ed25519 AAAAC3pub",
    )
    captured: list = []

    def handler(method, url, **kwargs):
        p = _path(url)
        captured.append((method, p, kwargs.get("json")))
        if method == "GET" and p == "/ssh-key":
            return _Resp(_ok([]))  # ключей ещё нет
        if method == "POST" and p == "/ssh-key":
            return _Resp(_ok({"id": 55}))
        if method == "POST" and p == "/server":
            return _Resp(_ok({"id": 9}))
        raise AssertionError(f"unexpected {method} {url}")

    d = _driver(handler)  # без ssh_key_ids
    sid, pw = d.order_server(name="n", region="1", plan="10", image="20")
    assert sid == "9" and pw == ""
    # создали ключ и заинжектили его id в заказ
    assert ("POST", "/ssh-key", {"name": "vpn-provisioning", "data": "ssh-ed25519 AAAAC3pub"}) in captured
    server_body = next(b for m, p, b in captured if (m, p) == ("POST", "/server"))
    assert server_body["ssh-key"] == 55


def test_ensure_key_reuses_existing_no_duplicate(monkeypatch):
    # GET /ssh-key уже содержит наш ключ по имени → переиспользуем, НЕ создаём дубль.
    monkeypatch.setattr(
        "app.services.ssh_bootstrap.provisioning_pubkey",
        lambda: "ssh-ed25519 AAAAC3pub",
    )
    captured: list = []

    def handler(method, url, **kwargs):
        p = _path(url)
        captured.append((method, p, kwargs.get("json")))
        if method == "GET" and p == "/ssh-key":
            return _Resp(_ok([{"id": 55, "name": "vpn-provisioning"}]))
        if method == "POST" and p == "/server":
            return _Resp(_ok({"id": 9}))
        raise AssertionError(f"unexpected {method} {url}")

    d = _driver(handler)
    sid, _ = d.order_server(name="n", region="1", plan="10", image="20")
    assert sid == "9"
    assert not any(m == "POST" and p == "/ssh-key" for m, p, _ in captured)  # без дубля
    server_body = next(b for m, p, b in captured if (m, p) == ("POST", "/server"))
    assert server_body["ssh-key"] == 55


def test_order_server_raises_without_key_or_pubkey(monkeypatch):
    monkeypatch.setattr("app.services.ssh_bootstrap.provisioning_pubkey", lambda: None)

    def handler(method, url, **kwargs):
        raise AssertionError("should not order without a key")

    d = _driver(handler)
    with pytest.raises(DriverError, match="ssh-ключ"):
        d.order_server(name="n", region="1", plan="10", image="20")


def test_order_server_rejects_nonnumeric_id():
    def handler(method, url, **kwargs):
        raise AssertionError("should fast-fail before any HTTP call")

    d = _driver(handler, ssh_key_ids=["42"])
    # дефолтный image '"ubuntu-22.04"' (строка) — НЕ числовой template id
    with pytest.raises(DriverError, match="template"):
        d.order_server(name="n", region="1", plan="10", image="ubuntu-22.04")


def test_create_server_full_path(monkeypatch):
    monkeypatch.setattr("app.services.cloud.vdsina.time.sleep", lambda *_: None)
    states = iter([
        _ok({"status": "new", "ip": []}),
        _ok({"status": "active", "ip": [{"ip": "203.0.113.5", "type": "ipv4"}]}),
    ])

    def handler(method, url, **kwargs):
        p = _path(url)
        if method == "POST" and p == "/server":
            return _Resp(_ok({"id": 9}))
        if method == "GET" and p == "/server/9":
            return _Resp(next(states))
        raise AssertionError(f"unexpected {method} {url}")

    d = _driver(handler, ssh_key_ids=["42"])
    srv = d.create_server(name="n", region="1", plan="10", image="20")
    assert srv.external_id == "9"
    assert srv.ipv4 == "203.0.113.5"
    assert srv.root_password is None  # ключ инжектится → пароля нет


def test_reinstall_server_body_reinjects_key():
    captured: list = []

    def handler(method, url, **kwargs):
        captured.append((method, _path(url), kwargs.get("json")))
        return _Resp(_ok(None))

    d = _driver(handler, ssh_key_ids=["7"])
    d.reinstall_server("9", "20")
    assert captured == [("PUT", "/server.reinstall/9", {"template": 20, "ssh-key": 7})]


def test_destroy_and_autoprolong_bodies():
    captured: list = []

    def handler(method, url, **kwargs):
        captured.append((method, _path(url), kwargs.get("json")))
        return _Resp(_ok(None))

    d = _driver(handler, ssh_key_ids=["7"])
    d.destroy_server("9")
    d.set_autoprolong("9", True)
    assert ("DELETE", "/server/9", None) in captured
    # autoprolong — СТРОКА "1"/"0", не bool
    assert ("PUT", "/server/9", {"autoprolong": "1"}) in captured


def test_wait_for_ipv4_timeout_raises(monkeypatch):
    # дедлайн в прошлом → цикл не входит → IP пуст → raise (без патча time.time)
    monkeypatch.setattr("app.services.cloud.vdsina._POLL_TIMEOUT", -1)
    d = _driver(lambda *a, **k: _Resp(_ok({"status": "new", "ip": []})), ssh_key_ids=["7"])
    with pytest.raises(DriverError, match="no IPv4"):
        d.wait_for_ipv4("9")


def test_list_datacenters_degrades_on_error():
    d_ok = _driver(lambda *a, **k: _Resp(_ok(
        [{"id": 1, "name": "Moscow", "country": "ru", "active": True}])))
    assert d_ok.list_datacenters()[0]["id"] == 1

    # ошибка/пустой список → [] (форма дегрейдит, деньги не теряем)
    d_err = _driver(lambda *a, **k: _Resp(_err("No DataCenter information")))
    assert d_err.list_datacenters() == []


def test_list_plans_dedupes_and_maps_specs():
    def handler(method, url, **kwargs):
        p = _path(url)
        if p == "/server-group":
            return _Resp(_ok([{"id": 1}]))
        if p == "/server-plan/1":
            return _Resp(_ok([{
                "id": 10, "name": "VDS-1", "cost": "5", "full_cost": "150",
                "period": "day",
                "data": {"cpu": {"value": 1}, "ram": {"value": "1 GB"},
                         "disk": {"value": "10 GB"}},
            }]))
        raise AssertionError(f"unexpected {url}")

    d = _driver(handler)
    plans = d.list_plans()
    assert len(plans) == 1
    p = plans[0]
    assert p["id"] == 10 and p["name"] == "VDS-1"
    assert p["price"] == 5.0 and p["full_price"] == 150.0
    assert p["period"] == "day"
    assert p["cpu"] == 1 and p["ram"] == "1 GB" and p["disk"] == "10 GB"


def test_get_balance_reads_real_and_preserves_zero():
    d = _driver(lambda *a, **k: _Resp(_ok({"real": "123.45", "bonus": "0"})))
    assert d.get_balance() == 123.45
    # ноль НЕ должен схлопнуться в None (иначе low-balance алерт молчит)
    d0 = _driver(lambda *a, **k: _Resp(_ok({"real": "0.00"})))
    assert d0.get_balance() == 0.0


def test_error_envelope_raises_on_money_path():
    d = _driver(lambda *a, **k: _Resp(_err("Invalid token")))
    with pytest.raises(DriverError, match="Invalid token"):
        d.get_balance()


def test_base_defaults_com_and_ru_override():
    """vdsina = .com (драйверный дефолт), vdsina_ru → base=.ru (get_driver)."""
    from app.services.cloud.vdsina import _BASE

    # дефолт (kind=vdsina) — модульный _BASE (.com, если не переопределён env)
    assert VdsinaDriver(token="t")._base == _BASE
    # явный base (kind=vdsina_ru из get_driver) — .ru-инсталляция
    assert (
        VdsinaDriver(token="t", base="https://userapi.vdsina.ru/v1")._base
        == "https://userapi.vdsina.ru/v1"
    )
    # трейлинг-слеш срезается (склейка f"{base}{path}" не должна давать //)
    assert VdsinaDriver(token="t", base="https://x/v1/")._base == "https://x/v1"
