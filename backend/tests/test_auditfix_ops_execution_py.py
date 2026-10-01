"""Audit-fix #119: execute_plan (RQ-джоба) обязан ДОЖДАТЬСЯ daemon-потоков
``_finalize_spawn`` перед возвратом — иначе RQ work-horse завершает процесс и
убивает достройку (нода вечно в registering с placeholder-host, деньги списаны).

Здесь spawn_node_async замокан: создаёт VPNNode и стартует daemon-поток —
как настоящий. Проверяем, что execute_plan:
1) возвращается только ПОСЛЕ завершения потока достройки (happy path);
2) при превышении бюджета OPS_FINALIZE_WAIT_SEC честно даунгрейдит статус в
   ``partial`` и пишет ``finalize_pending`` в execution;
3) дожидается потоков уже заказанных нод даже при сбое на середине шага.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest
from sqlalchemy.orm import Session

from app import models
from app.security import encrypt
from app.services import node_spawner
from app.services.agent import ops_execution


def _provider(db: Session) -> models.CloudProvider:
    p = models.CloudProvider(
        name="4vps-audit119", kind=models.CloudProviderKind.fourvps,
        api_token_enc=encrypt("1:key"), is_active=True,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _persist_plan_hashed(db: Session, steps: list[dict]) -> models.OpsPlan:
    """План с ПРАВИЛЬНЫМ content_hash (integrity-гейт execute_plan должен пройти)."""
    plan = {"feasible": True, "summary": "s", "steps": steps}
    canonical = json.dumps(plan, sort_keys=True, ensure_ascii=False)
    p = models.OpsPlan(
        actor="1", command="c", model="m", plan=plan,
        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        status="proposed",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _mk_node(db: Session, name: str) -> models.VPNNode:
    node = models.VPNNode(
        name=name, region="ru", host=node_spawner.SPAWN_PLACEHOLDER_HOST,
        status=models.VPNNodeStatus.registering, is_active=False,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_EXECUTE_ENABLED", "1")
    monkeypatch.setattr(
        ops_execution, "_preflight",
        lambda db, v: {"ok": True, "reasons": [], "total_cost": 590.0, "step_costs": {0: 590.0}},
    )
    monkeypatch.setattr(
        node_spawner, "resolve_spawn_name",
        lambda db, pid, region, x: f"auto-{time.monotonic_ns()}",
    )


def _order_step(provider_id: int, count: int = 1) -> dict:
    return {
        "kind": "order_node",
        "params": {"provider_id": provider_id, "count": count, "region": "de", "plan": "cx01"},
    }


def test_execute_waits_for_finalize_thread(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Возврат из execute_plan — только после завершения потока достройки."""
    _patch_common(monkeypatch)
    prov = _provider(db_session)
    p = _persist_plan_hashed(db_session, [_order_step(prov.id)])

    finalize_done = threading.Event()

    def fake_spawn(db, **kwargs):
        node = _mk_node(db, kwargs["name"])

        def _finalize_spawn():  # имя как у настоящего — матчится фильтром по Thread.name
            time.sleep(0.3)
            finalize_done.set()

        threading.Thread(target=_finalize_spawn, daemon=True).start()
        return node

    monkeypatch.setattr(node_spawner, "spawn_node_async", fake_spawn)

    res = ops_execution.execute_plan(db_session, p)

    # Ключевая проверка: к моменту возврата достройка УЖЕ завершилась
    # (до фикса daemon-поток убивался бы вместе с work-horse).
    assert finalize_done.is_set()
    assert res["status"] == "executed"
    db_session.refresh(p)
    assert p.status == "executed"
    assert "finalize_pending" not in p.execution


def test_execute_finalize_timeout_marks_partial(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Достройка не успела в OPS_FINALIZE_WAIT_SEC → partial + finalize_pending."""
    _patch_common(monkeypatch)
    monkeypatch.setenv("OPS_FINALIZE_WAIT_SEC", "0.2")
    prov = _provider(db_session)
    p = _persist_plan_hashed(db_session, [_order_step(prov.id)])
    created_ids: list[int] = []

    def fake_spawn(db, **kwargs):
        node = _mk_node(db, kwargs["name"])
        created_ids.append(node.id)

        def _finalize_spawn():
            time.sleep(5)  # заведомо дольше бюджета

        threading.Thread(target=_finalize_spawn, daemon=True).start()
        return node

    monkeypatch.setattr(node_spawner, "spawn_node_async", fake_spawn)

    res = ops_execution.execute_plan(db_session, p)

    # Заказ прошёл, но достройка не подтверждена — НЕ рапортуем «executed».
    assert res["status"] == "partial"
    db_session.refresh(p)
    assert p.status == "partial"
    assert p.execution["finalize_pending"] == [{"index": 0, "node_ids": created_ids}]
    assert p.execution["steps"][0]["status"] == "done"  # сам заказ успешен


def test_execute_waits_threads_even_on_mid_step_failure(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой на 2-й ноде шага: поток достройки 1-й (оплаченной) всё равно дожидаемся."""
    _patch_common(monkeypatch)
    prov = _provider(db_session)
    p = _persist_plan_hashed(db_session, [_order_step(prov.id, count=2)])

    finalize_done = threading.Event()
    calls = {"n": 0}

    def fake_spawn(db, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("boom on second order")
        node = _mk_node(db, kwargs["name"])

        def _finalize_spawn():
            time.sleep(0.3)
            finalize_done.set()

        threading.Thread(target=_finalize_spawn, daemon=True).start()
        return node

    monkeypatch.setattr(node_spawner, "spawn_node_async", fake_spawn)

    res = ops_execution.execute_plan(db_session, p)

    assert finalize_done.is_set()  # достройку оплаченной ноды дождались
    assert res["status"] == "failed"
    db_session.refresh(p)
    step0 = p.execution["steps"][0]
    assert step0["status"] == "failed"
    assert len(step0["created_node_ids"]) == 1  # оплаченная нода не потеряна
