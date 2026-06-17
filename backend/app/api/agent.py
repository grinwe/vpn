"""AI-агент: эндпоинты ``/api/agent/*``.

Фаза 1 (AI_AGENT_ROADMAP.md) — read-only диагностический триаж ноды. За флагом
AGENT_ENABLED; требует ANTHROPIC_API_KEY. Мутаций инфры нет. Действие
логируется в audit_logs (метит, что триаж выполнял агент).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from .. import models
from ..auth import require_admin
from ..rate_limit import limiter
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
def agent_triage_node(
    node_id: int,
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
    _audit(
        db,
        actor,
        "agent_ops_planned",
        "ops_plan",
        0,
        actor_type=actor_type,
        metadata={
            "command": payload.command[:500],
            "model": result.get("model"),
            "tool_calls": result.get("tool_calls"),
            "iterations": result.get("iterations"),
            "feasible": plan.get("feasible"),
            "needs_confirmation": plan.get("needs_confirmation"),
            "steps": len(plan.get("steps") or []),
        },
    )
    db.commit()
    return result
