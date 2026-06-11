"""AI-агент: эндпоинты ``/api/agent/*``.

Фаза 1 (AI_AGENT_ROADMAP.md) — read-only диагностический триаж ноды. За флагом
AGENT_ENABLED; требует ANTHROPIC_API_KEY. Мутаций инфры нет. Действие
логируется в audit_logs (метит, что триаж выполнял агент).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..auth import require_admin
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


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
