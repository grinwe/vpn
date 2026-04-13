"""Tariff plans CRUD: ``/api/plans``."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ._common import get_db

router = APIRouter()


@router.get("/plans", response_model=list[schemas.PlanOut])
def list_plans(db: Session = Depends(get_db)):
    plans = db.query(models.Plan).order_by(models.Plan.id).all()
    return [schemas.PlanOut.from_orm(plan) for plan in plans]


@router.post("/plans", response_model=schemas.PlanOut, status_code=201)
def create_plan(
    payload: schemas.PlanCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    existing = db.query(models.Plan).filter(models.Plan.name == payload.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Plan with this name already exists")
    plan = models.Plan(**payload.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return schemas.PlanOut.from_orm(plan)


@router.put("/plans/{plan_id}", response_model=schemas.PlanOut)
def update_plan(
    plan_id: int,
    payload: schemas.PlanUpdate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    plan = db.get(models.Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"] != plan.name:
        clash = db.query(models.Plan).filter(models.Plan.name == updates["name"]).first()
        if clash:
            raise HTTPException(status_code=409, detail="Plan with this name already exists")
    for field, value in updates.items():
        setattr(plan, field, value)
    db.commit()
    db.refresh(plan)
    return schemas.PlanOut.from_orm(plan)


@router.delete("/plans/{plan_id}", status_code=204)
def delete_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    plan = db.get(models.Plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    # Refuse to delete a plan that still has subscriptions — soft-hide with
    # is_visible=false instead. This is cheaper than cascading and avoids
    # orphaning historical records.
    has_subs = (
        db.query(models.Subscription)
        .filter(models.Subscription.plan_id == plan_id)
        .first()
    )
    if has_subs:
        raise HTTPException(
            status_code=409,
            detail="Plan has existing subscriptions; set is_visible=false instead",
        )
    db.delete(plan)
    db.commit()
    return None
