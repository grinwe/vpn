"""Task queue integration.

We use Redis + RQ to run provisioning/ansible work outside the HTTP request
lifecycle. This keeps the API stateless (so it can scale horizontally) and
gives us retries, backoff and a durable job log — things the previous
``threading.Thread`` approach could not offer.

The module is designed to be import-safe even when Redis or RQ are not
installed: if :func:`get_queue` fails for any reason, :func:`enqueue_task`
transparently falls back to the in-process thread runner from
``services.provisioning``. That lets local development (``docker-compose up``
without a Redis service) keep working.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from rq import Queue
    from redis import Redis


QUEUE_NAME = os.getenv("RQ_QUEUE", "vpn-provisioning")
DEFAULT_JOB_TIMEOUT = int(os.getenv("RQ_JOB_TIMEOUT", "900"))  # seconds
# RQ keeps failed jobs in a dead-letter-ish "failed" registry; we keep them
# around for a week so ops can inspect them.
FAILED_TTL = int(os.getenv("RQ_FAILED_TTL", "604800"))
RESULT_TTL = int(os.getenv("RQ_RESULT_TTL", "86400"))


def _backend_enabled() -> bool:
    return os.getenv("QUEUE_BACKEND", "").lower() == "rq" and bool(os.getenv("REDIS_URL"))


@lru_cache(maxsize=1)
def get_redis() -> "Redis | None":
    if not _backend_enabled():
        return None
    try:
        from redis import Redis

        url = os.environ["REDIS_URL"]
        client = Redis.from_url(url)
        client.ping()
        return client
    except Exception:  # noqa: BLE001
        logger.exception("Failed to connect to Redis; falling back to inline execution")
        return None


@lru_cache(maxsize=1)
def get_queue() -> "Queue | None":
    redis = get_redis()
    if redis is None:
        return None
    try:
        from rq import Queue

        return Queue(QUEUE_NAME, connection=redis, default_timeout=DEFAULT_JOB_TIMEOUT)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to construct RQ Queue; falling back to inline execution")
        return None


def enqueue_task(task_id: int, node_id: int | None) -> str | None:
    """Enqueue a provisioning task execution.

    Returns the RQ job id on success or ``None`` if the queue is unavailable
    (caller must then execute the task inline).
    """
    queue = get_queue()
    if queue is None:
        return None
    try:
        from rq import Retry
        from rq.job import Job
        from rq.exceptions import NoSuchJobError
        from rq.registry import StartedJobRegistry

        job_id = f"provision-{task_id}"
        # Reclaim zombie `started` jobs before the dedupe check below.
        # When a worker is SIGKILLed (OOM, container drop) mid-job, RQ
        # leaves the job in `started` state with the deterministic id
        # locked — `Job.fetch` then returns the zombie and the "skip if
        # already in-flight" branch returns its id without re-enqueueing,
        # so the matching ProvisioningTask sits in `pending` forever even
        # though the live worker is idle. StartedJobRegistry.cleanup()
        # inspects worker heartbeats and moves orphaned jobs into the
        # failed registry, after which the existing "delete failed →
        # fresh enqueue under the same id" path takes over uniformly.
        try:
            StartedJobRegistry(queue=queue).cleanup()
        except Exception:  # noqa: BLE001
            logger.debug("StartedJobRegistry.cleanup() failed (non-fatal)", exc_info=True)

        # Dedupe: if this task is already queued or in-flight, don't push
        # a second copy. We keep the deterministic job_id so restarts are
        # idempotent — re-enqueueing the same task after a crash is a
        # no-op, and crashed jobs get requeued by the worker's recovery
        # path (reset_stuck_tasks).
        try:
            existing = Job.fetch(job_id, connection=queue.connection)
            if existing.get_status(refresh=True) in {"queued", "started", "deferred", "scheduled"}:
                return existing.id
            # Finished/failed — delete the record so we can re-enqueue a
            # fresh run under the same id.
            existing.delete()
        except NoSuchJobError:
            pass

        job = queue.enqueue(
            "app.worker.run_provisioning_task",
            task_id,
            node_id,
            retry=Retry(max=3, interval=[10, 30, 120]),
            job_timeout=DEFAULT_JOB_TIMEOUT,
            failure_ttl=FAILED_TTL,
            result_ttl=RESULT_TTL,
            job_id=job_id,
        )
        return job.id
    except Exception:  # noqa: BLE001
        logger.exception("Failed to enqueue task %s; falling back to inline", task_id)
        return None


# Stable job_ids for the self-rescheduling worker ticks. Each tick's
# bootstrap call (in worker.main) and its self-reschedule call (at the
# end of the tick body) MUST pass the same tick_id — that's what makes
# repeated enqueues collapse into one scheduled job instead of spawning
# a parallel chain on every worker restart.
TICK_IDS = {
    "app.worker.run_pending_rescue_tick": "tick-pending-rescue",
    "app.worker.run_autoscale_tick": "tick-autoscale",
    "app.worker.run_renewal_check": "tick-renewal",
    "app.worker.run_warm_pool_check": "tick-warm-pool",
    "app.worker.run_balance_charge_tick": "tick-balance-charge",
    "app.worker.run_traffic_stats_tick": "tick-traffic-stats",
    "app.worker.run_user_health_ping_tick": "tick-health-ping",
    "app.worker.run_relay_link_health_tick": "tick-relay-link-health",
}

# Per-tick hard timeouts. Без них зависшая SSH (traffic-stats,
# relay-link-health ходят по всем активным нодам) держит воркер на
# `DEFAULT_JOB_TIMEOUT=900s` — следующие тики копят overdue, светофор
# жёлтый. Отдельный timeout запускает RQ kill horse быстро, self-reschedule
# внутри функции даже не успеет вызваться — но bootstrap на рестарте
# (и наш own reschedule из тика рядом) вернёт тик в строй.
TICK_TIMEOUTS = {
    "tick-traffic-stats": 120,
    "tick-relay-link-health": 120,
    "tick-pending-rescue": 60,
    "tick-warm-pool": 180,
    "tick-autoscale": 90,
    "tick-renewal": 300,
    "tick-balance-charge": 300,
    "tick-health-ping": 180,
}


def schedule_tick(
    func_name: str,
    interval_seconds: int,
    tick_id: str,
    *,
    replace: bool = False,
    job_timeout: int | None = None,
) -> str | None:
    """Enqueue a self-rescheduling tick with deterministic dedup.

    Mirrors ``enqueue_task`` but for periodic tick jobs: uses a stable
    ``tick_id`` as the RQ ``job_id`` so that the N+1-th call (bootstrap
    on worker restart while the previous scheduled job is still alive)
    collapses into a no-op instead of spawning a parallel chain.

    Pre-fix (random UUIDs from raw ``queue.enqueue_in``) a few worker
    restarts left 50+ duplicate ticks in ``scheduled``, each one debiting
    balance twice per cycle and multiplying SSH load from
    ``run_traffic_stats_tick``. See ``docs/components/worker.md``.

    ``replace=True`` forces a fresh enqueue even when the previous tick
    is still sitting in ``scheduled`` / ``queued`` / ``deferred`` /
    ``started``. This is the correct mode for **bootstrap at worker
    start-up**: if the RQScheduler fork died between deploys (or the
    worker crashed mid-tick and left its job in ``started`` with no
    owner), dedup-mode would see the stale entry and return early,
    leaving the tick frozen until someone clicks the UI refresh button.
    Self-reschedule calls at the end of a tick body should still use
    dedup (default) because there we *know* the queue is clean for the
    just-executed tick_id.

    Returns the RQ job id on success or ``None`` if the queue is
    unavailable.
    """
    queue = get_queue()
    if queue is None:
        return None
    try:
        from datetime import timedelta
        from rq.job import Job
        from rq.exceptions import NoSuchJobError
        from rq.registry import StartedJobRegistry

        try:
            StartedJobRegistry(queue=queue).cleanup()
        except Exception:  # noqa: BLE001
            logger.debug("StartedJobRegistry.cleanup() failed (non-fatal)", exc_info=True)

        try:
            existing = Job.fetch(tick_id, connection=queue.connection)
            if not replace and existing.get_status(refresh=True) in {
                "queued",
                "started",
                "deferred",
                "scheduled",
            }:
                return existing.id
            existing.delete()
        except NoSuchJobError:
            pass

        # job_timeout нужен чтобы зависшая SSH-сессия в traffic-stats /
        # relay-link-health не держала воркер вечно — RQ kill horse через
        # timeout, следующий тик поставится по self-reschedule (или
        # bootstrap'ом на рестарте). Без него SIGKILL воркера или
        # сетевой blip в SSH = scheduler не получает сигнал о завершении.
        enqueue_kwargs: dict = {
            "job_id": tick_id,
            "result_ttl": RESULT_TTL,
        }
        effective_timeout = (
            job_timeout if job_timeout is not None else TICK_TIMEOUTS.get(tick_id)
        )
        if effective_timeout is not None:
            enqueue_kwargs["job_timeout"] = effective_timeout
        job = queue.enqueue_in(
            timedelta(seconds=interval_seconds),
            func_name,
            **enqueue_kwargs,
        )
        return job.id
    except Exception:  # noqa: BLE001
        logger.exception("Failed to schedule tick %s (%s)", tick_id, func_name)
        return None
