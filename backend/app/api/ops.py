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

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import schemas
from ..auth import require_admin
from ..queue import RESULT_TTL, TICK_IDS, get_queue, get_ticks_queue
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


@router.get("/version")
def get_version() -> dict[str, str | None]:
    """Версия выкаченного кода. Без авторизации — секрета тут нет, а фронтам
    (и человеку через curl) она нужна, чтобы понять, какой билд отвечает."""
    from ..version import version_info

    return version_info()


_TICK_INTERVAL_ENV = {
    "tick-pending-rescue": ("PENDING_RESCUE_INTERVAL", "60"),
    "tick-autoscale": ("AUTOSCALE_INTERVAL", "0"),
    "tick-renewal": ("RENEWAL_CHECK_INTERVAL", "3600"),
    "tick-warm-pool": ("WARM_POOL_CHECK_INTERVAL", "120"),
    "tick-balance-charge": ("BALANCE_CHARGE_INTERVAL", "3600"),
    "tick-traffic-stats": ("TRAFFIC_STATS_INTERVAL", "300"),
    "tick-relay-link-health": ("RELAY_LINK_HEALTH_INTERVAL", "300"),
    "tick-health-ping": ("USER_HEALTH_PING_INTERVAL", "1800"),
    "tick-broadcast-dispatch": ("BROADCAST_DISPATCH_INTERVAL", "10"),
    "tick-node-versions": ("NODE_VERSIONS_INTERVAL", "3600"),
    "tick-xray-upstream": ("XRAY_UPSTREAM_INTERVAL", "21600"),
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

    # RQ хранит scheduled-ts в ZSET rq:scheduled:<queue> и не
    # прокидывает в Job напрямую. ВСЕГДА смотрим в ZSET, не только
    # когда hash.status==scheduled: после переноса self-reschedule в
    # начало тика, между окончанием run'а и scheduler-fire hash имеет
    # status=finished (worker перезаписал его при handle_job_success),
    # но tick_id уже лежит в ScheduledJobRegistry с future-score —
    # ZSET здесь source of truth для "когда следующий запуск".
    scheduled_for: datetime | None = None
    overdue: int | None = None
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
            # Hash может показывать finished/failed от предыдущего
            # запуска — для UI показываем "scheduled", потому что
            # следующий запуск реально запланирован.
            if status in ("finished", "failed"):
                status = "scheduled"
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
    """Энумерация воркеров по connection, а не по queue.

    Нам нужно показать и worker-ticks и worker-provisioning в одном списке,
    даже если они слушают разные очереди. Worker.all(connection=...) идёт
    через Redis SADD rq:workers — общий для всех ролей.
    """
    if queue is None:
        return []
    try:
        from rq import Worker

        workers = Worker.all(connection=queue.connection)
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
    # Ticks живут в отдельной очереди vpn-ticks (stage 3 split), но
    # "queue_available" мы всё ещё считаем по provisioning-очереди —
    # если Redis живой, она тоже есть. Tick lookup же должен идти в
    # ticks_queue, иначе ScheduledJobRegistry смотрит не туда и все
    # тики видны как "missing".
    prov_queue = get_queue()
    ticks_queue = get_ticks_queue()
    queue_available = prov_queue is not None or ticks_queue is not None

    # Воркеров enum'им по connection — любой из двух queue.connection
    # даёт доступ ко всем workers в Redis.
    enum_queue = ticks_queue or prov_queue
    workers = _describe_workers(enum_queue)
    ticks = [_describe_job(tid, ticks_queue) for tid in TICK_IDS.values()]

    _ = RESULT_TTL  # keep import eager for traceability

    return schemas.TicksStatusOut(
        queue_available=queue_available,
        workers=workers,
        ticks=ticks,
    )


@router.post("/ops/worker/restart", response_model=schemas.WorkerRestartOut)
def restart_workers(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Graceful shutdown всех RQ-воркеров через Redis pub/sub.

    RQ слушает команду в своём pubsub-треде и завершается после текущего
    job'а (warm shutdown — in-flight provisioning не оборвётся). Docker
    compose с ``restart: unless-stopped`` поднимет контейнер заново,
    bootstrap в ``main()`` переставит все tick'и с ``replace=True``.

    Удобно когда scheduler-форк молча умер и светофор красный — одним
    кликом из UI перезапускаем вместо ``docker compose restart worker``
    на хосте.
    """
    queue = get_queue() or get_ticks_queue()
    if queue is None:
        raise HTTPException(status_code=503, detail="Redis queue unavailable")

    from rq import Worker
    from rq.command import send_shutdown_command

    try:
        # По connection — ловим и worker-ticks и worker-provisioning
        # одним запросом.
        workers = Worker.all(connection=queue.connection)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail=f"Failed to enumerate workers: {exc}"
        ) from exc

    if not workers:
        raise HTTPException(status_code=404, detail="No registered workers")

    signalled: list[str] = []
    failed: list[str] = []
    for w in workers:
        try:
            send_shutdown_command(queue.connection, w.name)
            signalled.append(w.name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{w.name}: {exc}")

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "worker_restart",
        "worker",
        None,
        actor_type=actor_type,
        metadata={"signalled": signalled, "failed": failed},
    )

    return schemas.WorkerRestartOut(
        signalled=signalled,
        failed=failed,
    )


@router.post("/ops/worker/scale", response_model=schemas.WorkerScaleOut)
def scale_workers(
    body: schemas.WorkerScaleRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),  # noqa: ARG001
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Отскейлить число worker-контейнеров (1..20).

    API-образ без ssh/ключа, поэтому реальный ``docker compose --scale``
    делает worker по SSH на mgmt-хост (см. ``app.worker.run_scale_workers``
    + ``scripts/workers.sh``). Энкьюим job в provisioning-очередь и коротко
    ждём результат для синхронного фидбэка. Требует ≥1 живого воркера,
    который подхватит job (для бампа вверх это всегда так).
    """
    import os
    import time

    queue = get_queue()
    if queue is None:
        raise HTTPException(status_code=503, detail="Redis queue unavailable")

    n = body.replicas
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "worker_scale",
        "worker",
        None,
        actor_type=actor_type,
        metadata={"replicas": n},
    )

    try:
        job = queue.enqueue("app.worker.run_scale_workers", n, job_timeout=180)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail=f"Failed to enqueue scale job: {exc}"
        ) from exc

    # Коротко ждём результат (скейл — секунды). Не дождались → enqueued,
    # счётчик воркеров в виджете подтянется поллингом. Держим окно узким,
    # чтобы не занимать надолго поток threadpool'а и DB-сессию запроса
    # (env-кноб OPS_SCALE_WAIT_SECONDS, по умолчанию 5с).
    try:
        wait_seconds = float(os.getenv("OPS_SCALE_WAIT_SECONDS", "5"))
    except ValueError:
        wait_seconds = 5.0
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        status = job.get_status(refresh=True)
        if status == "finished":
            res = job.result or {}
            if res.get("ok"):
                return schemas.WorkerScaleOut(
                    replicas=n,
                    status="applied",
                    detail=((res.get("stdout") or "")[-400:]) or None,
                )
            return schemas.WorkerScaleOut(
                replicas=n,
                status="failed",
                detail=(res.get("stderr") or "scale failed")[-400:],
            )
        if status == "failed":
            return schemas.WorkerScaleOut(
                replicas=n,
                status="failed",
                detail=(job.exc_info or "job failed")[-400:],
            )
        time.sleep(1)

    return schemas.WorkerScaleOut(
        replicas=n,
        status="enqueued",
        detail="job запущен, счётчик обновится поллингом виджета",
    )
