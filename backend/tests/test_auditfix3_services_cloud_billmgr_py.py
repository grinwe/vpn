"""Находка 94: _wait_active глотал персистентную ошибку API молча все 900с, потом
всплывало вводящее в заблуждение «service not found» без причины. Проверяем, что
ошибка теперь (а) логируется throttled в поллинге, (б) попадает в финальные
сообщения create_server («service not found» / «no IPv4»)."""
import logging

import pytest

from app.services.cloud.base import DriverError
from app.services.cloud.billmgr import BillmgrDriver

_TOKEN = '{"base_url": "https://panel.example", "username": "u", "password": "p"}'


def _driver() -> BillmgrDriver:
    return BillmgrDriver(_TOKEN)


def test_wait_active_logs_and_returns_last_exc(monkeypatch, caplog):
    """Персистентная ошибка API в поллинге → не молчим: возвращаем last_exc и
    пишем throttled WARNING (а не крутимся немо весь таймаут)."""
    d = _driver()
    monkeypatch.setattr("app.services.cloud.billmgr._POLL_TIMEOUT", 0.05)
    monkeypatch.setattr("app.services.cloud.billmgr._POLL_INTERVAL", 0.0)
    monkeypatch.setattr("app.services.cloud.billmgr.time.sleep", lambda *_: None)

    def boom(func, **kw):
        raise DriverError("captcha_verification_failed")

    monkeypatch.setattr(d, "_call", boom)
    # Alembic-миграции на старте харнесса зовут fileConfig(disable_existing_
    # loggers) → логгер billmgr приходит disabled=True и throttled-WARNING не
    # доходит до caplog. Ре-активируем + захватываем именно этот логгер.
    logging.getLogger("app.services.cloud.billmgr").disabled = False
    with caplog.at_level(logging.WARNING, logger="app.services.cloud.billmgr"):
        service_id, ipv4, cost, elem, last_exc = d._wait_active("dc-ru-99")

    assert service_id == ""
    assert ipv4 == ""
    assert isinstance(last_exc, DriverError)
    assert "captcha_verification_failed" in str(last_exc)
    assert any(
        "captcha_verification_failed" in r.getMessage() for r in caplog.records
    )


def test_create_server_surfaces_last_exc_service_not_found(monkeypatch):
    """service not found + была ошибка API → причина в тексте DriverError."""
    d = _driver()
    monkeypatch.setattr(d, "_existing_ids", lambda name: set())
    monkeypatch.setattr(d, "_call", lambda func, **kw: {})  # ордер «прошёл»
    monkeypatch.setattr(
        d, "_wait_active",
        lambda name, known: ("", "", None, {}, DriverError("captcha_verification_failed")),
    )
    with pytest.raises(DriverError) as ei:
        d.create_server(name="dc-ru-99", region="1", plan="2", image="3")
    msg = str(ei.value)
    assert "service not found" in msg
    assert "captcha_verification_failed" in msg


def test_create_server_surfaces_last_exc_no_ipv4(monkeypatch):
    """Услуга есть, но без IPv4 + была ошибка API → причина в тексте + orphan-guard."""
    d = _driver()
    destroyed: list[str] = []
    monkeypatch.setattr(d, "_existing_ids", lambda name: set())
    monkeypatch.setattr(d, "_call", lambda func, **kw: {})
    monkeypatch.setattr(d, "_safe_destroy", lambda sid: destroyed.append(sid))
    monkeypatch.setattr(
        d, "_wait_active",
        lambda name, known: ("77", "", None, {}, DriverError("api timeout")),
    )
    with pytest.raises(DriverError) as ei:
        d.create_server(name="dc-ru-99", region="1", plan="2", image="3")
    msg = str(ei.value)
    assert "no IPv4" in msg
    assert "api timeout" in msg
    assert destroyed == ["77"]


def test_create_server_no_reason_suffix_when_no_exc(monkeypatch):
    """Без сглоченной ошибки суффикс-причина не добавляется (сообщение как раньше)."""
    d = _driver()
    monkeypatch.setattr(d, "_existing_ids", lambda name: set())
    monkeypatch.setattr(d, "_call", lambda func, **kw: {})
    monkeypatch.setattr(
        d, "_wait_active", lambda name, known: ("", "", None, {}, None),
    )
    with pytest.raises(DriverError) as ei:
        d.create_server(name="dc-ru-99", region="1", plan="2", image="3")
    assert str(ei.value) == "billmgr order dc-ru-99 charged but service not found"
