"""audit #105 — guard: единственный селектор целевой ноды — provisioning.choose_node.

Мёртвый модуль ``app.services.failover`` (функция ``select_target_node``) удалён:
у него была расходящаяся с ``choose_node`` семантика (не учитывал ``cooldown_until``
и ``NodeUserBan``), из-за чего любой, кто взял бы его по докстрингу, получил бы
миграции на ноды в cooldown/бан-листе. Тест фиксирует удаление, чтобы параллельный
селектор не вернулся незаметно.
"""
from __future__ import annotations

import importlib

import pytest


def test_dead_failover_module_removed() -> None:
    """Модуль-дублёр удалён — импорт обязан падать."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.failover")


def test_choose_node_is_the_sole_selector() -> None:
    """Канонический селектор живёт в provisioning и никуда не делся."""
    provisioning = importlib.import_module("app.services.provisioning")
    assert hasattr(provisioning, "choose_node")
    # На всякий: не должно быть реэкспорта старого имени.
    assert not hasattr(provisioning, "select_target_node")
