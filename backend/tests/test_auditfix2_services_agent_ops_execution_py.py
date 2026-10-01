"""Audit-fix волна 2 для ``services/agent/ops_execution.py``.

Находки:
* #121 — pre-flight ловил только DriverError; кривая env-переменная
  (``OPS_MAX_SPEND_RUB=""``) или нечисловая цена от провайдера роняла
  необработанным исключением → план залипал в ``executing``.
* #122 — ``count=0`` / нечисловой count из плана LLM молча становился
  заказом 1 ноды вместо отклонения плана.
* #123 — ``_exec_order_node`` не откатывал сессию после сбоя на
  ``db.commit()``; последующий финальный commit статуса плана падал
  ``PendingRollbackError`` и план залипал в ``executing``.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy.orm import Session

from app import models
from app.security import encrypt
from app.services import node_spawner
from app.services.agent import ops_execution


def _provider(db: Session) -> models.CloudProvider:
    p = models.CloudProvider(
        name="4vps-audit2", kind=models.CloudProviderKind.fourvps,
        api_token_enc=encrypt("1:key"), is_active=True,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _persist_plan(db: Session, steps: list[dict]) -> models.OpsPlan:
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


# ── #121: устойчивость env-кноба и pre-flight ────────────────────────────────

def test_max_spend_rub_empty_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """OPS_MAX_SPEND_RUB="" (set-but-empty) не роняет ValueError, а даёт дефолт."""
    monkeypatch.setenv("OPS_MAX_SPEND_RUB", "")
    assert ops_execution._max_spend_rub() == 5000.0
    monkeypatch.setenv("OPS_MAX_SPEND_RUB", "мусор")
    assert ops_execution._max_spend_rub() == 5000.0


def _validated_order(pid: int, plan: str = "cx01", region: str = "de") -> dict:
    return {
        "steps": [{
            "index": 0, "kind": "order_node",
            "resolved": {"provider_id": pid, "count": 1, "plan": plan, "region": region},
        }],
    }


def test_preflight_non_driver_exception_is_reason(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Любой сбой драйвера (не DriverError) — это reason отказа, а не краш."""
    prov = _provider(db_session)
    from app.services import cloud

    def boom(_provider):
        raise RuntimeError("kaboom secret=abc")

    monkeypatch.setattr(cloud, "get_driver", boom)
    out = ops_execution._preflight(db_session, _validated_order(prov.id))
    assert out["ok"] is False
    assert any("pre-flight упал" in r for r in out["reasons"])


def test_preflight_non_numeric_price_is_reason(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нечисловая цена тарифа из API провайдера — reason отказа, не ValueError."""
    prov = _provider(db_session)
    from app.services import cloud

    class _FakeDriver:
        def list_plans(self):
            return [{"id": "cx01", "price": "не-число"}]

        def list_datacenters(self):
            return [{"id": "de"}]

        def get_balance(self):
            return 100000.0

    monkeypatch.setattr(cloud, "get_driver", lambda _p: _FakeDriver())
    out = ops_execution._preflight(db_session, _validated_order(prov.id))
    assert out["ok"] is False
    assert any("нечисловая цена" in r for r in out["reasons"])


# ── #122: битый/нулевой count отклоняется, а не коэрсится в 1 ─────────────────

@pytest.mark.parametrize("bad_count", [0, "два", True])
def test_validate_rejects_bad_count(
    db_session: Session, bad_count
) -> None:
    prov = _provider(db_session)
    step = {"kind": "order_node", "params": {
        "provider_id": prov.id, "count": bad_count, "region": "de", "plan": "cx01"}}
    p = _persist_plan(db_session, [step])
    out = ops_execution.validate_plan(db_session, p)
    assert out["ok"] is False
    # count=0 отклоняется как «вне диапазона», нечисловой — как «не число».
    assert any("count" in r for r in out["rejections"])


def test_validate_absent_count_defaults_to_one(db_session: Session) -> None:
    """count не задан → дефолт 1, план валиден (обратная совместимость)."""
    prov = _provider(db_session)
    step = {"kind": "order_node", "params": {
        "provider_id": prov.id, "region": "de", "plan": "cx01"}}
    p = _persist_plan(db_session, [step])
    out = ops_execution.validate_plan(db_session, p)
    assert out["ok"] is True
    assert out["steps"][0]["resolved"]["count"] == 1


# ── #123: rollback после сбоя заказа не срывает финальный commit ──────────────

def test_order_failure_rolls_back_and_plan_marked_failed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn падает IntegrityError на unique-имени → сессия откатывается,
    план получает статус failed (не PendingRollbackError → залипание)."""
    monkeypatch.setenv("OPS_EXECUTE_ENABLED", "1")
    monkeypatch.setattr(
        ops_execution, "_preflight",
        lambda db, v: {"ok": True, "reasons": [], "total_cost": 0.0, "step_costs": {0: 0.0}},
    )
    monkeypatch.setattr(
        node_spawner, "resolve_spawn_name",
        lambda db, pid, region, x: "dup-node-audit2",
    )
    prov = _provider(db_session)

    # Пред-создаём ноду с тем же именем — spawn воткнётся в unique-констрейнт.
    db_session.add(models.VPNNode(
        name="dup-node-audit2", region="ru",
        host=node_spawner.SPAWN_PLACEHOLDER_HOST,
        status=models.VPNNodeStatus.registering, is_active=False,
    ))
    db_session.commit()

    def fake_spawn(db, **kwargs):
        # Как настоящий spawn: db.add + commit → IntegrityError (гонка имён),
        # оставляет сессию в pending-rollback.
        db.add(models.VPNNode(
            name=kwargs["name"], region=kwargs["region"],
            host=node_spawner.SPAWN_PLACEHOLDER_HOST,
            status=models.VPNNodeStatus.registering, is_active=False,
        ))
        db.commit()  # unique violation
        raise AssertionError("commit должен был бросить")

    monkeypatch.setattr(node_spawner, "spawn_node_async", fake_spawn)

    step = {"kind": "order_node", "params": {
        "provider_id": prov.id, "count": 1, "region": "de", "plan": "cx01"}}
    p = _persist_plan(db_session, [step])

    # Не должно быть необработанного PendingRollbackError.
    res = ops_execution.execute_plan(db_session, p)
    assert res["status"] == "failed"
    db_session.refresh(p)
    assert p.status == "failed"
    # Отчёт о шаге записан (терминальный статус + execution сохранены).
    assert p.execution["phase"] == "done"
    assert any(s["status"] == "failed" for s in p.execution["steps"])
