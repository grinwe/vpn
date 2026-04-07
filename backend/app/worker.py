"""RQ worker entrypoint.

Run with ``python -m app.worker`` inside the backend image. The worker
imports the same SQLAlchemy / provisioning code as the API and executes the
Ansible subprocess itself, so the API container no longer needs ansible
installed or SSH keys mounted — a meaningful blast-radius reduction.

The job function :func:`run_provisioning_task` is intentionally a thin
wrapper around :meth:`ProvisioningOrchestrator._run_task_in_new_session`,
which already opens its own DB session. That lets us share one code path
between the legacy inline thread runner and the new queue worker.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


def run_autoscale_tick() -> list[dict]:
    """Periodic job — walk pools and scale up where needed.

    Returns a list of :class:`PoolDecision` dicts so that the RQ result
    backend captures a useful audit trail for each tick. The job re-enqueues
    itself at the end so that one-shot ``enqueue_in`` on worker startup
    produces a recurring schedule without a full-fat rq-scheduler dependency.
    """
    from dataclasses import asdict
    from datetime import timedelta

    from .db import SessionLocal
    from .queue import get_queue
    from .services.autoscale import evaluate_all_pools

    session = SessionLocal()
    try:
        decisions = evaluate_all_pools(session)
        result = [asdict(d) for d in decisions]
    finally:
        session.close()

    interval = int(os.getenv("AUTOSCALE_INTERVAL", "0"))
    if interval > 0:
        queue = get_queue()
        if queue is not None:
            try:
                queue.enqueue_in(
                    timedelta(seconds=interval),
                    "app.worker.run_autoscale_tick",
                    result_ttl=3600,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to re-enqueue autoscale tick")
    return result


def run_provisioning_task(task_id: int, node_id: int | None = None) -> dict:
    """RQ job — executed by the worker process."""
    # Imported lazily so that the HTTP image does not pull provisioning code
    # into memory just to enqueue jobs.
    from .db import SessionLocal
    from . import models
    from .services.provisioning import ProvisioningOrchestrator

    session = SessionLocal()
    try:
        task = session.get(models.ProvisioningTask, task_id)
        if not task:
            logger.error("Task %s disappeared before worker picked it up", task_id)
            return {"status": "missing", "task_id": task_id}

        # Reset if a previous run left it running (e.g. worker crashed mid-job).
        if task.status == models.ProvisioningTaskStatus.running:
            task.status = models.ProvisioningTaskStatus.pending
            task.error_message = None
            session.add(task)
            session.commit()

        orchestrator = ProvisioningOrchestrator(session)
        node = None
        if node_id:
            node = session.get(models.VPNNode, node_id)
        orchestrator.run_task(task, node=node)
        return {
            "status": task.status.value,
            "task_id": task.id,
            "error": task.error_message,
        }
    finally:
        session.close()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        from redis import Redis
        from rq import Queue, Worker
    except ImportError:
        logger.error("redis/rq are not installed; worker cannot start")
        sys.exit(1)

    url = os.getenv("REDIS_URL")
    if not url:
        logger.error("REDIS_URL is required for the worker")
        sys.exit(1)

    queue_name = os.getenv("RQ_QUEUE", "vpn-provisioning")
    connection = Redis.from_url(url)
    connection.ping()

    # Run migrations on worker startup too — the worker is a valid place to
    # apply schema upgrades in deployments where the API is not the first
    # process to come up.
    try:
        from .migrations import run_migrations

        run_migrations()
    except Exception:  # noqa: BLE001
        logger.exception("Worker failed to run migrations at startup")

    queue = Queue(queue_name, connection=connection)

    # Schedule the autoscale tick using rq.Queue's built-in scheduler. Using
    # a deterministic job_id ("autoscale-tick") means repeated worker
    # restarts will not create duplicate schedules — enqueue_at will refuse
    # an id that already exists in the scheduler.
    autoscale_interval = int(os.getenv("AUTOSCALE_INTERVAL", "0"))
    if autoscale_interval > 0:
        try:
            from datetime import timedelta

            queue.enqueue_in(
                timedelta(seconds=autoscale_interval),
                "app.worker.run_autoscale_tick",
                result_ttl=3600,
            )
            logger.info(
                "Autoscale bootstrapped: first tick in %ss (interval=%ss)",
                autoscale_interval,
                autoscale_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule autoscale tick")

    worker = Worker([queue], connection=connection)
    logger.info("Starting RQ worker on queue %s", queue_name)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
