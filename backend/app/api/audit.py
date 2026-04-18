"""Audit log read endpoint: ``GET /api/audit-logs``."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ._common import get_db

router = APIRouter()


class AuditLogListResponse(BaseModel):
    items: list[schemas.AuditLogOut]
    total: int
    has_more: bool


@router.get("/audit-logs", response_model=AuditLogListResponse)
def list_audit_logs(
    action: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    q = db.query(models.AuditLog)
    if action:
        q = q.filter(models.AuditLog.action == action)
    if target_type:
        q = q.filter(models.AuditLog.target_type == target_type)
    if actor:
        q = q.filter(models.AuditLog.actor.ilike(f"%{actor}%"))
    total = q.count()
    rows = q.order_by(models.AuditLog.created_at.desc()).offset(offset).limit(limit).all()
    return AuditLogListResponse(
        items=rows,
        total=total,
        has_more=(offset + limit) < total,
    )
