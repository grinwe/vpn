"""RQ worker entrypoint.

Extends the original worker with:
  - Subscription renewal cron job (checks expires_at, creates renewal invoices)
  - Bot notification callback after successful provisioning
"""
from __future__ import annotations

import logging
import os
import sys

from prometheus_client import Counter, Gauge

logger = logging.getLogger(__name__)

# Heartbeat metrics — Grafana alerts on "no increment for >2× interval".
RENEWAL_RUNS = Counter(
    "vpn_renewal_check_runs_total", "Renewal cron tick count", ["outcome"]
)
RENEWAL_LAST_RUN = Gauge(
    "vpn_renewal_check_last_run_timestamp", "Unix ts of last successful renewal tick"
)
RENEWAL_REVOKED = Counter(
    "vpn_renewal_revoked_total", "Subscriptions auto-revoked after grace period"
)

# Grace window after expires_at before we actually rip the user off the node.
# Default: 24h. Set to 0 for instant revoke.
RENEWAL_GRACE_HOURS = int(os.getenv("RENEWAL_GRACE_HOURS", "24"))


def run_autoscale_tick() -> list[dict]:
    """Periodic job — walk pools and scale up where needed."""
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


def run_renewal_check() -> dict:
    """Periodic job — find subscriptions expiring within 3 days and create
    renewal invoices + notify users. Also expire overdue subscriptions.

    Returns summary stats for the RQ result backend.
    """
    from datetime import timedelta

    from .db import SessionLocal
    from .queue import get_queue
    from . import models
    from .services.provisioning import ProvisioningOrchestrator
    from .time_utils import utcnow

    session = SessionLocal()
    stats = {"reminded": 0, "expired": 0, "revoked": 0, "errors": 0}
    try:
        now = utcnow()
        remind_horizon = now + timedelta(days=3)
        revoke_cutoff = now - timedelta(hours=RENEWAL_GRACE_HOURS)

        # ── Mark overdue subscriptions as expired (status flip only). ──
        # The /sub/{token} endpoint already 403s on expired+blocked, so the
        # client stops getting fresh configs immediately. The hard revoke
        # below kicks in after the grace window.
        overdue = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.expires_at < now,
            )
            .all()
        )
        for sub in overdue:
            sub.status = models.SubscriptionStatus.expired
            session.add(sub)
            stats["expired"] += 1
        session.commit()

        # ── Hard revoke: drop the user from the node after grace window ──
        to_revoke = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.expired,
                models.Subscription.expires_at < revoke_cutoff,
            )
            .all()
        )
        if to_revoke:
            orch = ProvisioningOrchestrator(session)
            for sub in to_revoke:
                # Skip if all devices are already disabled — re-runs are
                # cheap but Ansible noise on a thousand-sub backlog isn't.
                live = [
                    d for d in sub.devices
                    if d.status not in (
                        models.DeviceStatus.revoked, models.DeviceStatus.disabled,
                    )
                ]
                if not live:
                    continue
                try:
                    orch.revoke_subscription_devices(
                        sub, reason=f"expired {sub.expires_at.isoformat()}"
                    )
                    stats["revoked"] += 1
                    RENEWAL_REVOKED.inc()
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to revoke expired sub %s", sub.id)
                    stats["errors"] += 1

        # ── Create renewal invoices for auto_renew subscriptions ──
        expiring_soon = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.auto_renew.is_(True),
                models.Subscription.expires_at <= remind_horizon,
                models.Subscription.expires_at > now,
            )
            .all()
        )
        for sub in expiring_soon:
            # Check if a pending renewal invoice already exists
            existing = (
                session.query(models.Invoice)
                .filter(
                    models.Invoice.subscription_id == sub.id,
                    models.Invoice.action == models.InvoiceAction.renewal,
                    models.Invoice.status == models.InvoiceStatus.pending,
                )
                .first()
            )
            if existing:
                continue
            plan = session.get(models.Plan, sub.plan_id)
            if not plan:
                continue
            invoice = models.Invoice(
                user_id=sub.user_id,
                plan_id=sub.plan_id,
                subscription_id=sub.id,
                amount=float(plan.price),
                currency="USD",
                action=models.InvoiceAction.renewal,
            )
            session.add(invoice)
            session.flush()

            # Store notification marker in a lightweight way
            user = session.get(models.User, sub.user_id)
            if user and user.telegram_id:
                log = models.AuditLog(
                    actor="system",
                    actor_type=models.AuditActor.system,
                    action="renewal_reminder",
                    target_type="subscription",
                    target_id=sub.id,
                    extra={
                        "telegram_id": user.telegram_id,
                        "invoice_id": invoice.id,
                        "expires_at": sub.expires_at.isoformat(),
                    },
                )
                session.add(log)
            stats["reminded"] += 1
        session.commit()

        # ── Remind non-auto-renew users about expiration ──
        expiring_manual = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.auto_renew.is_(False),
                models.Subscription.expires_at <= remind_horizon,
                models.Subscription.expires_at > now,
            )
            .all()
        )
        for sub in expiring_manual:
            user = session.get(models.User, sub.user_id)
            if user and user.telegram_id:
                # Check if we already sent a reminder
                existing_log = (
                    session.query(models.AuditLog)
                    .filter(
                        models.AuditLog.action == "expiry_reminder",
                        models.AuditLog.target_type == "subscription",
                        models.AuditLog.target_id == sub.id,
                    )
                    .first()
                )
                if existing_log:
                    continue
                log = models.AuditLog(
                    actor="system",
                    actor_type=models.AuditActor.system,
                    action="expiry_reminder",
                    target_type="subscription",
                    target_id=sub.id,
                    extra={
                        "telegram_id": user.telegram_id,
                        "subscription_id": sub.id,
                        "expires_at": sub.expires_at.isoformat(),
                    },
                )
                session.add(log)
        session.commit()

        RENEWAL_RUNS.labels(outcome="ok").inc()
        RENEWAL_LAST_RUN.set(now.timestamp())
    except Exception:  # noqa: BLE001
        logger.exception("Renewal check failed")
        stats["errors"] += 1
        RENEWAL_RUNS.labels(outcome="error").inc()
        if session.is_active:
            session.rollback()
    finally:
        session.close()

    # Re-enqueue self
    interval = int(os.getenv("RENEWAL_CHECK_INTERVAL", "3600"))
    if interval > 0:
        from .queue import get_queue
        from datetime import timedelta
        queue = get_queue()
        if queue is not None:
            try:
                queue.enqueue_in(
                    timedelta(seconds=interval),
                    "app.worker.run_renewal_check",
                    result_ttl=3600,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to re-enqueue renewal check")
    return stats


def run_provisioning_task(task_id: int, node_id: int | None = None) -> dict:
    """RQ job — executed by the worker process."""
    from .db import SessionLocal
    from . import models
    from .services.provisioning import ProvisioningOrchestrator

    session = SessionLocal()
    try:
        task = session.get(models.ProvisioningTask, task_id)
        if not task:
            logger.error("Task %s disappeared before worker picked it up", task_id)
            return {"status": "missing", "task_id": task_id}

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

    try:
        from .migrations import run_migrations
        run_migrations()
    except Exception:  # noqa: BLE001
        logger.exception("Worker failed to run migrations at startup")

    queue = Queue(queue_name, connection=connection)

    # Schedule autoscale tick
    autoscale_interval = int(os.getenv("AUTOSCALE_INTERVAL", "0"))
    if autoscale_interval > 0:
        try:
            from datetime import timedelta
            queue.enqueue_in(
                timedelta(seconds=autoscale_interval),
                "app.worker.run_autoscale_tick",
                result_ttl=3600,
            )
            logger.info("Autoscale bootstrapped: first tick in %ss", autoscale_interval)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule autoscale tick")

    # Schedule renewal check (default: every hour)
    renewal_interval = int(os.getenv("RENEWAL_CHECK_INTERVAL", "3600"))
    if renewal_interval > 0:
        try:
            from datetime import timedelta
            queue.enqueue_in(
                timedelta(seconds=min(renewal_interval, 60)),
                "app.worker.run_renewal_check",
                result_ttl=3600,
            )
            logger.info("Renewal check bootstrapped: first run in 60s (interval=%ss)", renewal_interval)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule renewal check")

    worker = Worker([queue], connection=connection)
    logger.info("Starting RQ worker on queue %s", queue_name)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
