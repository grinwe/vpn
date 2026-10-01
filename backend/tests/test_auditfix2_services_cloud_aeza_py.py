"""Регрессия на finding #89: Aeza-драйвер не должен падать голым
AttributeError, если провайдер вернёт data["data"] списком (как /locations),
а не dict. Ожидаем корректный разбор или чистый DriverError.
"""
from __future__ import annotations

import app.services.cloud.aeza as aeza_mod
from app.services.cloud.aeza import AezaDriver
from app.services.cloud.base import DriverError


def _driver() -> AezaDriver:
    return AezaDriver(token="dummy")


def test_create_server_handles_list_envelope(monkeypatch) -> None:
    """data["data"] приходит списком — разбор не должен крашить."""
    drv = _driver()
    monkeypatch.setattr(drv, "_post", lambda path, body: {"data": [{"id": "s1"}]})
    monkeypatch.setattr(
        drv,
        "_get",
        lambda path: {"data": [{"id": "s1", "status": "active", "ip": "1.2.3.4"}]},
    )
    server = drv.create_server(
        name="n", region="r", plan="p", image="i"
    )
    assert server.external_id == "s1"
    assert server.ipv4 == "1.2.3.4"


def test_wait_running_non_dict_data_raises_driver_error(monkeypatch) -> None:
    """Если API отдаёт нестандартную форму и статус не active —
    должен подняться DriverError по таймауту, а не AttributeError."""
    drv = _driver()
    monkeypatch.setattr(aeza_mod, "POLL_TIMEOUT", 0.01)
    monkeypatch.setattr(aeza_mod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(drv, "_get", lambda path: {"data": "weird-string"})
    try:
        drv._wait_running("s1")
    except DriverError:
        pass
    else:  # pragma: no cover - защита от регрессии
        raise AssertionError("ожидался DriverError")
