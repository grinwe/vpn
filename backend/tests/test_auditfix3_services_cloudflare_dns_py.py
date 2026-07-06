"""Auditfix #80 — delete_record различает 404 (тихий no-op) и остальные
ошибки CF (ретрай + ERROR-эскалация вместо молчаливого warning).

Тестируем поведение через мок ``_request``: не отличает ли delete_record
404 от 5xx, делает ли ретраи, эскалирует ли в ERROR при персистентной
не-404 ошибке.
"""
from __future__ import annotations

import logging

from app.services import cloudflare_dns as cf


def _err(status_code=None, msg="boom"):
    return cf.CloudflareError(msg, status_code=status_code)


def test_delete_record_empty_id_is_noop(monkeypatch):
    called = []
    monkeypatch.setattr(cf, "_request", lambda *a, **k: called.append(1))
    cf.delete_record("")
    cf.delete_record(None)  # type: ignore[arg-type]
    assert called == []


def test_delete_record_success_no_retry(monkeypatch):
    calls = []

    def fake_request(method, path, **kw):
        calls.append(path)
        return {"success": True, "result": {}}

    monkeypatch.setattr(cf, "_request", fake_request)
    monkeypatch.setattr(cf, "_get_zone_id", lambda domain=None: "zone1")
    cf.delete_record("rec123", domain="wgse.info")
    assert len(calls) == 1


def test_delete_record_404_is_silent_noop(monkeypatch, caplog):
    """404 → не ретраим, не эскалируем в ERROR (идемпотентность)."""
    calls = []

    def fake_request(method, path, **kw):
        calls.append(path)
        raise _err(status_code=404, msg="not found")

    monkeypatch.setattr(cf, "_request", fake_request)
    monkeypatch.setattr(cf, "_get_zone_id", lambda domain=None: "zone1")
    monkeypatch.setattr(cf.time, "sleep", lambda _s: None)
    with caplog.at_level(logging.ERROR):
        cf.delete_record("recgone")
    assert len(calls) == 1  # без ретраев
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_delete_record_cf_code_81044_is_gone(monkeypatch):
    """Код CF 81044 (Record does not exist) в теле → тоже no-op без ретрая."""
    calls = []

    def fake_request(method, path, **kw):
        calls.append(path)
        raise _err(status_code=400, msg="[{'code': 81044, 'message': '...'}]")

    monkeypatch.setattr(cf, "_request", fake_request)
    monkeypatch.setattr(cf, "_get_zone_id", lambda domain=None: "zone1")
    cf.delete_record("rec81044")
    assert len(calls) == 1


def test_delete_record_transient_error_retries_then_escalates(monkeypatch, caplog):
    """5xx/таймаут → ретраи (_DELETE_RETRIES+1 попыток) → ERROR, без raise."""
    calls = []

    def fake_request(method, path, **kw):
        calls.append(path)
        raise _err(status_code=502, msg="bad gateway")

    monkeypatch.setattr(cf, "_request", fake_request)
    monkeypatch.setattr(cf, "_get_zone_id", lambda domain=None: "zone1")
    monkeypatch.setattr(cf.time, "sleep", lambda _s: None)
    # Alembic-миграции на старте харнесса зовут fileConfig(disable_existing_
    # loggers) → логгер cloudflare_dns приходит disabled=True и ERROR-эскалация
    # не доходит до caplog. Ре-активируем + захватываем именно этот логгер.
    logging.getLogger("app.services.cloudflare_dns").disabled = False
    with caplog.at_level(logging.ERROR, logger="app.services.cloudflare_dns"):
        cf.delete_record("recflaky")  # не должно бросать
    assert len(calls) == cf._DELETE_RETRIES + 1
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs and "STALE" in errs[0].getMessage()


def test_delete_record_network_error_retries(monkeypatch):
    """Сетевая ошибка (status_code=None) трактуется как транзиентная → ретрай."""
    calls = []

    def fake_request(method, path, **kw):
        calls.append(path)
        raise _err(status_code=None, msg="CF request failed: timeout")

    monkeypatch.setattr(cf, "_request", fake_request)
    monkeypatch.setattr(cf, "_get_zone_id", lambda domain=None: "zone1")
    monkeypatch.setattr(cf.time, "sleep", lambda _s: None)
    cf.delete_record("recnet")
    assert len(calls) == cf._DELETE_RETRIES + 1


def test_delete_record_recovers_on_retry(monkeypatch):
    """Первая попытка 502, вторая успешна → успех, без ERROR."""
    state = {"n": 0}
    calls = []

    def fake_request(method, path, **kw):
        calls.append(path)
        state["n"] += 1
        if state["n"] == 1:
            raise _err(status_code=503, msg="unavailable")
        return {"success": True, "result": {}}

    monkeypatch.setattr(cf, "_request", fake_request)
    monkeypatch.setattr(cf, "_get_zone_id", lambda domain=None: "zone1")
    monkeypatch.setattr(cf.time, "sleep", lambda _s: None)
    cf.delete_record("recrecover")
    assert len(calls) == 2


def test_cloudflare_error_carries_status_code():
    e = cf.CloudflareError("x", status_code=404)
    assert e.status_code == 404
    assert cf.CloudflareError("y").status_code is None
