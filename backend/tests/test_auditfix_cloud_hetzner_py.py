"""Аудит-фиксы #82/#83 для Hetzner-драйвера (mocked HTTP).

#82 — транзиентный сбой поллинга (сеть/429/5xx) не обрывает ожидание running;
      при таймауте последняя ошибка поллинга попадает в сообщение DriverError.
#83 — orphan-guard: блокирующий create_server при провале после успешного POST
      best-effort сносит оплаченный сервер; сплит order_server/wait_for_ipv4
      позволяет node_spawner фиксировать external_id в БД до поллинга.
"""
from __future__ import annotations

import pytest

from app.services.cloud import hetzner as hz
from app.services.cloud.base import DriverError
from app.services.cloud.hetzner import HetznerDriver


class _Resp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _path(url: str) -> str:
    return url.split("/v1", 1)[1] if "/v1" in url else url


def _running_server(server_id: int = 42, ipv4: str | None = "1.2.3.4") -> dict:
    public_net: dict = {"ipv6": {"ip": "2001:db8::1"}}
    if ipv4:
        public_net["ipv4"] = {"ip": ipv4}
    return {
        "id": server_id,
        "status": "running",
        "public_net": public_net,
        "server_type": {"prices": [{"price_monthly": {"gross": "4.51"}}]},
    }


def _driver(handler) -> HetznerDriver:
    d = HetznerDriver(token="tok")
    d._session.request = handler  # type: ignore[assignment]
    return d


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    """Без реальных пауз между поллами и с коротким дедлайном."""
    monkeypatch.setattr(hz.time, "sleep", lambda _s: None)
    monkeypatch.setattr(hz, "POLL_TIMEOUT", 0.2)


# ---------- #82: устойчивость поллинга ----------

def test_wait_running_survives_transient_poll_error():
    calls = {"n": 0}

    def handler(method, url, timeout=None, **kwargs):
        assert method == "GET"
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp({"error": "bad gateway"}, status_code=502)
        return _Resp({"server": _running_server()})

    d = _driver(handler)
    server = d._wait_running(42)
    assert server["status"] == "running"
    assert calls["n"] == 2  # первый сбой пережит, второй полл успешен


def test_wait_running_timeout_includes_last_poll_error():
    def handler(method, url, timeout=None, **kwargs):
        return _Resp({"error": "rate limited"}, status_code=429)

    d = _driver(handler)
    with pytest.raises(DriverError) as exc:
        d._wait_running(42)
    assert "last poll error" in str(exc.value)
    assert "429" in str(exc.value)


# ---------- #83: сплит order_server / wait_for_ipv4 ----------

def test_order_server_returns_id_and_root_password():
    def handler(method, url, timeout=None, json=None, **kwargs):
        assert (method, _path(url)) == ("POST", "/servers")
        assert json["server_type"] == "cx22"
        return _Resp({"server": {"id": 42}, "root_password": "s3cret"})

    d = _driver(handler)
    assert d.order_server(
        name="n1", region="fsn1", plan="cx22", image="ubuntu-22.04"
    ) == ("42", "s3cret")


def test_wait_for_ipv4_returns_ip_cost_raw():
    def handler(method, url, timeout=None, **kwargs):
        return _Resp({"server": _running_server()})

    d = _driver(handler)
    ipv4, cost, raw = d.wait_for_ipv4("42")
    assert ipv4 == "1.2.3.4"
    assert cost == pytest.approx(4.51)
    assert raw["status"] == "running"


# ---------- #83: orphan-guard в блокирующем create_server ----------

def test_create_server_destroys_orphan_on_poll_timeout():
    deleted: list[str] = []

    def handler(method, url, timeout=None, json=None, **kwargs):
        p = _path(url)
        if method == "POST" and p == "/servers":
            return _Resp({"server": {"id": 42}})
        if method == "GET":
            return _Resp({"server": {"id": 42, "status": "starting"}})
        if method == "DELETE":
            deleted.append(p)
            return _Resp(None, status_code=204)
        raise AssertionError(f"unexpected {method} {p}")

    d = _driver(handler)
    with pytest.raises(DriverError, match="did not reach running"):
        d.create_server(name="n1", region="fsn1", plan="cx22", image="ubuntu-22.04")
    assert deleted == ["/servers/42"]  # оплаченный сервер снесён, не сирота


def test_create_server_destroys_orphan_when_no_ipv4():
    deleted: list[str] = []

    def handler(method, url, timeout=None, json=None, **kwargs):
        p = _path(url)
        if method == "POST" and p == "/servers":
            return _Resp({"server": {"id": 42}})
        if method == "GET":
            return _Resp({"server": _running_server(ipv4=None)})
        if method == "DELETE":
            deleted.append(p)
            return _Resp(None, status_code=204)
        raise AssertionError(f"unexpected {method} {p}")

    d = _driver(handler)
    with pytest.raises(DriverError, match="no public IPv4"):
        d.create_server(name="n1", region="fsn1", plan="cx22", image="ubuntu-22.04")
    assert deleted == ["/servers/42"]


def test_orphan_guard_failure_does_not_mask_original_error():
    def handler(method, url, timeout=None, json=None, **kwargs):
        p = _path(url)
        if method == "POST" and p == "/servers":
            return _Resp({"server": {"id": 42}})
        if method == "GET":
            return _Resp({"server": {"id": 42, "status": "starting"}})
        if method == "DELETE":
            return _Resp({"error": "boom"}, status_code=500)
        raise AssertionError(f"unexpected {method} {p}")

    d = _driver(handler)
    # снос не удался → исходная ошибка поллинга всё равно наружу (не 500 от DELETE)
    with pytest.raises(DriverError, match="did not reach running"):
        d.create_server(name="n1", region="fsn1", plan="cx22", image="ubuntu-22.04")


def test_create_server_success_returns_cloudserver():
    def handler(method, url, timeout=None, json=None, **kwargs):
        p = _path(url)
        if method == "POST" and p == "/servers":
            return _Resp({"server": {"id": 42}, "root_password": None})
        if method == "GET":
            return _Resp({"server": _running_server()})
        raise AssertionError(f"unexpected {method} {p}")

    d = _driver(handler)
    srv = d.create_server(name="n1", region="fsn1", plan="cx22", image="ubuntu-22.04")
    assert (srv.external_id, srv.ipv4, srv.ipv6) == ("42", "1.2.3.4", "2001:db8::1")
    assert srv.monthly_cost == pytest.approx(4.51)
    assert srv.root_password is None  # ssh-ключ инжектится → пароля нет
