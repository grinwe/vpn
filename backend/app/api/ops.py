"""Admin ops/diagnostics endpoints: ``/api/ops/*``.

Что тут живёт — runtime-наблюдение за самим воркером. Проблема: если
RQScheduler-форк умер (между деплоями, из-за ООМ-а, утекшего Redis-lock'а)
— воркер-процесс всё ещё жив, healthcheck у него проходит, но
самоперепланирующиеся tick-ы ``queue.schedule_tick`` останавливаются
молча. Снаружи это выглядит как "почему observed_at старый" — а корневая
причина не в tick-коде, а в том что scheduler не кикает scheduled-job'ы.

``GET /ops/ticks/status`` отдаёт админу живой снимок: для каждого
зарегистрированного ``TICK_IDS`` — состояние Job в Redis (queued /
scheduled / started / finished / failed / missing), next-run timestamp
и overdue-лаг, плюс список workers с их last_heartbeat. UI показывает
светофор "worker alive / stalled / down" без заглядывания в docker logs.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from .. import schemas
from ..auth import require_admin
from ..queue import RESULT_TTL, TICK_IDS, get_queue

router = APIRouter()


_TICK_INTERVAL_ENV = {
    "tick-pending-rescue": ("PENDING_RESCUE_INTERVAL", "60"),
    "tick-autoscale": ("AUTOSCALE_INTERVAL", "0"),
    "tick-renewal": ("RENEWAL_CHECK_INTERVAL", "3600"),
    "tick-warm-pool": ("WARM_POOL_CHECK_INTERVAL", "120"),
    "tick-balance-charge": ("BALANCE_CHARGE_INTERVAL", "3600"),
    "tick-traffic-stats": ("TRAFFIC_STATS_INTERVAL", "300"),
    "tick-relay-link-health": ("RELAY_LINK_HEALTH_INTERVAL", "300"),
    "tick-health-ping": ("USER_HEALTH_PING_INTERVAL", "1800"),
}


def _tick_interval(tick_id: str) -> int:
    import os

    env_key, default = _TICK_INTERVAL_ENV.get(tick_id, (None, "0"))
    if env_key is None:
        return 0
    try:
        return int(os.getenv(env_key, default))
    except ValueError:
        return 0


def _aware(dt: datetime | None) -> datetime | None:
    """RQ отдаёт наивные UTC datetime'ы; навешиваем tz для UTCDateTime сериализации."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _describe_job(tick_id: str, queue) -> schemas.TickStatusItem:
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    func_name = next(
        (k for k, v in TICK_IDS.items() if v == tick_id),
        tick_id,
    )
    interval = _tick_interval(tick_id)

    if queue is None:
        return schemas.TickStatusItem(
            tick_id=tick_id,
            func_name=func_name,
            interval_seconds=interval,
            job_status="unknown",
        )

    try:
        job = Job.fetch(tick_id, connection=queue.connection)
    except NoSuchJobError:
        return schemas.TickStatusItem(
            tick_id=tick_id,
            func_name=func_name,
            interval_seconds=interval,
            job_status="missing",
        )
    except Exception:  # noqa: BLE001
        return schemas.TickStatusItem(
            tick_id=tick_id,
            func_name=func_name,
            interval_seconds=interval,
            job_status="unknown",
        )

    status = job.get_status(refresh=True)
    enqueued_at = _aware(job.enqueued_at)
    started_at = _aware(job.started_at)
    ended_at = _aware(job.ended_at)

    # RQ хранит scheduled-ts в ZSET rq:scheduler-ranking и не
    # прокидывает в Job напрямую. Достаём из ScheduledJobRegistry
    # когда статус == scheduled.
    scheduled_for: datetime | None = None
    overdue: int | None = None
    if status == "scheduled":
        try:
            from rq.registry import ScheduledJobRegistry

            reg = ScheduledJobRegistry(queue=queue)
            score = queue.connection.zscore(reg.key, tick_id)
            if score is not None:
                scheduled_for = datetime.fromtimestamp(
                    float(score), tz=timezone.utc
                )
                now = datetime.now(tz=timezone.utc)
                if scheduled_for < now:
                    overdue = int((now - scheduled_for).total_seconds())
                else:
                    overdue = 0
        except Exception:  # noqa: BLE001
            pass

    exc_type: str | None = None
    if status == "failed" and job.exc_info:
        first = job.exc_info.splitlines()[0] if job.exc_info else ""
        exc_type = first.strip()[:200] or None

    return schemas.TickStatusItem(
        tick_id=tick_id,
        func_name=func_name,
        interval_seconds=interval,
        job_status=status or "missing",
        enqueued_at=enqueued_at,
        started_at=started_at,
        ended_at=ended_at,
        scheduled_for=scheduled_for,
        overdue_by_seconds=overdue,
        last_exc_type=exc_type,
    )


def _describe_workers(queue) -> list[schemas.WorkerInfo]:
    if queue is None:
        return []
    try:
        from rq import Worker

        workers = Worker.all(queue=queue)
    except Exception:  # noqa: BLE001
        return []

    out: list[schemas.WorkerInfo] = []
    for w in workers:
        try:
            last_hb = _aware(w.last_heartbeat)
            state = w.get_state()
            current = w.get_current_job_id()
        except Exception:  # noqa: BLE001
            last_hb = None
            state = "unknown"
            current = None
        out.append(
            schemas.WorkerInfo(
                name=w.name,
                state=state or "unknown",
                last_heartbeat=last_hb,
                current_job_id=current,
            )
        )
    return out


@router.get("/ops/ticks/status", response_model=schemas.TicksStatusOut)
def ticks_status(admin_token: str = Depends(require_admin)):
    """Админский снимок состояния всех периодических тиков.

    Отдаёт: живость Redis-очереди, список workers (с last_heartbeat),
    и по каждому tick'у — RQ Job status + scheduled_for + overdue-лаг.

    UI-виджет красит "worker alive" если хотя бы один worker'овский
    ``last_heartbeat`` моложе 90 сек, и "tick healthy" если job-status
    in {started, scheduled, queued} И overdue_by_seconds < interval*2.
    Если красный (``started`` без прогресса > interval*3 или
    ``scheduled`` с overdue > interval*2) — scheduler-форк скорее всего
    умер, рестарт воркера освежит всё благодаря ``replace=True`` в
    bootstrap'ах.
    """
    queue = get_queue()
    queue_available = queue is not None

    workers = _describe_workers(queue)
    ticks = [_describe_job(tid, queue) for tid in TICK_IDS.values()]

    _ = RESULT_TTL  # keep import eager for traceability

    return schemas.TicksStatusOut(
        queue_available=queue_available,
        workers=workers,
        ticks=ticks,
    )
