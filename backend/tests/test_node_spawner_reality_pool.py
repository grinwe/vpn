"""Tests for Reality SNI pool rotation in node_spawner.

Покрывает:
- ``pick_reality_sni`` возвращает наименее используемый SNI из пула.
- ``ensure_reality_config`` при ``sni=None`` использует pool-pick.
- Явно переданный ``sni`` имеет приоритет над пулом.
- ``REALITY_SNI`` env-override форсит один SNI (dev/test обратная совместимость).
"""
from __future__ import annotations

from app.services import node_spawner
from tests.factories import make_config, make_node


def test_pick_reality_sni_empty_pool_returns_first(db_session) -> None:
    """На чистой БД — возвращается первый элемент пула (все counts == 0, min
    стабилен по итерации)."""
    pick = node_spawner.pick_reality_sni(db_session)
    assert pick in node_spawner.REALITY_DEST_POOL
    # min() по равным значениям берёт первый встреченный — yandex.
    assert pick == node_spawner.REALITY_DEST_POOL[0]


def test_pick_reality_sni_avoids_used(db_session) -> None:
    """Занятые SNI уступают свободным, даже если первый в пуле занят."""
    node1 = make_node(db_session, name="n1", host="203.0.113.1")
    make_config(db_session, node1, sni=node_spawner.REALITY_DEST_POOL[0])
    pick = node_spawner.pick_reality_sni(db_session)
    # Первый уже занят → выбирается один из свободных.
    assert pick != node_spawner.REALITY_DEST_POOL[0]
    assert pick in node_spawner.REALITY_DEST_POOL


def test_pick_reality_sni_rotates_balance(db_session) -> None:
    """Все домены пула заняты по разу — следующий выбор должен быть снова
    ранним элементом пула (равные counts)."""
    for idx, sni in enumerate(node_spawner.REALITY_DEST_POOL):
        node = make_node(db_session, name=f"n{idx}", host=f"203.0.113.{10 + idx}")
        make_config(db_session, node, name=f"cfg{idx}", sni=sni)
    pick = node_spawner.pick_reality_sni(db_session)
    # Все заняты по одному — min даст первый в пуле.
    assert pick == node_spawner.REALITY_DEST_POOL[0]


def test_pick_reality_sni_env_override(monkeypatch, db_session) -> None:
    """REALITY_SNI env переопределяет pool-rotation."""
    monkeypatch.setattr(node_spawner, "_REALITY_SNI_ENV_OVERRIDE", "forced.example.com")
    pick = node_spawner.pick_reality_sni(db_session)
    assert pick == "forced.example.com"


def test_ensure_reality_config_uses_pool_when_sni_not_given(db_session) -> None:
    """sni=None → pick из пула."""
    node = make_node(db_session, name="spawn-pool", host="203.0.113.50")
    cfg = node_spawner.ensure_reality_config(db_session, node)
    assert cfg.sni in node_spawner.REALITY_DEST_POOL
    assert cfg.fallback == f"{cfg.sni}:443"
    assert cfg.settings["dest"] == f"{cfg.sni}:443"


def test_ensure_reality_config_explicit_sni_wins(db_session) -> None:
    """Явный sni имеет приоритет над пулом даже если он НЕ в пуле."""
    node = make_node(db_session, name="spawn-explicit", host="203.0.113.51")
    cfg = node_spawner.ensure_reality_config(db_session, node, sni="custom.host.io")
    assert cfg.sni == "custom.host.io"
    assert cfg.fallback == "custom.host.io:443"


def test_ensure_reality_config_idempotent_preserves_sni(db_session) -> None:
    """Повторный вызов не перезаписывает уже существующий sni."""
    node = make_node(db_session, name="spawn-idem", host="203.0.113.52")
    first = node_spawner.ensure_reality_config(db_session, node, sni="vk.ru")
    second = node_spawner.ensure_reality_config(db_session, node, sni="mail.ru")
    assert first.id == second.id
    assert second.sni == "vk.ru"


def test_reality_dest_pool_has_expected_ru_hosts() -> None:
    """Smoke: пул не пустой, все хосты выглядят как RU-ASN домены."""
    assert len(node_spawner.REALITY_DEST_POOL) >= 3
    assert "www.yandex.ru" in node_spawner.REALITY_DEST_POOL
    assert "vk.ru" in node_spawner.REALITY_DEST_POOL
