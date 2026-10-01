"""Netfix-регресс: VDSina offerings отличают транзиентный сетевой/HTTP-сбой от
«реально пустого каталога».

Находка аудита (#5): ``list_datacenters/list_plans/list_images`` глушили ЛЮБОЙ
``DriverError`` в пустой список — оператор видел пустой каталог без признака
сбоя. Фикс: ``_call`` поднимает ``TransientDriverError`` на таймауте/429/5xx и
ретраит идемпотентные GET; offerings пробрасывают транзиентный сбой (роут отдаёт
502), а в ``[]`` дегрейдят только бизнес-ошибку («No X information»).
"""
from __future__ import annotations

import pytest
import requests

from app.services.cloud.base import DriverError
from app.services.cloud.vdsina import (
    TransientDriverError,
    VdsinaDriver,
    _parse_retry_after,
)


class _Resp:
    def __init__(self, payload, status_code: int = 200, headers=None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

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


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Ретрай-backoff не должен реально тормозить тесты.
    monkeypatch.setattr("app.services.cloud.vdsina.time.sleep", lambda *_a, **_k: None)


def test_transient_is_subclass_of_driver_error():
    # Подкласс → все существующие ``except DriverError`` ловят как раньше
    # (роут /offerings, ops_tools, ops_execution).
    assert issubclass(TransientDriverError, DriverError)


def test_business_error_still_degrades_to_empty():
    # «Реально пусто» (status:error) → [], как и раньше — деньги/каталог не теряем.
    d = _driver(lambda *a, **k: _Resp(_err("No DataCenter information")))
    assert d.list_datacenters() == []
    assert d.list_images() == []


def test_network_timeout_propagates_from_offerings():
    # Таймаут/обрыв TCP → TransientDriverError пробрасывается (не глушится в []).
    def boom(*_a, **_k):
        raise requests.ConnectTimeout("timed out")

    d = _driver(boom)
    with pytest.raises(TransientDriverError):
        d.list_datacenters()
    with pytest.raises(TransientDriverError):
        d.list_images()


def test_http_503_propagates_from_offerings():
    d = _driver(lambda *a, **k: _Resp(None, status_code=503))
    with pytest.raises(TransientDriverError):
        d.list_datacenters()


def test_idempotent_get_retries_then_succeeds():
    calls = {"n": 0}

    def handler(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:  # первые две — 503, третья — ok
            return _Resp(None, status_code=503)
        return _Resp(_ok([{"id": 1, "name": "Moscow", "country": "ru"}]))

    d = _driver(handler)
    dcs = d.list_datacenters()
    assert dcs and dcs[0]["id"] == 1
    assert calls["n"] == 3  # 1 попытка + 2 ретрая (VDSINA_RETRY_ATTEMPTS=2)


def test_non_idempotent_post_is_not_retried():
    # POST (заказ ssh-ключа) не идемпотентен — повтор задвоил бы операцию.
    calls = {"n": 0}

    def handler(method, url, **kwargs):
        calls["n"] += 1
        return _Resp(None, status_code=503)

    d = _driver(handler)
    with pytest.raises(TransientDriverError):
        d._call("POST", "/ssh-key", {"name": "x", "data": "y"})
    assert calls["n"] == 1  # ровно одна попытка, без ретрая


def test_list_plans_inner_group_transient_propagates():
    # Транзиентный сбой на одной группе не должен давать ЧАСТИЧНЫЙ каталог.
    def handler(method, url, **kwargs):
        if url.endswith("/server-group"):
            return _Resp(_ok([{"id": 1}]))
        return _Resp(None, status_code=502)  # /server-plan/1 падает

    d = _driver(handler)
    with pytest.raises(TransientDriverError):
        d.list_plans()


def test_parse_retry_after_seconds_and_junk():
    assert _parse_retry_after(_Resp(None, headers={"Retry-After": "7"})) == 7.0
    # HTTP-date форму не парсим → None (падаем на экспон. backoff).
    assert _parse_retry_after(_Resp(None, headers={"Retry-After": "Wed, 21 Oct"})) is None
    assert _parse_retry_after(_Resp(None, headers={})) is None
