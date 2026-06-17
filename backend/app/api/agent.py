"""AI-агент: эндпоинты ``/api/agent/*``.

Фаза 1 (AI_AGENT_ROADMAP.md) — read-only диагностический триаж ноды. За флагом
AGENT_ENABLED; требует ANTHROPIC_API_KEY. Мутаций инфры нет. Действие
логируется в audit_logs (метит, что триаж выполнял агент).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from slowapi.util import get_remote_address
from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import models
from ..auth import require_admin
from ..rate_limit import limiter
from ..time_utils import utcnow
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


def _ops_actor_key(request: Request) -> str:
    """Ключ рейт-лимита для агент-эндпоинтов — по X-Admin-Actor (TG-id), не по IP:
    весь трафик бота приходит с одного backend-контейнера, поэтому IP-ключ
    схлопывает всех операторов в один бакет и бесполезен как per-actor лимит."""
    return request.headers.get(ADMIN_ACTOR_HEADER) or get_remote_address(request)


class OpsPlanRequest(BaseModel):
    command: str


@router.post("/agent/triage/{node_id}")
@limiter.limit("30/minute")  # глобальный потолок на источник
@limiter.limit("6/minute", key_func=_ops_actor_key)  # на оператора
def agent_triage_node(
    node_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Read-only диагностический триаж ноды AI-агентом: собирает
    health/traffic/configs/provisioning и выдаёт root-cause + рекомендацию
    (ничего не выполняет). За флагом AGENT_ENABLED + ANTHROPIC_API_KEY."""
    from ..services.agent.triage import AgentError, triage_node

    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        result = triage_node(db, node_id)
    except AgentError as exc:
        # disabled / нет ключа / LLM-сбой / не сошлось — сервис недоступен/не готов.
        raise HTTPException(status_code=503, detail=f"agent triage: {exc}") from exc

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "agent_node_triaged",
        "vpn_node",
        node_id,
        actor_type=actor_type,
        metadata={
            "model": result.get("model"),
            "tool_calls": result.get("tool_calls"),
            "iterations": result.get("iterations"),
        },
    )
    db.commit()
    return result


@router.post("/agent/ops/plan")
@limiter.limit("30/minute")  # глобальный потолок на источник (бот = один IP)
@limiter.limit("6/minute", key_func=_ops_actor_key)  # на оператора
def agent_ops_plan(
    payload: OpsPlanRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Ops-планировщик (dry-run): NL-команда → структурированный план с оценкой
    стоимости/влияния. НИЧЕГО НЕ ВЫПОЛНЯЕТ — только читает состояние флота и
    предлагает шаги. За флагом AGENT_ENABLED + ANTHROPIC_API_KEY. Выполнение
    плана — отдельная фаза (за одним подтверждением)."""
    from ..services.agent.ops import AgentError, plan_ops

    try:
        result = plan_ops(db, payload.command)
    except AgentError as exc:
        raise HTTPException(status_code=503, detail=f"agent ops plan: {exc}") from exc

    plan = result.get("plan") or {}
    actor, actor_type = _resolve_admin_actor(admin_actor)

    # Персист всего плана: полный аудит-след + фундамент Phase 3 confirm-binding
    # (исполнение обязано ссылаться на план по id+content_hash, чтобы исполнялось
    # ровно то, что подтвердил оператор — без params от клиента и без переплана).
    canonical = json.dumps(plan, sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    ttl_min = max(1, int(os.getenv("OPS_PLAN_TTL_MIN", "60")))
    ops_plan = models.OpsPlan(
        actor=actor,
        command=payload.command,
        model=result.get("model"),
        plan=plan,
        content_hash=content_hash,
        feasible=plan.get("feasible"),
        needs_confirmation=plan.get("needs_confirmation"),
        status="proposed",
        expires_at=utcnow() + timedelta(minutes=ttl_min),
    )
    db.add(ops_plan)
    db.flush()  # получить id до записи аудита

    _audit(
        db,
        actor,
        "agent_ops_planned",
        "ops_plan",
        ops_plan.id,
        actor_type=actor_type,
        metadata={
            "command": payload.command[:500],
            "model": result.get("model"),
            "tool_calls": result.get("tool_calls"),
            "iterations": result.get("iterations"),
            "feasible": plan.get("feasible"),
            "needs_confirmation": plan.get("needs_confirmation"),
            "steps": len(plan.get("steps") or []),
            "plan_id": ops_plan.id,
            "content_hash": content_hash,
        },
    )
    db.commit()

    # Отдаём ссылку на сохранённый план — будущий confirm-эндпоинт примет ТОЛЬКО
    # plan_id (+ свериться по content_hash), а не сырой план от клиента.
    result["plan_id"] = ops_plan.id
    result["content_hash"] = content_hash
    result["expires_at"] = ops_plan.expires_at.isoformat()
    return result


class OpsExecuteRequest(BaseModel):
    plan_id: int


@router.post("/agent/ops/execute")
@limiter.limit("30/minute")
@limiter.limit("6/minute", key_func=_ops_actor_key)
def agent_ops_execute(
    payload: OpsExecuteRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Исполнить РАНЕЕ СОХРАНЁННЫЙ план по ``plan_id`` (Phase 3, за флагом
    OPS_EXECUTE_ENABLED). Принимает ТОЛЬКО ``plan_id`` — никаких params от клиента
    (исполняется ровно то, что планировщик сохранил и оператор подтвердил).

    Синхронно: грузит план → проверяет флаг/статус/TTL → ре-валидирует (быстрый
    отказ до постановки в очередь) → армит (status=executing) → энкьюит RQ-джобу
    ``run_ops_plan_execute``. Тяжёлая работа (pre-flight по живым ценам/балансу,
    заказ нод) — в воркере. Денежные/destructive гарды — в валидаторе и pre-flight,
    НЕ здесь."""
    from ..services.agent.ops_execution import execute_enabled, validate_plan

    if not execute_enabled():
        raise HTTPException(
            status_code=503, detail="исполнение планов выключено (OPS_EXECUTE_ENABLED=0)"
        )

    plan = db.get(models.OpsPlan, payload.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan.status != "proposed":
        raise HTTPException(
            status_code=409, detail=f"план в статусе {plan.status} — исполнить нельзя"
        )
    if plan.expires_at and plan.expires_at < utcnow():
        plan.status = "expired"
        db.commit()
        raise HTTPException(status_code=409, detail="план протух (TTL) — построй заново")

    # Быстрый отказ до очереди: ре-валидация (флот мог измениться).
    validated = validate_plan(db, plan)
    if not validated["ok"]:
        raise HTTPException(
            status_code=422,
            detail="план не исполним: " + "; ".join(validated["rejections"][:5]),
        )

    actor, actor_type = _resolve_admin_actor(admin_actor)

    # АТОМАРНЫЙ арминг: условный UPDATE proposed→executing под row-lock. Это
    # единственная защита от двойного заказа — in-Python read-check-write под
    # READ COMMITTED гонится (два параллельных POST оба видят proposed). rowcount=0
    # → план уже армлен/исполнен/протух → 409. (См. ops-executor-review: CRITICAL.)
    armed = db.execute(
        update(models.OpsPlan)
        .where(models.OpsPlan.id == plan.id, models.OpsPlan.status == "proposed")
        .values(status="executing")
    ).rowcount
    if not armed:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="план уже исполняется или исполнен (гонка)"
        )
    _audit(
        db, actor, "agent_ops_execute_armed", "ops_plan", plan.id,
        actor_type=actor_type,
        metadata={
            "content_hash": plan.content_hash,
            "needs_confirmation": validated["needs_confirmation"],
            "order_count": validated["totals"]["order_count"],
            "destructive": validated["totals"]["destructive"],
        },
    )  # _audit коммитит арм+аудит одной транзакцией

    def _disarm() -> None:
        db.execute(
            update(models.OpsPlan)
            .where(models.OpsPlan.id == plan.id, models.OpsPlan.status == "executing")
            .values(status="proposed")
        )
        db.commit()

    from ..queue import get_queue

    queue = get_queue()
    if queue is None:
        _disarm()  # иначе план застрянет executing навсегда
        raise HTTPException(status_code=503, detail="очередь недоступна")
    try:
        # Детерминированный job_id — RQ схлопывает дубли (belt-and-suspenders к
        # атомарному армингу против at-least-once доставки).
        job = queue.enqueue(
            "app.worker.run_ops_plan_execute",
            plan.id,
            job_id=f"ops-exec-{plan.id}",
            job_timeout=1800,
        )
    except Exception as exc:  # noqa: BLE001
        _disarm()
        raise HTTPException(
            status_code=503, detail=f"не удалось поставить job: {exc}"
        ) from exc

    return {"status": "enqueued", "plan_id": plan.id, "job_id": job.id}
