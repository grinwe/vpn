"""Health and stats endpoints: ``/api/healthz`` and ``/api/stats``."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func, text
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ._common import get_db

router = APIRouter()


@router.get("/healthz")
def healthcheck(db: Session = Depends(get_db), deep: bool = Query(default=False)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="database not reachable")
    if deep:
        # The API image intentionally ships without ansible (the provisioning
        # worker image carries it). Deep health-check therefore probes the
        # task queue instead: if we cannot enqueue, new subscriptions will
        # silently fall back to the legacy in-process thread runner.
        from ..queue import get_queue

        queue = get_queue()
        if queue is None:
            raise HTTPException(status_code=500, detail="queue backend unreachable")
    return {"status": "ok"}


@router.get("/stats", response_model=schemas.StatsOut)
def get_stats(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """High-level counters for the admin dashboard.

    Everything is a cheap ``COUNT(*)`` on indexed columns — safe to hit
    on every dashboard render. For anything heavier (time series, per-
    node traffic, probe histograms) point people at Grafana instead of
    growing this endpoint.
    """
    users_total = db.query(sa_func.count(models.User.id)).scalar() or 0
    subs_active = (
        db.query(sa_func.count(models.Subscription.id))
        .filter(models.Subscription.status == models.SubscriptionStatus.active)
        .scalar()
        or 0
    )
    subs_total = db.query(sa_func.count(models.Subscription.id)).scalar() or 0
    invoices_pending = (
        db.query(sa_func.count(models.Invoice.id))
        .filter(models.Invoice.status == models.InvoiceStatus.pending)
        .scalar()
        or 0
    )
    nodes_total = db.query(sa_func.count(models.VPNNode.id)).scalar() or 0
    nodes_active = (
        db.query(sa_func.count(models.VPNNode.id))
        .filter(models.VPNNode.status == models.VPNNodeStatus.active)
        .scalar()
        or 0
    )
    devices_active = (
        db.query(sa_func.count(models.Device.id))
        .filter(models.Device.status == models.DeviceStatus.active)
        .scalar()
        or 0
    )
    tasks_pending = (
        db.query(sa_func.count(models.ProvisioningTask.id))
        .filter(
            models.ProvisioningTask.status.in_(
                [
                    models.ProvisioningTaskStatus.pending,
                    models.ProvisioningTaskStatus.running,
                ]
            )
        )
        .scalar()
        or 0
    )
    tasks_failed = (
        db.query(sa_func.count(models.ProvisioningTask.id))
        .filter(models.ProvisioningTask.status == models.ProvisioningTaskStatus.failed)
        .scalar()
        or 0
    )

    # «Активны за 24ч» по РЕАЛЬНОМУ трафику: distinct access_username из
    # NodeTrafficSample.details[*]["users"] за окно → резолв в Device→User
    # (тем же путём, что /nodes/{id}/users — xray видит имя КРЕДА, а не Device).
    # Считаем юзеров (user_id != сирота), устройства (юзер с N девайсами ≠ 1) и
    # активных сирот отдельно (recovery-плейсхолдер user_id=999999, инцидент
    # 2026-05). Дороже COUNT(*): скан окна сэмплов (~сотни строк/сутки) + один
    # IN-join по индексированному access_username — для дашборда (малый флот,
    # поллинг 15с) приемлемо.
    from datetime import timedelta

    from ..time_utils import utcnow

    orphan_owner_id = 999999  # см. api/admin_claim.py ORPHAN_OWNER_ID
    lookback = utcnow() - timedelta(hours=24)
    active_usernames: set[str] = set()
    for (details,) in (
        db.query(models.NodeTrafficSample.details)
        .filter(models.NodeTrafficSample.observed_at >= lookback)
        .all()
    ):
        if not isinstance(details, dict):
            continue
        for proto_data in details.values():
            if not isinstance(proto_data, dict):
                continue
            users = proto_data.get("users")
            if isinstance(users, list):  # legacy rows had bare int — пропускаем
                for uname in users:
                    if isinstance(uname, str) and uname:
                        active_usernames.add(uname)

    users_active_24h = devices_active_24h = orphans_active_24h = 0
    if active_usernames:
        real_users: set[int] = set()
        all_devices: set[int] = set()
        orphan_devices: set[int] = set()
        for dev_id, uid in (
            db.query(models.Device.id, models.Device.user_id)
            .join(models.Credential, models.Credential.device_id == models.Device.id)
            .filter(models.Credential.access_username.in_(list(active_usernames)))
            .distinct()
            .all()
        ):
            all_devices.add(dev_id)
            if uid == orphan_owner_id:
                orphan_devices.add(dev_id)
            elif uid is not None:
                real_users.add(uid)
        users_active_24h = len(real_users)
        devices_active_24h = len(all_devices)
        orphans_active_24h = len(orphan_devices)

    return schemas.StatsOut(
        users_total=users_total,
        subscriptions_active=subs_active,
        subscriptions_total=subs_total,
        invoices_pending=invoices_pending,
        nodes_total=nodes_total,
        nodes_active=nodes_active,
        devices_active=devices_active,
        users_active_24h=users_active_24h,
        devices_active_24h=devices_active_24h,
        orphans_active_24h=orphans_active_24h,
        provisioning_tasks_pending=tasks_pending,
        provisioning_tasks_failed=tasks_failed,
    )
