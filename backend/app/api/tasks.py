"""Provisioning task admin endpoints: ``/api/provisioning/tasks/*``.

Task rows are first-class entities because half the admin UI lives on
top of them — progress banners, grouped migrations, the ability to
re-run a failed ansible job without recreating the originating
subscription.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..services.provisioning import ProvisioningOrchestrator
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

logger = logging.getLogger(__name__)

router = APIRouter()


def _enrich_task_telegram(
    db: Session, tasks: list[models.ProvisioningTask]
) -> dict[int, str | None]:
    """Resolve owning telegram_id for each task in one batched pass.

    Walks device and subscription targets up to their user. Done as
    two bulk SELECTs (one per target type) rather than a per-row
    lookup, so the Tasks admin page stays cheap even with 200 rows.
    Node tasks return None (no owner concept).
    """
    device_ids = [t.target_id for t in tasks if t.target_type == "device"]
    sub_ids = [t.target_id for t in tasks if t.target_type == "subscription"]

    device_owner: dict[int, str | None] = {}
    if device_ids:
        rows = (
            db.query(models.Device.id, models.User.telegram_id)
            .join(models.Subscription, models.Device.subscription_id == models.Subscription.id)
            .join(models.User, models.Subscription.user_id == models.User.id)
            .filter(models.Device.id.in_(device_ids))
            .all()
        )
        device_owner = {row[0]: row[1] for row in rows}

    sub_owner: dict[int, str | None] = {}
    if sub_ids:
        rows = (
            db.query(models.Subscription.id, models.User.telegram_id)
            .join(models.User, models.Subscription.user_id == models.User.id)
            .filter(models.Subscription.id.in_(sub_ids))
            .all()
        )
        sub_owner = {row[0]: row[1] for row in rows}

    out: dict[int, str | None] = {}
    for t in tasks:
        if t.target_type == "device":
            out[t.id] = device_owner.get(t.target_id)
        elif t.target_type == "subscription":
            out[t.id] = sub_owner.get(t.target_id)
        else:
            out[t.id] = None
    return out


@router.get("/provisioning/tasks", response_model=list[schemas.ProvisioningTaskOut])
def list_tasks(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    target_type: str | None = None,
    telegram_id: str | None = Query(
        default=None,
        description="Filter tasks to ones owned by this telegram_id (via device→sub→user chain)",
    ),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    query = db.query(models.ProvisioningTask)
    if status_filter:
        try:
            query = query.filter(
                models.ProvisioningTask.status == models.ProvisioningTaskStatus(status_filter)
            )
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid status") from exc
    if target_type:
        query = query.filter(models.ProvisioningTask.target_type == target_type)

    if telegram_id:
        # Narrow by telegram_id: collect the device/subscription ids
        # owned by that user, then constrain the task query to those.
        user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
        if user is None:
            return []
        sub_ids = [
            s.id for s in db.query(models.Subscription.id)
            .filter(models.Subscription.user_id == user.id).all()
        ]
        dev_ids: list[int] = []
        if sub_ids:
            dev_ids = [
                d.id for d in db.query(models.Device.id)
                .filter(models.Device.subscription_id.in_(sub_ids)).all()
            ]
        conds = []
        if sub_ids:
            conds.append(
                and_(
                    models.ProvisioningTask.target_type == "subscription",
                    models.ProvisioningTask.target_id.in_(sub_ids),
                )
            )
        if dev_ids:
            conds.append(
                and_(
                    models.ProvisioningTask.target_type == "device",
                    models.ProvisioningTask.target_id.in_(dev_ids),
                )
            )
        if not conds:
            return []
        query = query.filter(or_(*conds))

    tasks = (
        query.order_by(models.ProvisioningTask.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    tg_map = _enrich_task_telegram(db, tasks)
    out: list[schemas.ProvisioningTaskOut] = []
    for t in tasks:
        dto = schemas.ProvisioningTaskOut.from_orm(t)
        dto.telegram_id = tg_map.get(t.id)
        out.append(dto)
    return out


@router.delete("/provisioning/tasks/{task_id}", status_code=204)
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Delete a single ProvisioningTask row.

    Refuses ``running`` tasks — those are in-flight and the orchestrator
    still holds a reference to them. Terminal states (success, failed,
    pending) are free to remove; the history lives in AuditLog anyway.
    """
    task = db.get(models.ProvisioningTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status == models.ProvisioningTaskStatus.running:
        raise HTTPException(
            status_code=409,
            detail="Task is running; wait for it to finish before deleting",
        )
    db.delete(task)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "task_deleted", "provisioning_task", task_id, actor_type=actor_type)
    return None


@router.get("/provisioning/tasks/{task_id}", response_model=schemas.ProvisioningTaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(models.ProvisioningTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return schemas.ProvisioningTaskOut.from_orm(task)


@router.post(
    "/provisioning/tasks/{task_id}/execute",
    response_model=schemas.ProvisioningTaskOut,
)
def execute_task(
    task_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    task = db.get(models.ProvisioningTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    orchestrator = ProvisioningOrchestrator(db)
    node = db.get(models.VPNNode, task.target_id) if task.target_type == "node" else None
    orchestrator.run_task_async(task, node=node)
    db.refresh(task)
    return schemas.ProvisioningTaskOut.from_orm(task)


@router.post(
    "/provisioning/tasks/{task_id}/rerun",
    response_model=schemas.ProvisioningTaskOut,
)
def rerun_task(
    task_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    task = db.get(models.ProvisioningTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    # Allow rerun on success too: "re-run bootstrap" is a legitimate
    # way to ship updated ansible roles to an already-live node
    # without a round trip through node delete + spawn. Refuse only
    # the still-in-flight states so we don't stomp on a running job.
    if task.status in {
        models.ProvisioningTaskStatus.running,
    }:
        raise HTTPException(
            status_code=409,
            detail="Task is currently running; wait for it to finish",
        )
    orchestrator = ProvisioningOrchestrator(db)
    orchestrator.reset_failed_task(task)
    orchestrator.run_task_async(task, node=None)
    db.refresh(task)
    return schemas.ProvisioningTaskOut.from_orm(task)


@router.post("/provisioning/tasks/batch")
def batch_tasks(
    body: dict,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Batch action on multiple provisioning tasks.

    Body: ``{ "ids": [1,2,3], "action": "delete" | "rerun" }``
    """
    ids = body.get("ids", [])
    action = body.get("action", "")
    if not ids or action not in ("delete", "rerun"):
        raise HTTPException(
            status_code=400,
            detail="ids (list) and action (delete|rerun) required",
        )

    actor, actor_type = _resolve_admin_actor(admin_actor)
    results: dict[str, list[int]] = {"ok": [], "skipped": [], "not_found": []}
    orchestrator = ProvisioningOrchestrator(db)

    for tid in ids:
        task = db.get(models.ProvisioningTask, tid)
        if not task:
            results["not_found"].append(tid)
            continue

        if action == "delete":
            if task.status == models.ProvisioningTaskStatus.running:
                results["skipped"].append(tid)
                continue
            db.delete(task)
            results["ok"].append(tid)

        elif action == "rerun":
            if task.status == models.ProvisioningTaskStatus.running:
                results["skipped"].append(tid)
                continue
            orchestrator.reset_failed_task(task)
            orchestrator.run_task_async(task, node=None)
            results["ok"].append(tid)

    db.commit()
    _audit(
        db, actor, f"tasks_batch_{action}", "provisioning_task", None,
        actor_type=actor_type,
        metadata={"ids": ids, "results": results},
    )
    return results


@router.get("/provisioning/queue-status")
def queue_status(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Snapshot of the RQ backend + the 8 self-rescheduling tick jobs.

    Admins use this to spot when zombie jobs accumulate — pre-F.1 a few
    worker restarts left 50+ ``scheduled`` entries; a healthy queue
    shows one per tick_id. ``stuck_tasks`` counts DB rows in ``running``
    that RQ can no longer see, which is the other half of the "did a
    worker die mid-job?" diagnosis.
    """
    from ..queue import QUEUE_NAME, TICK_IDS, get_queue

    snapshot: dict[str, Any] = {
        "redis_available": False,
        "queue_name": QUEUE_NAME,
        "queued": 0,
        "started": 0,
        "scheduled": 0,
        "deferred": 0,
        "failed": 0,
        "finished": 0,
        "workers": [],
        "ticks": [],
        "stuck_tasks": 0,
    }

    queue = get_queue()
    if queue is None:
        return snapshot
    snapshot["redis_available"] = True

    try:
        from rq import Worker
        from rq.job import Job
        from rq.exceptions import NoSuchJobError
        from rq.registry import (
            DeferredJobRegistry,
            FailedJobRegistry,
            FinishedJobRegistry,
            ScheduledJobRegistry,
            StartedJobRegistry,
        )

        started = StartedJobRegistry(queue=queue)
        try:
            started.cleanup()
        except Exception:  # noqa: BLE001
            logger.debug("StartedJobRegistry.cleanup() failed", exc_info=True)

        snapshot["queued"] = queue.count
        snapshot["started"] = started.count
        snapshot["scheduled"] = ScheduledJobRegistry(queue=queue).count
        snapshot["deferred"] = DeferredJobRegistry(queue=queue).count
        snapshot["failed"] = FailedJobRegistry(queue=queue).count
        snapshot["finished"] = FinishedJobRegistry(queue=queue).count

        snapshot["workers"] = [
            {
                "name": w.name,
                "state": w.get_state(),
                "current_job_id": w.get_current_job_id(),
            }
            for w in Worker.all(queue=queue)
        ]

        tick_rows: list[dict[str, Any]] = []
        for func_name, tick_id in TICK_IDS.items():
            row: dict[str, Any] = {
                "tick_id": tick_id,
                "func": func_name,
                "status": None,
                "enqueued_at": None,
            }
            try:
                job = Job.fetch(tick_id, connection=queue.connection)
                row["status"] = job.get_status(refresh=True)
                if job.enqueued_at is not None:
                    row["enqueued_at"] = job.enqueued_at.isoformat()
            except NoSuchJobError:
                row["status"] = "missing"
            except Exception:  # noqa: BLE001
                logger.debug("Job.fetch(%s) failed", tick_id, exc_info=True)
                row["status"] = "error"
            tick_rows.append(row)
        snapshot["ticks"] = tick_rows
    except Exception:  # noqa: BLE001
        logger.exception("queue_status introspection failed")

    stuck = (
        db.query(models.ProvisioningTask)
        .filter(models.ProvisioningTask.status == models.ProvisioningTaskStatus.running)
        .count()
    )
    snapshot["stuck_tasks"] = stuck
    return snapshot


@router.post("/provisioning/queue/reset-stuck")
def queue_reset_stuck(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Rerun the startup "reset stuck" sweep on demand.

    Same code path as ``app.main.reset_stuck_tasks`` — requeues tasks
    left in ``running`` by a crashed worker and re-enqueues stray
    ``pending`` rows. Exposed so ops can unblock the queue without
    restarting the API container. Audited because batch re-enqueue
    is a load spike on ansible.
    """
    from ..main import reset_stuck_tasks

    summary = reset_stuck_tasks()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "queue_reset_stuck", "provisioning_task", None,
        actor_type=actor_type,
        metadata=dict(summary),
    )
    return summary
