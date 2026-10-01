"""Audit-fix (волна 2) tests для BILLmanager-драйвера — находки #84 и #85.

#84: timeout ровно на денежном vds.order.param не должен терять оплаченную услугу —
     после сетевого обрыва делаем сверку с панелью и подхватываем/фейлимся явно.
#85: старая снесённая услуга-тёзка (тот же domain) не должна перехватывать
     _wait_active и подсовывать свой id вместо только что оплаченной услуги.
"""
from __future__ import annotations

import json

import pytest
import requests

import app.services.cloud.billmgr as billmgr
from app.services.cloud.base import DriverError
from app.services.cloud.billmgr import BillmgrDriver

_TOKEN = json.dumps({"base_url": "http://bill.test", "username": "u", "password": "p"})


class _Resp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._p = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._p


def _ok(doc):
    return {"doc": doc}


def _driver(post_handler) -> BillmgrDriver:
    d = BillmgrDriver(token=_TOKEN)
    d._session.post = post_handler  # type: ignore[assignment]
    return d


def test_stale_deleted_twin_is_ignored(monkeypatch):
    # #85: до заказа с этим domain уже висит СНЕСЁННАЯ услуга (id=50, status=4).
    # Драйвер обязан её проигнорировать и дождаться НОВОЙ услуги (id=90), а не
    # вернуть старый id без IP (что привело бы к сносу и оставило новую сиротой).
    monkeypatch.setattr(billmgr.time, "sleep", lambda *_: None)
    vds_states = iter([
        # snapshot ДО заказа: только старая снесённая тёзка
        _ok({"elem": [{"id": "50", "domain": "n-1", "item_status": "4", "ip": ""}]}),
        # сразу после заказа: новой ещё не видно
        _ok({"elem": [{"id": "50", "domain": "n-1", "item_status": "4", "ip": ""}]}),
        # затем появляется новая услуга active+ip
        _ok({"elem": [
            {"id": "50", "domain": "n-1", "item_status": "4", "ip": ""},
            {"id": "90", "domain": "n-1", "item_status": "2", "ip": "9.9.9.9"},
        ]}),
    ])

    def fake_post(url, data=None, timeout=None):
        f = data["func"]
        if f == "vds.order.param":
            return _Resp(_ok({}))
        if f == "vds":
            return _Resp(next(vds_states))
        if f == "service.changepassword":
            return _Resp(_ok({}))
        raise AssertionError(f)

    d = _driver(fake_post)
    srv = d.create_server(name="n-1", region="7", plan="6", image="t")
    assert srv.external_id == "90"  # новая услуга, а не старая снесённая тёзка
    assert srv.ipv4 == "9.9.9.9"


def test_order_timeout_reconciles_charged_service(monkeypatch):
    # #84: timeout ровно на vds.order.param (деньги списаны, ответ не дошёл) →
    # драйвер сверяется с панелью, находит НОВУЮ услугу и доводит её до active.
    monkeypatch.setattr(billmgr.time, "sleep", lambda *_: None)
    vds_states = iter([
        _ok({"elem": []}),  # snapshot: пусто
        _ok({"elem": [{"id": "77", "domain": "n-2", "item_status": "5", "ip": ""}]}),
        _ok({"elem": [{"id": "77", "domain": "n-2",
                       "item_status": "2", "ip": "1.2.3.4"}]}),
    ])

    def fake_post(url, data=None, timeout=None):
        f = data["func"]
        if f == "vds.order.param":
            raise requests.Timeout("boom")  # обрыв на денежном вызове
        if f == "vds":
            return _Resp(next(vds_states))
        if f == "service.changepassword":
            return _Resp(_ok({}))
        raise AssertionError(f)

    d = _driver(fake_post)
    srv = d.create_server(name="n-2", region="7", plan="6", image="t")
    assert srv.external_id == "77"
    assert srv.ipv4 == "1.2.3.4"


def test_order_timeout_no_service_raises_manual_check(monkeypatch):
    # #84: timeout на ордере, но новой услуги не появилось → авто-снос вслепую
    # опасен (могли не списать) → явная ошибка «проверь панель ВРУЧНУЮ».
    monkeypatch.setattr(billmgr.time, "sleep", lambda *_: None)
    monkeypatch.setattr(billmgr, "_ORDER_RECONCILE_TIMEOUT", 0.03)

    def fake_post(url, data=None, timeout=None):
        f = data["func"]
        if f == "vds.order.param":
            raise requests.Timeout("boom")
        if f == "vds":
            return _Resp(_ok({"elem": []}))
        raise AssertionError(f)

    d = _driver(fake_post)
    with pytest.raises(DriverError, match="ВРУЧНУЮ"):
        d.create_server(name="n-3", region="7", plan="6", image="t")


def test_order_uses_longer_timeout(monkeypatch):
    # #84: денежный ордер-вызов идёт с _ORDER_TIMEOUT, поллинг — с обычным _TIMEOUT.
    monkeypatch.setattr(billmgr.time, "sleep", lambda *_: None)
    seen: dict = {}

    def fake_post(url, data=None, timeout=None):
        f = data["func"]
        seen[f] = timeout
        if f == "vds.order.param":
            return _Resp(_ok({}))
        if f == "vds":
            return _Resp(_ok({"elem": [{"id": "5", "domain": "n-4",
                                        "item_status": "2", "ip": "1.1.1.1"}]}))
        if f == "service.changepassword":
            return _Resp(_ok({}))
        raise AssertionError(f)

    d = _driver(fake_post)
    d.create_server(name="n-4", region="7", plan="6", image="t")
    assert seen["vds.order.param"] == billmgr._ORDER_TIMEOUT
    assert seen["vds"] == billmgr._TIMEOUT
