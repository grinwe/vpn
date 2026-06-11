"""Ops-планировщик (Phase 2, dry-run): read-тулы + kill-switch.

LLM-петлю (plan_ops c Claude) тут не гоняем — только детерминированные read-тулы
(`ops_tools`, без сети/API) и гард AGENT_ENABLED (срабатывает до любого вызова
Claude). Боевую генерацию плана проверяем вручную/в стейджинге с ключом.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import models
from app.security import encrypt
from app.services.agent import ops, ops_tools


def _provider(db: Session) -> models.CloudProvider:
    p = models.CloudProvider(
        name="4vps-ru", kind=models.CloudProviderKind.fourvps,
        api_token_enc=encrypt("1:key"), is_active=True,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_list_providers_nodes_exits(db_session: Session) -> None:
    p = _provider(db_session)
    node = models.VPNNode(
        name="ru-node-1", region="ru", host="1.1.1.1",
        status=models.VPNNodeStatus.active, is_active=True, provider_id=p.id,
    )
    exit_node = models.WGExitNode(
        name="fi-exit-1", region="fi", host="2.2.2.2",
        status=models.WGExitNodeStatus.active, is_active=True, provider_id=p.id,
    )
    db_session.add_all([node, exit_node])
    db_session.commit()

    provs = ops_tools.list_providers(db_session)["providers"]
    assert any(x["id"] == p.id and x["kind"] == "4vps" for x in provs)

    nodes = ops_tools.list_nodes(db_session)["nodes"]
    assert any(n["name"] == "ru-node-1" and n["status"] == "active" for n in nodes)

    # фильтр по статусу
    assert ops_tools.list_nodes(db_session, status="disabled")["nodes"] == []

    exits = ops_tools.list_exits(db_session)["exits"]
    assert any(e["name"] == "fi-exit-1" for e in exits)


def test_node_load_empty_node(db_session: Session) -> None:
    node = models.VPNNode(
        name="ru-node-2", region="ru", host="3.3.3.3",
        status=models.VPNNodeStatus.active, is_active=True,
    )
    db_session.add(node)
    db_session.commit()
    db_session.refresh(node)

    load = ops_tools.node_load(db_session, node.id)
    assert load["node_id"] == node.id
    assert load["assigned_subscriptions"] == 0
    assert load["active_users_latest"] is None


def test_node_load_missing_node(db_session: Session) -> None:
    assert "error" in ops_tools.node_load(db_session, 999999)


def test_plan_ops_disabled_raises(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    monkeypatch.delenv("AGENT_ENABLED", raising=False)
    # выключен → AgentError ДО любого обращения к Claude
    with pytest.raises(ops.AgentError, match="выключен"):
        ops.plan_ops(db_session, "закажи 2 ноды в германии")


def test_plan_ops_empty_command_raises(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    monkeypatch.setenv("AGENT_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    with pytest.raises(ops.AgentError, match="пустая команда"):
        ops.plan_ops(db_session, "   ")
