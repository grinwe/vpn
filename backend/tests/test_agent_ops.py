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
from app.services.agent import ops, ops_execution, ops_tools


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


def test_ops_plan_persist_roundtrip(db_session: Session) -> None:
    # Фундамент Phase 3: план персистится целиком (шаги/params), id+hash —
    # якорь для confirm→execute. conftest гоняет alembic-миграции → тест
    # заодно валидирует миграцию 0052 и модель OpsPlan.
    p = models.OpsPlan(
        actor="123",
        command="закажи 2 ноды в германии, подними туннель",
        model="claude-sonnet-4-6",
        plan={
            "summary": "заказ 2 нод",
            "feasible": True,
            "steps": [{"kind": "order_node", "tier": "costly", "params": {"count": 2}}],
            "needs_confirmation": True,
        },
        content_hash="ab" * 32,  # 64 hex
        feasible=True,
        needs_confirmation=True,
        status="proposed",
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    assert p.id is not None
    assert p.created_at is not None  # default=utcnow
    got = db_session.get(models.OpsPlan, p.id)
    assert got.command == "закажи 2 ноды в германии, подними туннель"
    assert got.plan["steps"][0]["params"]["count"] == 2
    assert got.status == "proposed"
    assert len(got.content_hash) == 64


# ── validate_plan (Phase 3 gate 2/3/6, без сети/исполнения) ──


def _ops_plan(steps: list[dict], *, feasible: bool = True) -> models.OpsPlan:
    return models.OpsPlan(
        actor="1", command="c", model="m",
        plan={"feasible": feasible, "summary": "s", "steps": steps},
        content_hash="0" * 64,
    )


def _active_node(db: Session, name: str, host: str) -> models.VPNNode:
    n = models.VPNNode(
        name=name, region="ru", host=host,
        status=models.VPNNodeStatus.active, is_active=True,
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def test_validate_rejects_unknown_kind(db_session: Session) -> None:
    res = ops_execution.validate_plan(db_session, _ops_plan([{"kind": "nuke", "params": {}}]))
    assert not res["ok"]
    assert any("allowlist" in r for r in res["rejections"])


def test_validate_order_ok_server_tier_overrides_llm(db_session: Session) -> None:
    p = _provider(db_session)
    res = ops_execution.validate_plan(
        db_session,
        # LLM наврал tier=read на платном заказе — сервер должен поставить costly.
        _ops_plan([{"kind": "order_node", "tier": "read",
                    "params": {"provider_id": p.id, "count": 2}}]),
    )
    assert res["ok"], res["rejections"]
    assert res["steps"][0]["tier"] == "costly"
    assert res["needs_confirmation"] is True
    assert res["totals"]["order_count"] == 2


def test_validate_count_cap(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _provider(db_session)
    monkeypatch.setenv("OPS_MAX_ORDER_COUNT", "3")
    res = ops_execution.validate_plan(
        db_session,
        _ops_plan([{"kind": "order_node", "params": {"provider_id": p.id, "count": 9}}]),
    )
    assert not res["ok"]
    assert any("OPS_MAX_ORDER_COUNT" in r for r in res["rejections"])


def test_validate_max_nodes_per_plan(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _provider(db_session)
    monkeypatch.setenv("OPS_MAX_ORDER_COUNT", "5")
    monkeypatch.setenv("OPS_MAX_NODES_PER_PLAN", "2")
    res = ops_execution.validate_plan(
        db_session,
        _ops_plan([
            {"kind": "order_node", "params": {"provider_id": p.id, "count": 2}},
            {"kind": "order_node", "params": {"provider_id": p.id, "count": 2}},
        ]),
    )
    assert not res["ok"]
    assert any("OPS_MAX_NODES_PER_PLAN" in r for r in res["rejections"])


def test_validate_provider_not_found(db_session: Session) -> None:
    res = ops_execution.validate_plan(
        db_session,
        _ops_plan([{"kind": "order_node", "params": {"provider_id": 999999, "count": 1}}]),
    )
    assert not res["ok"]
    assert any("не найден" in r for r in res["rejections"])


def test_validate_destructive_invariant_blocks(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _active_node(db_session, "ru-node-d1", "9.9.9.1")
    monkeypatch.setattr(ops_execution, "_assigned_subscriptions", lambda db, nid: 5)
    res = ops_execution.validate_plan(
        db_session, _ops_plan([{"kind": "destroy", "params": {"node_id": node.id}}])
    )
    assert not res["ok"]
    assert any("gate 6" in r for r in res["rejections"])


def test_validate_destructive_ok_after_migrate(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _active_node(db_session, "ru-node-d2", "9.9.9.2")
    dst = _active_node(db_session, "ru-node-d3", "9.9.9.3")
    monkeypatch.setattr(ops_execution, "_assigned_subscriptions", lambda db, nid: 5)
    res = ops_execution.validate_plan(
        db_session,
        _ops_plan([
            {"kind": "migrate_users",
             "params": {"from_node_id": src.id, "to_node_id": dst.id}},
            {"kind": "destroy", "params": {"node_id": src.id}},
        ]),
    )
    assert res["ok"], res["rejections"]
    assert res["totals"]["destructive"] == 2


def test_validate_feasible_false_rejected(db_session: Session) -> None:
    res = ops_execution.validate_plan(
        db_session, _ops_plan([{"kind": "set_active", "params": {}}], feasible=False)
    )
    assert not res["ok"]


# ── execute_plan (Phase 3 исполнитель, за флагом; pre-flight/dispatch замоканы) ──


def _persist_plan(
    db: Session, steps: list[dict], *, status: str = "proposed"
) -> models.OpsPlan:
    p = models.OpsPlan(
        actor="1", command="c", model="m",
        plan={"feasible": True, "summary": "s", "steps": steps},
        content_hash="0" * 64, status=status,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_execute_disabled_raises(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPS_EXECUTE_ENABLED", raising=False)
    p = _persist_plan(db_session, [{"kind": "set_active", "params": {}}])
    with pytest.raises(ops_execution.OpsExecError, match="выключено"):
        ops_execution.execute_plan(db_session, p)


def test_execute_terminal_status_raises(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPS_EXECUTE_ENABLED", "1")
    p = _persist_plan(db_session, [{"kind": "set_active", "params": {}}], status="executed")
    with pytest.raises(ops_execution.OpsExecError, match="повторное"):
        ops_execution.execute_plan(db_session, p)


def test_execute_validation_fail_marks_failed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPS_EXECUTE_ENABLED", "1")
    p = _persist_plan(db_session, [{"kind": "nuke", "params": {}}])
    with pytest.raises(ops_execution.OpsExecError, match="валидацию"):
        ops_execution.execute_plan(db_session, p)
    db_session.refresh(p)
    assert p.status == "failed"
    assert p.execution["phase"] == "validate"


def test_execute_preflight_fail_marks_failed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPS_EXECUTE_ENABLED", "1")
    prov = _provider(db_session)
    p = _persist_plan(db_session, [
        {"kind": "order_node",
         "params": {"provider_id": prov.id, "count": 1, "region": "de", "plan": "cx01"}},
    ])
    monkeypatch.setattr(
        ops_execution, "_preflight",
        lambda db, v: {"ok": False, "reasons": ["баланс мал"], "total_cost": 0, "step_costs": {}},
    )
    with pytest.raises(ops_execution.OpsExecError, match="pre-flight"):
        ops_execution.execute_plan(db_session, p)
    db_session.refresh(p)
    assert p.status == "failed"
    assert p.execution["phase"] == "preflight"


def test_execute_happy_path_order(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPS_EXECUTE_ENABLED", "1")
    prov = _provider(db_session)
    p = _persist_plan(db_session, [
        {"kind": "order_node",
         "params": {"provider_id": prov.id, "count": 2, "region": "de", "plan": "cx01"}},
    ])
    monkeypatch.setattr(
        ops_execution, "_preflight",
        lambda db, v: {"ok": True, "reasons": [], "total_cost": 1180.0, "step_costs": {0: 1180.0}},
    )
    monkeypatch.setattr(ops_execution, "_exec_order_node", lambda db, step, plan_id: ([101, 102], None))
    res = ops_execution.execute_plan(db_session, p)
    assert res["status"] == "executed"
    db_session.refresh(p)
    assert p.status == "executed"
    assert p.execution["steps"][0]["created_node_ids"] == [101, 102]


def test_execute_partial_on_unsupported_kind(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPS_EXECUTE_ENABLED", "1")
    prov = _provider(db_session)
    p = _persist_plan(db_session, [
        {"kind": "order_node",
         "params": {"provider_id": prov.id, "count": 1, "region": "de", "plan": "cx01"}},
        {"kind": "set_active", "params": {}},
    ])
    monkeypatch.setattr(
        ops_execution, "_preflight",
        lambda db, v: {"ok": True, "reasons": [], "total_cost": 590.0, "step_costs": {0: 590.0}},
    )
    monkeypatch.setattr(ops_execution, "_exec_order_node", lambda db, step, plan_id: ([201], None))
    res = ops_execution.execute_plan(db_session, p)
    # исполнимая часть (order) прошла → executed; skipped помечены, не downgrade'ят.
    assert res["status"] == "executed"
    db_session.refresh(p)
    statuses = {s["kind"]: s["status"] for s in p.execution["steps"]}
    assert statuses["order_node"] == "done"
    assert statuses["set_active"] == "skipped"
    assert p.execution["skipped"] == 1


def test_execute_partial_failure_keeps_created_ids(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Сбой на середине заказа: уже оплаченные ноды должны остаться в отчёте.
    monkeypatch.setenv("OPS_EXECUTE_ENABLED", "1")
    prov = _provider(db_session)
    p = _persist_plan(db_session, [
        {"kind": "order_node",
         "params": {"provider_id": prov.id, "count": 3, "region": "de", "plan": "cx01"}},
    ])
    monkeypatch.setattr(
        ops_execution, "_preflight",
        lambda db, v: {"ok": True, "reasons": [], "total_cost": 1770.0, "step_costs": {0: 1770.0}},
    )
    # заказала 1, упала на 2-й — created=[301], error
    monkeypatch.setattr(ops_execution, "_exec_order_node", lambda db, step, plan_id: ([301], "DriverError: boom"))
    res = ops_execution.execute_plan(db_session, p)
    assert res["status"] == "failed"
    db_session.refresh(p)
    step0 = p.execution["steps"][0]
    assert step0["status"] == "failed"
    assert step0["created_node_ids"] == [301]  # оплаченная нода не потеряна


def test_validate_order_bad_pool_id_rejected(db_session: Session) -> None:
    prov = _provider(db_session)
    res = ops_execution.validate_plan(
        db_session,
        _ops_plan([{"kind": "order_node",
                    "params": {"provider_id": prov.id, "count": 1, "pool_id": 999999}}]),
    )
    assert not res["ok"]
    assert any("pool_id" in r for r in res["rejections"])
