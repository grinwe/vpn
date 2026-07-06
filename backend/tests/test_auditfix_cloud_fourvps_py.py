"""Аудит-фикс #86: get_balance у 4vps не должен схлопывать баланс 0 в None.

`_to_float` написан как ``float(v) or None`` — для цен это ок (0 → None),
но для баланса 0.00 это валидное значение: при None воркер cloud-billing
пропускает провайдера, и low-balance алерт молчит ровно когда деньги
кончились. get_balance парсит напрямую, без ``or None``.
"""
from app.services.cloud.fourvps import FourVpsDriver, _to_float


def _driver_with_balance(monkeypatch, value):
    drv = FourVpsDriver("panel:apikey")
    monkeypatch.setattr(drv, "_call", lambda method, path, params: {"userBalance": value})
    return drv


def test_get_balance_zero_is_zero_not_none(monkeypatch):
    """Баланс ровно 0 → 0.0, а не None (алерт должен сработать)."""
    drv = _driver_with_balance(monkeypatch, 0)
    assert drv.get_balance() == 0.0

    drv = _driver_with_balance(monkeypatch, "0.00")
    assert drv.get_balance() == 0.0


def test_get_balance_positive_and_garbage(monkeypatch):
    """Положительный баланс парсится, мусор/отсутствие → None."""
    drv = _driver_with_balance(monkeypatch, "1234.56")
    assert drv.get_balance() == 1234.56

    drv = _driver_with_balance(monkeypatch, None)
    assert drv.get_balance() is None

    drv = _driver_with_balance(monkeypatch, "n/a")
    assert drv.get_balance() is None


def test_get_balance_empty_data(monkeypatch):
    """API вернул пустой data → None (нет значения, а не нулевой баланс)."""
    drv = FourVpsDriver("panel:apikey")
    monkeypatch.setattr(drv, "_call", lambda method, path, params: None)
    assert drv.get_balance() is None


def test_to_float_still_collapses_zero():
    """_to_float намеренно оставлен как есть — для цен 0 → None приемлемо."""
    assert _to_float(0) is None
    assert _to_float("1.5") == 1.5
    assert _to_float(None) is None
