"""Autoscale admin endpoints: ``/api/pools/{id}/autoscale`` and ``/api/autoscale/tick``.

Thin wrapper around ``services.autoscale.evaluate_all_pools`` — the
background worker loop drives the real autoscale cadence via
``AUTOSCALE_INTERVAL``; these routes exist for smoke tests and manual
operator ticks from the admin UI.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..services.autoscale import evaluate_all_pools
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


def _pool_to_autoscale_out(pool: models.ServerPool) -> schemas.PoolAutoscaleOut:
    return schemas.PoolAutoscaleOut(
        pool_id=pool.id,
        pool_name=pool.name,
        autoscale_enabled=bool(pool.autoscale_enabled),
        autoscale_provider_id=pool.autoscale_provider_id,
        autoscale_region=pool.autoscale_region,
        autoscale_plan=pool.autoscale_plan,
        autoscale_image=pool.autoscale_image,
        autoscale_high_watermark=(
            float(pool.autoscale_high_watermark)
            if pool.autoscale_high_watermark is not None
            else None
        ),
        autoscale_max_nodes=pool.autoscale_max_nodes,
    )


@router.put("/pools/{pool_id}/autoscale", response_model=schemas.PoolAutoscaleOut)
def update_pool_autoscale(
    pool_id: int,
    body: schemas.PoolAutoscaleConfig,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    pool = db.get(models.ServerPool, pool_id)
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")

    for field in (
        "autoscale_enabled",
        "autoscale_provider_id",
        "autoscale_region",
        "autoscale_plan",
        "autoscale_image",
        "autoscale_high_watermark",
        "autoscale_max_nodes",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(pool, field, value)

    db.add(pool)
    db.commit()
    db.refresh(pool)

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "pool_autoscale_updated", "server_pool", pool.id, actor_type=actor_type)
    return _pool_to_autoscale_out(pool)


@router.post("/autoscale/tick", response_model=list[schemas.PoolDecisionOut])
def autoscale_tick(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Run one autoscale evaluation synchronously.

    Intended for smoke tests and manual operator runs. The periodic loop
    runs inside the worker process (``AUTOSCALE_INTERVAL`` env var) — this
    endpoint bypasses the queue and reports decisions right away.
    """
    decisions = evaluate_all_pools(db)
    return [
        schemas.PoolDecisionOut(
            pool_id=d.pool_id,
            pool_name=d.pool_name,
            utilization=d.utilization,
            total_capacity=d.total_capacity,
            active_subs=d.active_subs,
            node_count=d.node_count,
            scaled_up=d.scaled_up,
            new_node_id=d.new_node_id,
            reason=d.reason,
        )
        for d in decisions
    ]
