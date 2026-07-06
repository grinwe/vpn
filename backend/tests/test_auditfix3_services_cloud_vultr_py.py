"""Регрессия на находку #90: Vultr-драйвер не должен подставлять приватный
internal_ip как публичный host ноды и не должен возвращать инстанс до того,
как назначен публичный main_ip."""
from __future__ import annotations

import pytest

from app.services.cloud.base import DriverError
from app.services.cloud.vultr import VultrDriver, _is_public_ipv4


def test_is_public_ipv4_rejects_private_and_placeholder():
    # NB: 45.63.x — реальный маршрутизируемый диапазон Vultr. НЕ берём
    # 203.0.113.x (TEST-NET-3, RFC5737): stdlib ``ipaddress`` помечает
    # документационные диапазоны как ``is_private`` → драйвер их корректно
    # отсекает как непубличные, так что тест-плейсхолдер должен быть настоящим
    # публичным адресом.
    assert _is_public_ipv4("45.63.10.20") is True
    # RFC1918 — приватные VPC-адреса Vultr (internal_ip)
    assert _is_public_ipv4("10.1.2.3") is False
    assert _is_public_ipv4("172.16.0.5") is False
    assert _is_public_ipv4("192.168.1.1") is False
    # плейсхолдеры/мусор
    assert _is_public_ipv4("0.0.0.0") is False
    assert _is_public_ipv4("") is False
    assert _is_public_ipv4(None) is False
    assert _is_public_ipv4("not-an-ip") is False
    assert _is_public_ipv4("127.0.0.1") is False


class _FakeSession:
    """Отдаёт заранее заготовленную последовательность инстанс-снапшотов."""

    def __init__(self, snapshots: list[dict]):
        self._snapshots = snapshots
        self.headers = _HeaderStub()

    def request(self, method, url, **kwargs):  # pragma: no cover - не используется
        raise AssertionError("HTTP should be stubbed at _get/_post level")


class _HeaderStub:
    def update(self, *_a, **_k):
        pass


def _make_driver(monkeypatch, poll_snapshots: list[dict]):
    driver = VultrDriver(token="x")
    # _post -> create возвращает инстанс с id
    monkeypatch.setattr(
        driver, "_post", lambda path, body: {"instance": {"id": "inst-1"}}
    )
    seq = iter(poll_snapshots)

    def fake_get(path):
        try:
            snap = next(seq)
        except StopIteration:
            snap = poll_snapshots[-1]
        return {"instance": snap}

    monkeypatch.setattr(driver, "_get", fake_get)
    return driver


def test_create_server_ignores_internal_ip(monkeypatch):
    # main_ip пуст, но есть приватный internal_ip — драйвер НЕ должен его
    # вернуть, а должен уйти в таймаут ожидания public main_ip.
    monkeypatch.setattr("app.services.cloud.vultr.POLL_TIMEOUT", 0.05)
    monkeypatch.setattr("app.services.cloud.vultr.time.sleep", lambda *_: None)
    snap = {
        "id": "inst-1",
        "status": "active",
        "server_status": "ok",
        "main_ip": "",
        "internal_ip": "10.7.7.7",
    }
    driver = _make_driver(monkeypatch, [snap])
    with pytest.raises(DriverError):
        driver.create_server(
            name="n", region="ams", plan="p", image="1234"
        )


def test_create_server_waits_for_public_main_ip(monkeypatch):
    monkeypatch.setattr("app.services.cloud.vultr.POLL_TIMEOUT", 5)
    monkeypatch.setattr("app.services.cloud.vultr.time.sleep", lambda *_: None)
    snapshots = [
        # шаг 1: active, но main_ip ещё 0.0.0.0
        {
            "id": "inst-1",
            "status": "active",
            "server_status": "ok",
            "main_ip": "0.0.0.0",
            "internal_ip": "10.1.1.1",
        },
        # шаг 2: назначен публичный main_ip (реальный Vultr-диапазон 45.63.x —
        # не документационный 203.0.113.x, который stdlib считает приватным)
        {
            "id": "inst-1",
            "status": "active",
            "server_status": "ok",
            "main_ip": "45.63.10.20",
            "internal_ip": "10.1.1.1",
            "v6_main_ip": "2001:db8::1",
            "plan_price_monthly": "5.0",
        },
    ]
    driver = _make_driver(monkeypatch, snapshots)
    srv = driver.create_server(name="n", region="ams", plan="p", image="1234")
    assert srv.ipv4 == "45.63.10.20"
    assert srv.external_id == "inst-1"
    assert srv.monthly_cost == 5.0
