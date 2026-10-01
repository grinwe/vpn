"""Scoped API tokens admin CRUD: ``/api/api-tokens``."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import ALL_SCOPES, generate_token, require_admin
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


@router.get("/api-tokens", response_model=list[schemas.ApiTokenOut])
def list_api_tokens(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """List all scoped API tokens (hashes never leave the DB)."""
    rows = db.query(models.ApiToken).order_by(models.ApiToken.id).all()
    return [schemas.ApiTokenOut.from_orm(r) for r in rows]


@router.post(
    "/api-tokens",
    response_model=schemas.ApiTokenCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
def create_api_token(
    payload: schemas.ApiTokenCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Mint a new scoped token.

    The plaintext is returned exactly once in the response body — it is
    never stored and cannot be recovered later. Validate scopes against
    the canonical list so a typo in a curl call doesn't quietly create a
    token with zero capabilities.
    """
    bad = [s for s in payload.scopes if s not in ALL_SCOPES]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unknown scopes: {bad}. allowed: {list(ALL_SCOPES)}",
        )
    if db.query(models.ApiToken).filter(models.ApiToken.name == payload.name).first():
        raise HTTPException(status_code=409, detail="token with that name already exists")

    plaintext, token_hash = generate_token()
    row = models.ApiToken(
        name=payload.name,
        token_hash=token_hash,
        scopes=payload.scopes,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor=actor,
        action="api_token.create",
        target_type="api_token",
        target_id=row.id,
        metadata={"name": row.name, "scopes": row.scopes},
        actor_type=actor_type,
    )
    db.commit()

    out = schemas.ApiTokenCreatedOut(
        id=row.id,
        name=row.name,
        scopes=list(row.scopes or []),
        is_active=row.is_active,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        token=plaintext,
    )
    return out


@router.delete("/api-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_token(
    token_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Disable a token. Row is kept for audit continuity."""
    row = db.get(models.ApiToken, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="token not found")
    row.is_active = False
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor=actor,
        action="api_token.revoke",
        target_type="api_token",
        target_id=row.id,
        metadata={"name": row.name},
        actor_type=actor_type,
    )
    db.commit()
