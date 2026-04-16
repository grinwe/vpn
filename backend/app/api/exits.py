"""WireGuard exit-node admin endpoints: ``/api/exits/*``.

CRUD for foreign exit nodes that RU relay jump nodes tunnel to. See
``docs/RELAY_ROADMAP.md`` stage B for the architecture.

Private keys are stored encrypted via ``security.encrypt`` (same Fernet
scheme as cloud tokens). Responses expose ``wg_public_key`` but never
the private half — only ``has_private_key: bool`` to let the UI tell
whether keygen is needed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..security import encrypt as _encrypt
from ..services.vless import generate_wireguard_keypair
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


def _to_out(exit_node: models.WGExitNode) -> schemas.WGExitNodeOut:
    return schemas.WGExitNodeOut(
        id=exit_node.id,
        name=exit_node.name,
        region=exit_node.region,
        host=exit_node.host,
        ssh_port=exit_node.ssh_port,
        wg_port=exit_node.wg_port,
        wg_address_v4=exit_node.wg_address_v4,
        wg_public_key=exit_node.wg_public_key,
        has_private_key=exit_node.wg_private_key_enc is not None,
        provider_id=exit_node.provider_id,
        provider_external_id=exit_node.provider_external_id,
        provider_region=exit_node.provider_region,
        status=exit_node.status.value,
        is_active=exit_node.is_active,
        notes=exit_node.notes,
        created_at=exit_node.created_at,
        updated_at=exit_node.updated_at,
    )


@router.get("/exits", response_model=list[schemas.WGExitNodeOut])
def list_exits(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    rows = db.query(models.WGExitNode).order_by(models.WGExitNode.id).all()
    return [_to_out(r) for r in rows]


@router.post("/exits", response_model=schemas.WGExitNodeOut)
def create_exit(
    payload: schemas.WGExitNodeCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    if db.query(models.WGExitNode).filter(models.WGExitNode.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Exit node with this name already exists")

    exit_node = models.WGExitNode(
        name=payload.name,
        region=payload.region,
        host=payload.host,
        ssh_port=payload.ssh_port,
        wg_port=payload.wg_port,
        wg_address_v4=payload.wg_address_v4,
        wg_public_key=payload.wg_public_key,
        wg_private_key_enc=_encrypt(payload.wg_private_key) if payload.wg_private_key else None,
        provider_id=payload.provider_id,
        provider_external_id=payload.provider_external_id,
        provider_region=payload.provider_region,
        is_active=payload.is_active,
        notes=payload.notes,
    )
    db.add(exit_node)
    db.commit()
    db.refresh(exit_node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "wg_exit_created", "wg_exit_node", exit_node.id, actor_type=actor_type)
    return _to_out(exit_node)


@router.get("/exits/{exit_id}", response_model=schemas.WGExitNodeOut)
def get_exit(
    exit_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    return _to_out(exit_node)


@router.patch("/exits/{exit_id}", response_model=schemas.WGExitNodeOut)
def update_exit(
    exit_id: int,
    payload: schemas.WGExitNodePatch,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")

    if payload.name is not None:
        if (
            db.query(models.WGExitNode)
            .filter(models.WGExitNode.name == payload.name, models.WGExitNode.id != exit_id)
            .first()
        ):
            raise HTTPException(status_code=409, detail="Another exit node with this name exists")
        exit_node.name = payload.name
    if payload.region is not None:
        exit_node.region = payload.region
    if payload.host is not None:
        exit_node.host = payload.host
    if payload.ssh_port is not None:
        exit_node.ssh_port = payload.ssh_port
    if payload.wg_port is not None:
        exit_node.wg_port = payload.wg_port
    if payload.wg_address_v4 is not None:
        exit_node.wg_address_v4 = payload.wg_address_v4
    if payload.wg_public_key is not None:
        exit_node.wg_public_key = payload.wg_public_key
    if payload.wg_private_key is not None:
        exit_node.wg_private_key_enc = _encrypt(payload.wg_private_key)
    if payload.provider_id is not None:
        exit_node.provider_id = payload.provider_id
    if payload.provider_external_id is not None:
        exit_node.provider_external_id = payload.provider_external_id
    if payload.provider_region is not None:
        exit_node.provider_region = payload.provider_region
    if payload.status is not None:
        try:
            exit_node.status = models.WGExitNodeStatus(payload.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Unknown status") from exc
    if payload.is_active is not None:
        exit_node.is_active = payload.is_active
    if payload.notes is not None:
        exit_node.notes = payload.notes

    db.commit()
    db.refresh(exit_node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "wg_exit_updated", "wg_exit_node", exit_node.id, actor_type=actor_type)
    return _to_out(exit_node)


@router.delete("/exits/{exit_id}", status_code=200)
def delete_exit(
    exit_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "wg_exit_deleted", "wg_exit_node", exit_node.id, actor_type=actor_type)
    db.delete(exit_node)
    db.commit()
    return {"exit_id": exit_id, "deleted": True}


@router.post("/exits/{exit_id}/keygen", response_model=schemas.WGExitKeygenOut)
def keygen_exit(
    exit_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Generate a fresh X25519 WireGuard keypair for this exit.

    Overwrites any existing keys on the row. Returns only the public key
    — the private half is stored encrypted and never exposed via API.
    """
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    pub, priv = generate_wireguard_keypair()
    exit_node.wg_public_key = pub
    exit_node.wg_private_key_enc = _encrypt(priv)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "wg_exit_keygen", "wg_exit_node", exit_node.id, actor_type=actor_type)
    return schemas.WGExitKeygenOut(id=exit_node.id, wg_public_key=pub)
