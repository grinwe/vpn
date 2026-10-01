"""Ретрай транзиентных сбоев HTTP-слоя Hetzner-драйвера (net-audit находка #3).

Проверяем, что 429/5xx/сетевые таймауты на идемпотентных запросах ретраятся
с уважением Retry-After, а неидемпотентный POST-заказ ретраится только на 429
(без риска задвоить оплату на 5xx/таймауте).
"""
from __future__ import annotations

import types

import pytest
import requests

from app.services.cloud import hetzner
from app.services.cloud.base import DriverError


class _Resp:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._json


def _driver_with_responses(responses):
    """Драйвер, чей session.request поочерёдно отдаёт элементы responses.
    Элемент = либо _Resp, либо исключение (будет брошено)."""
    drv = hetzner.HetznerDriver(token="x")
    calls = {"n": 0}

    def fake_request(method, url, timeout=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        item = responses[i]
        if isinstance(item, Exception):
            raise item
        return item

    drv._session = types.SimpleNamespace(request=fake_request, headers={})
    drv._calls = calls
    return drv


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(hetzner.time, "sleep", lambda *_: None)


def test_get_retries_on_429_then_succeeds():
    drv = _driver_with_responses([
        _Resp(429, headers={"Retry-After": "0"}),
        _Resp(200, {"ok": True}),
    ])
    assert drv._get("/locations") == {"ok": True}
    assert drv._calls["n"] == 2


def test_get_retries_on_5xx():
    drv = _driver_with_responses([
        _Resp(503),
        _Resp(500),
        _Resp(200, {"ok": 1}),
    ])
    assert drv._get("/servers/1") == {"ok": 1}
    assert drv._calls["n"] == 3


def test_get_retries_on_network_error():
    drv = _driver_with_responses([
        requests.ConnectTimeout("boom"),
        _Resp(200, {"ok": 1}),
    ])
    assert drv._get("/servers/1") == {"ok": 1}
    assert drv._calls["n"] == 2


def test_get_exhausts_retries_and_raises():
    drv = _driver_with_responses([_Resp(429)] * (hetzner.HTTP_MAX_RETRIES + 1))
    with pytest.raises(DriverError):
        drv._get("/locations")
    assert drv._calls["n"] == hetzner.HTTP_MAX_RETRIES + 1


def test_post_retries_on_429_only():
    # 429 до создания сервера — безопасно ретраить.
    drv = _driver_with_responses([
        _Resp(429, headers={"Retry-After": "0"}),
        _Resp(201, {"server": {"id": 7}}),
    ])
    assert drv._post("/servers", {}) == {"server": {"id": 7}}
    assert drv._calls["n"] == 2


def test_post_does_not_retry_on_5xx():
    # 5xx на POST-заказе НЕ ретраим (сервер мог успеть создаться → задвоение).
    drv = _driver_with_responses([_Resp(500), _Resp(201, {"server": {"id": 7}})])
    with pytest.raises(DriverError):
        drv._post("/servers", {})
    assert drv._calls["n"] == 1


def test_post_does_not_retry_on_network_error():
    drv = _driver_with_responses([
        requests.ConnectTimeout("boom"),
        _Resp(201, {"server": {"id": 7}}),
    ])
    with pytest.raises(DriverError):
        drv._post("/servers", {})
    assert drv._calls["n"] == 1


def test_retry_after_header_respected(monkeypatch):
    captured = []
    monkeypatch.setattr(hetzner.time, "sleep", lambda s: captured.append(s))
    drv = _driver_with_responses([
        _Resp(429, headers={"Retry-After": "5"}),
        _Resp(200, {"ok": 1}),
    ])
    drv._get("/locations")
    assert captured == [5.0]
