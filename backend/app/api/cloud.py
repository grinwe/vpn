"""Cloud provider admin endpoints: ``/api/cloud/providers/*``.

CRUD for Hetzner/DigitalOcean/... API credentials we use to spawn nodes
on demand. Tokens are stored encrypted via ``security.encrypt``; the
create/update routes accept plaintext tokens and hand them to the
encryptor, the responses never include the token back.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..security import encrypt as _encrypt
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


class CloudProviderUpdate(BaseModel):
    name: str | None = None
    default_image: str | None = None
    default_region: str | None = None
    default_plan: str | None = None
    ssh_key_ids: list[str] | None = None
    is_active: bool | None = None
    api_token: str | None = None


@router.post("/cloud/providers", response_model=schemas.CloudProviderOut)
def create_cloud_provider(
    payload: schemas.CloudProviderCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    try:
        kind = models.CloudProviderKind(payload.kind)
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Unknown provider kind") from exc

    provider = models.CloudProvider(
        name=payload.name,
        kind=kind,
        api_token_enc=_encrypt(payload.api_token),
        default_image=payload.default_image,
        default_region=payload.default_region,
        default_plan=payload.default_plan,
        ssh_key_ids=payload.ssh_key_ids,
        is_active=payload.is_active,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "cloud_provider_created", "cloud_provider", provider.id, actor_type=actor_type)
    return schemas.CloudProviderOut(
        id=provider.id,
        name=provider.name,
        kind=provider.kind.value,
        default_image=provider.default_image,
        default_region=provider.default_region,
        default_plan=provider.default_plan,
        ssh_key_ids=provider.ssh_key_ids,
        is_active=provider.is_active,
        created_at=provider.created_at,
    )


@router.get("/cloud/providers", response_model=list[schemas.CloudProviderOut])
def list_cloud_providers(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    rows = db.query(models.CloudProvider).order_by(models.CloudProvider.id).all()
    return [
        schemas.CloudProviderOut(
            id=p.id,
            name=p.name,
            kind=p.kind.value,
            default_image=p.default_image,
            default_region=p.default_region,
            default_plan=p.default_plan,
            ssh_key_ids=p.ssh_key_ids,
            is_active=p.is_active,
            created_at=p.created_at,
        )
        for p in rows
    ]


@router.get(
    "/cloud/providers/{provider_id}/offerings",
    response_model=schemas.ProviderOfferingsOut,
)
def provider_offerings(
    provider_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Datacenters / tariffs / OS-images провайдера для admin-формы заказа.
    Делает live-запросы к API провайдера. Драйверы, не умеющие конкретный
    список (capability-проверка через hasattr), отдают пустой список."""
    from ..services.cloud.base import DriverError, get_driver

    provider = db.get(models.CloudProvider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    try:
        driver = get_driver(provider)
    except DriverError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _safe(method: str) -> list[dict]:
        fn = getattr(driver, method, None)
        if not callable(fn):
            return []
        try:
            return fn() or []
        except DriverError as exc:
            raise HTTPException(
                status_code=502, detail=f"{provider.kind.value} {method}: {exc}"
            ) from exc

    return schemas.ProviderOfferingsOut(
        datacenters=_safe("list_datacenters"),
        plans=_safe("list_plans"),
        images=_safe("list_images"),
    )


@router.patch("/cloud/providers/{provider_id}", response_model=schemas.CloudProviderOut)
def update_cloud_provider(
    provider_id: int,
    payload: CloudProviderUpdate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    prov = db.get(models.CloudProvider, provider_id)
    if not prov:
        raise HTTPException(status_code=404, detail="Provider not found")
    if payload.name is not None:
        prov.name = payload.name
    if payload.default_image is not None:
        prov.default_image = payload.default_image
    if payload.default_region is not None:
        prov.default_region = payload.default_region
    if payload.default_plan is not None:
        prov.default_plan = payload.default_plan
    if payload.ssh_key_ids is not None:
        prov.ssh_key_ids = payload.ssh_key_ids
    if payload.is_active is not None:
        prov.is_active = payload.is_active
    if payload.api_token is not None:
        prov.api_token_enc = _encrypt(payload.api_token)
    db.commit()
    db.refresh(prov)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "cloud_provider_updated", "cloud_provider", prov.id, actor_type=actor_type)
    return schemas.CloudProviderOut(
        id=prov.id, name=prov.name, kind=prov.kind.value,
        default_image=prov.default_image, default_region=prov.default_region,
        default_plan=prov.default_plan, ssh_key_ids=prov.ssh_key_ids,
        is_active=prov.is_active, created_at=prov.created_at,
    )


@router.delete("/cloud/providers/{provider_id}", status_code=200)
def delete_cloud_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    prov = db.get(models.CloudProvider, provider_id)
    if not prov:
        raise HTTPException(status_code=404, detail="Provider not found")
    linked = db.query(models.VPNNode).filter(models.VPNNode.provider_id == prov.id).count()
    if linked > 0:
        raise HTTPException(status_code=409, detail=f"Provider has {linked} linked node(s)")
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "cloud_provider_deleted", "cloud_provider", prov.id, actor_type=actor_type)
    db.delete(prov)
    db.commit()
    return {"provider_id": provider_id, "deleted": True}
