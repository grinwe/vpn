"""Unit tests for the generic ISPsystem BILLmanager cloud driver (mocked HTTP).

Проверяем: разбор JSON-токена, боевой order→poll→changepassword путь (skipbasket+
sok, поиск услуги по domain, item_status==2+ip), delete/reinstall тела запросов,
извлечение IP/scalar-разворот, дегрейд offerings, конверт ошибки billmgr.
"""
from __future__ import annotations

import json

import pytest

from app.services.cloud.base import DriverError
from app.services.cloud.billmgr import (
    BillmgrDriver,
    _extract_ip,
    _find_by_domain,
    _parse_token,
    _scalar,
)

_TOKEN = json.dumps(
    {"base_url": "https://bill.ufo.hosting/billmgr/", "username": "u", "password": "p"}
)


class _Resp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._p = payload
        self.status_code = status_code

    def json(self):
        return self._p


def _ok(doc):
    return {"doc": doc}


def _driver(post_handler) -> BillmgrDriver:
    d = BillmgrDriver(token=_TOKEN)
    d._session.post = post_handler  # type: ignore[assignment]
    return d


def test_parse_token():
    base, user, pw = _parse_token(_TOKEN)
    # _parse_token срезает хвостовой /billmgr (его дописывает _call → иначе
    # /billmgr/billmgr = HTML 404); trailing / тоже снимается.
    assert base == "https://bill.ufo.hosting"
    assert user == "u" and pw == "p"
    with pytest.raises(DriverError):
        _parse_token("not json")
    with pytest.raises(DriverError):
        _parse_token(json.dumps({"base_url": "x"}))  # нет username/password


def test_scalar_unwrap():
    assert _scalar({"$": "v"}) == "v"
    assert _scalar([{"$": "a"}]) == "a"
    assert _scalar("plain") == "plain"


def test_extract_ip_variants():
    assert _extract_ip({"ip": "1.2.3.4"}) == "1.2.3.4"
    assert _extract_ip({"ip": "1.2.3.4 2001:db8::1"}) == "1.2.3.4"  # v4 из смеси
    assert _extract_ip({"ip": {"$": "5.6.7.8"}}) == "5.6.7.8"
    assert _extract_ip({"ip": "2001:db8::1"}) == ""  # ipv6-only
    # СПИСОК с ipv6 ПЕРВЫМ — должны дойти до v4 во втором элементе (была бага)
    assert _extract_ip({"ip": [{"$": "2001:db8::1"}, {"$": "5.6.7.8"}]}) == "5.6.7.8"
    assert _extract_ip({"ip": [{"ip": "9.9.9.9", "type": "ipv4"}]}) == "9.9.9.9"
    assert _extract_ip({}) == ""


def test_find_by_domain_newest():
    doc = {"elem": [
        {"id": "10", "domain": "n"},
        {"id": "20", "domain": "n"},
        {"id": "30", "domain": "other"},
    ]}
    assert _find_by_domain(doc, "n")["id"] == "20"  # свежайший по id
    assert _find_by_domain(doc, "missing") is None


def test_create_server_full_path(monkeypatch):
    monkeypatch.setattr("app.services.cloud.billmgr.time.sleep", lambda *_: None)
    vds_states = iter([
        _ok({"elem": [{"id": "100", "domain": "ufo-1", "item_status": "5", "ip": ""}]}),
        _ok({"elem": [{"id": "100", "domain": "ufo-1", "item_status": "2",
                       "ip": "185.1.2.3", "cost": "5"}]}),
    ])
    calls: list = []

    def fake_post(url, data=None, timeout=None):
        func = data["func"]
        calls.append((func, data))
        if func == "vds.order.param":
            return _Resp(_ok({}))
        if func == "vds":
            return _Resp(next(vds_states))
        if func == "service.changepassword":
            return _Resp(_ok({}))
        raise AssertionError(f"unexpected func {func}")

    d = _driver(fake_post)
    srv = d.create_server(
        name="ufo-1", region="7", plan="6", image="ISPsystem__Ubuntu-22.04",
    )
    assert srv.external_id == "100"
    assert srv.ipv4 == "185.1.2.3"
    assert srv.root_password and len(srv.root_password) == 20  # известный root-пароль
    order = next(dd for f, dd in calls if f == "vds.order.param")
    assert order["skipbasket"] == "on" and order["sok"] == "ok"
    assert order["domain"] == "ufo-1"
    assert order["pricelist"] == "6" and order["datacenter"] == "7"
    assert order["ostempl"] == "ISPsystem__Ubuntu-22.04"
    assert order["autoprolong"] == "1"  # иначе нода удалится в конце периода
    assert order["authinfo"] == "u:p"
    assert any(f == "service.changepassword" for f, _ in calls)


def test_create_server_dollar_wrapped_json(monkeypatch):
    # billmgr заворачивает значения в {"$": v} — money-path должен это переварить
    # (item_status/id/ip читаются через _scalar, не напрямую).
    monkeypatch.setattr("app.services.cloud.billmgr.time.sleep", lambda *_: None)
    vds_states = iter([
        _ok({"elem": [{"id": {"$": "100"}, "domain": {"$": "ufo-1"},
                       "item_status": {"$": "5"}, "ip": {"$": ""}}]}),
        _ok({"elem": [{"id": {"$": "100"}, "domain": {"$": "ufo-1"},
                       "item_status": {"$": "2"}, "ip": {"$": "185.1.2.3"}}]}),
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
    srv = d.create_server(name="ufo-1", region="7", plan="6", image="t")
    assert srv.external_id == "100" and srv.ipv4 == "185.1.2.3"


def test_create_server_orphan_guard_on_timeout(monkeypatch):
    # услуга заказана (списали), но не поднялась за таймаут → СНОСИМ её (vds.delete),
    # чтобы ретраи не плодили оплаченных сирот, и поднимаем ошибку.
    monkeypatch.setattr("app.services.cloud.billmgr.time.sleep", lambda *_: None)
    monkeypatch.setattr("app.services.cloud.billmgr._POLL_TIMEOUT", 0.03)
    calls: list = []

    def fake_post(url, data=None, timeout=None):
        f = data["func"]
        calls.append(f)
        if f == "vds.order.param":
            return _Resp(_ok({}))
        if f == "vds":  # всегда processing, IP не появляется
            return _Resp(_ok({"elem": [{"id": "100", "domain": "ufo-1",
                                        "item_status": "5", "ip": ""}]}))
        if f == "vds.delete":
            return _Resp(_ok({}))
        raise AssertionError(f)

    d = _driver(fake_post)
    with pytest.raises(DriverError, match="destroyed"):
        d.create_server(name="ufo-1", region="7", plan="6", image="t")
    assert "vds.delete" in calls  # orphan-guard снёс оплаченную залипшую услугу


def test_create_server_self_destroys_on_changepassword_fail(monkeypatch):
    monkeypatch.setattr("app.services.cloud.billmgr.time.sleep", lambda *_: None)
    calls: list = []

    def fake_post(url, data=None, timeout=None):
        f = data["func"]
        calls.append(f)
        if f == "vds.order.param":
            return _Resp(_ok({}))
        if f == "vds":
            return _Resp(_ok({"elem": [{"id": "100", "domain": "ufo-1",
                                        "item_status": "2", "ip": "1.2.3.4"}]}))
        if f == "service.changepassword":
            return _Resp(_ok({"error": {"msg": "policy reject"}}))  # всегда фейл
        if f == "vds.delete":
            return _Resp(_ok({}))
        raise AssertionError(f)

    d = _driver(fake_post)
    with pytest.raises(DriverError, match="changepassword failed"):
        d.create_server(name="ufo-1", region="7", plan="6", image="t")
    assert "vds.delete" in calls  # no-key нода без пароля бесполезна → снесли


def test_reinstall_raises_on_changepassword_fail():
    def fake_post(url, data=None, timeout=None):
        f = data["func"]
        if f == "vds.edit":
            return _Resp(_ok({}))
        if f == "service.changepassword":
            return _Resp(_ok({"error": {"msg": "nope"}}))
        raise AssertionError(f)

    d = _driver(fake_post)
    # changepassword НЕ глушим — reinstall падает громко (иначе пароль разъедется)
    with pytest.raises(DriverError, match="nope"):
        d.reinstall_server("100", "t", password="pw")


def test_destroy_and_reinstall_bodies():
    calls: list = []

    def fake_post(url, data=None, timeout=None):
        calls.append((data["func"], data))
        return _Resp(_ok({}))

    d = _driver(fake_post)
    d.destroy_server("100")
    d.reinstall_server("100", "ISPsystem__Debian-12", password="newpw123")
    delete = next(dd for f, dd in calls if f == "vds.delete")
    assert delete["elid"] == "100" and delete["sok"] == "ok"
    edit = next(dd for f, dd in calls if f == "vds.edit")
    assert edit["elid"] == "100" and edit["ostempl"] == "ISPsystem__Debian-12"
    cp = next(dd for f, dd in calls if f == "service.changepassword")
    assert cp["passwd"] == "newpw123" and cp["confirm"] == "newpw123"


def test_error_envelope_raises():
    def fake_post(url, data=None, timeout=None):
        return _Resp(_ok({"error": {"msg": "Access denied"}}))

    d = _driver(fake_post)
    with pytest.raises(DriverError, match="Access denied"):
        d.destroy_server("100")


def test_offerings_degrade_to_empty():
    # неизвестная форма slist / ошибка → [] (форма дегрейдит, не падаем)
    def err_post(url, data=None, timeout=None):
        return _Resp(_ok({"error": {"msg": "no"}}))

    assert _driver(err_post).list_plans() == []

    def weird_post(url, data=None, timeout=None):
        return _Resp(_ok({"slist": "unexpected"}))

    assert _driver(weird_post).list_datacenters() == []


def test_offerings_real_ufo_shape():
    # реальная двухшаговая форма UFO: тарифы в list[tariflist].elem (НЕ slist),
    # датацентры в slist шага 1, ОС в slist шага 2 (vds.order.param с pricelist).
    pricelist_doc = _ok({
        "list": [{"$name": "tariflist", "elem": [
            {"pricelist": {"$": "150"},
             "desc": {"$": "<p>Naos, vCore x1, 1 GB RAM</p>"},
             "price": {"$": "<del>1025.85 RUB per month</del> <b>605.85 RUB per month</b>"}},
        ]}],
        "slist": [
            {"$name": "datacenter", "val": [
                {"$key": "3", "$": "Russia"}, {"$key": "9", "$": "Russia"}]},
            {"$name": "period", "val": [{"$key": "1", "$": "Month"}]},
        ],
    })
    param_doc = _ok({"slist": [{"$name": "ostempl", "val": [
        {"$key": "ISPsystem__Ubuntu-22.04-amd64", "$": "Ubuntu 22.04"}]}]})

    def post(url, data=None, timeout=None):
        f = data["func"]
        if f == "vds.order":  # вход через vds.order (он отдаёт шаг pricelist)
            return _Resp(pricelist_doc)
        if f == "vds.order.param":  # шаг 2: pricelist+datacenter обязаны быть выбраны
            assert data["pricelist"] == "150" and data["datacenter"] == "3"
            return _Resp(param_doc)
        raise AssertionError(f)

    d = _driver(post)
    assert d.list_plans() == [
        {"id": "150", "name": "Naos, vCore x1, 1 GB RAM", "price": 605.85}
    ]
    assert d.list_datacenters() == [
        {"id": "3", "name": "Russia"}, {"id": "9", "name": "Russia"}
    ]
    assert d.list_images() == [
        {"id": "ISPsystem__Ubuntu-22.04-amd64", "name": "Ubuntu 22.04"}
    ]
