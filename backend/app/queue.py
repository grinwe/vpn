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

        job_id = f"provision-{task_id}"
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
            failure_ttl=FAILED_TTL,
            result_ttl=RESULT_TTL,
            job_id=job_id,
        )
        return job.id
    except Exception:  # noqa: BLE001
        logger.exception("Failed to enqueue task %s; falling back to inline", task_id)
        return None
