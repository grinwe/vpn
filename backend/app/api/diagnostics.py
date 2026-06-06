"""Admin diagnose-incident controls — generic over node|exit targets.

Backs the bot inline buttons (callback ``diag:<action>:<kind>:<id>``) and the
admin UI toggles. ``kind`` ∈ {'node','exit'} → ``VPNNode`` | ``WGExitNode``
(both carry the migration-0039 ``diagnose_*`` columns). All ``require_admin``.

Two INDEPENDENT toggles + the per-incident actions live here:
  * disable/enable — hard-stop ALL diagnose tasks for the target;
  * mute (N hours / forever / unmute) — silence admin alerts only;
  * ack — "вижу, работаю", stop auto re-diagnosis for the open incident;
  * follow — opt into exponential 30m→2h→6h re-diagnosis.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..auth import require_admin
from ..services import diagnostics_state
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()

_KIND_MODEL = {"node": models.VPNNode, "exit": models.WGExitNode}


def _resolve_target(db: Session, kind: str, target_id: int):
    model = _KIND_MODEL.get(kind)
    if model is None:
        raise HTTPException(status_code=400, detail=f"unknown target kind: {kind!r}")
    row = db.get(model, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{kind} #{target_id} not found")
    return row


def _label(target) -> str:
    return getattr(target, "name", None) or str(getattr(target, "id", "?"))


def _state(target) -> dict:
    """Snapshot the diagnose state for the response (and the UI)."""
    return {
        "diagnostics_disabled_at": getattr(target, "diagnostics_disabled_at", None),
        "alerts_muted_until": getattr(target, "alerts_muted_until", None),
        "diagnose_incident_open_at": getattr(target, "diagnose_incident_open_at", None),
        "diagnose_follow_mode": getattr(target, "diagnose_follow_mode", None),
        "diagnose_acked_at": getattr(target, "diagnose_acked_at", None),
        "last_diagnosed_at": getattr(target, "last_diagnosed_at", None),
    }


class MuteRequest(BaseModel):
    # >0 = mute N hours; <0 = forever ("совсем"); 0 = unmute.
    hours: int = 1


class FollowRequest(BaseModel):
    mode: str = "exponential"  # 'exponential' | 'once'


@router.post("/diagnostics/{kind}/{target_id}/disable")
def diagnostics_disable(
    kind: str,
    target_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Hard-stop ALL diagnose tasks (auto + manual) for this target."""
    target = _resolve_target(db, kind, target_id)
    diagnostics_state.set_diagnostics_disabled(target, True)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "diagnostics_disabled", kind, target_id, actor_type=actor_type)
    return {"kind": kind, "id": target_id, **_state(target)}


@router.post("/diagnostics/{kind}/{target_id}/enable")
def diagnostics_enable(
    kind: str,
    target_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Re-enable diagnostics. Also clears the legacy combined mute flag."""
    target = _resolve_target(db, kind, target_id)
    diagnostics_state.set_diagnostics_disabled(target, False)
    # Clear the legacy flag too so a re-enable is a clean slate.
    if getattr(target, "auto_diagnose_disabled_at", None) is not None:
        target.auto_diagnose_disabled_at = None
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "diagnostics_enabled", kind, target_id, actor_type=actor_type)
    return {"kind": kind, "id": target_id, **_state(target)}


@router.post("/diagnostics/{kind}/{target_id}/mute")
def diagnostics_mute(
    kind: str,
    target_id: int,
    body: MuteRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Silence admin alerts for N hours (<0 = forever, 0 = unmute)."""
    target = _resolve_target(db, kind, target_id)
    until = diagnostics_state.mute_alerts(target, body.hours)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "diagnose_alerts_muted", kind, target_id,
        actor_type=actor_type, metadata={"hours": body.hours},
    )
    return {"kind": kind, "id": target_id, "alerts_muted_until": until, **_state(target)}


@router.post("/diagnostics/{kind}/{target_id}/ack")
def diagnostics_ack(
    kind: str,
    target_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """«Вижу, работаю» — stop auto re-diagnosis for the open incident."""
    target = _resolve_target(db, kind, target_id)
    diagnostics_state.ack_incident(target)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "diagnose_incident_acked", kind, target_id, actor_type=actor_type)
    return {"kind": kind, "id": target_id, "label": _label(target), **_state(target)}


@router.post("/diagnostics/{kind}/{target_id}/follow")
def diagnostics_follow(
    kind: str,
    target_id: int,
    body: FollowRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Opt into exponential 30m→2h→6h re-diagnosis (or back to 'once')."""
    mode = body.mode if body.mode in ("exponential", "once") else "exponential"
    target = _resolve_target(db, kind, target_id)
    diagnostics_state.set_follow_mode(target, mode if mode == "exponential" else None)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "diagnose_follow_set", kind, target_id,
        actor_type=actor_type, metadata={"mode": mode},
    )
    return {"kind": kind, "id": target_id, "follow_mode": mode, **_state(target)}
