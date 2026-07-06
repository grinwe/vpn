"""Аудит-фикс #88: 4vps set_autoprolong не должен слепо тоггать вслепую.

`/action/autoprolong` у 4vps — ТОГГЛ, а не установка значения. Старый код
дёргал его до двух раз, не зная исходного состояния: если автопродление уже
включено, первый вызов его ВЫКЛЮЧАЛ и корректность держалась на втором вызове.
Падение второго молча оставляло ноду без автопродления. Фикс: читаем текущее
состояние из /myservers и тоггаем только при несовпадении.
"""
import pytest

from app.services.cloud.base import DriverError
from app.services.cloud.fourvps import FourVpsDriver


class _Recorder:
    """Мок _call: отдаёт /myservers с заданным autoprolong и считает тогглы."""

    def __init__(self, current_state, toggle_new_state=None):
        self._current = current_state  # значение поля autoprolong в /myservers
        self.toggle_calls = 0
        # состояние, которое вернёт тоггл (по умолчанию — инверсия current bool)
        self._toggle_new = toggle_new_state

    def __call__(self, method, path, params):
        if path == "/myservers":
            srv = {"id": "srv-1", "ipv4": "1.1.1.1", "status": "active"}
            if self._current is not None:
                srv["autoprolong"] = self._current
            return {"serverlist": [srv]}
        if path == "/action/autoprolong":
            self.toggle_calls += 1
            return self._toggle_new
        raise AssertionError(f"unexpected call {path}")


def _driver(monkeypatch, recorder):
    drv = FourVpsDriver("panel:apikey")
    monkeypatch.setattr(drv, "_call", recorder)
    return drv


def test_already_enabled_no_toggle(monkeypatch):
    """Уже включено и просят включить → НИ одного тоггла (иначе выключили бы)."""
    rec = _Recorder(current_state="1")
    drv = _driver(monkeypatch, rec)
    assert drv.set_autoprolong("srv-1", True) is True
    assert rec.toggle_calls == 0


def test_already_disabled_no_toggle(monkeypatch):
    """Уже выключено и просят выключить → ни одного тоггла."""
    rec = _Recorder(current_state="0")
    drv = _driver(monkeypatch, rec)
    assert drv.set_autoprolong("srv-1", False) is False
    assert rec.toggle_calls == 0


def test_off_to_on_single_toggle(monkeypatch):
    """Выключено, просят включить → ровно один тоггл, вернувший True."""
    rec = _Recorder(current_state="0", toggle_new_state=True)
    drv = _driver(monkeypatch, rec)
    assert drv.set_autoprolong("srv-1", True) is True
    assert rec.toggle_calls == 1


def test_toggle_lands_wrong_raises(monkeypatch):
    """Известно исходное, но тоггл вернул не то состояние → DriverError."""
    rec = _Recorder(current_state="0", toggle_new_state=False)
    drv = _driver(monkeypatch, rec)
    with pytest.raises(DriverError, match="autoprolong"):
        drv.set_autoprolong("srv-1", True)


def test_unknown_state_falls_back_to_blind_toggle(monkeypatch):
    """Поле autoprolong отсутствует → слепой тоггл; первый вернул True → ок."""
    rec = _Recorder(current_state=None, toggle_new_state=True)
    drv = _driver(monkeypatch, rec)
    assert drv.set_autoprolong("srv-1", True) is True
    assert rec.toggle_calls == 1
