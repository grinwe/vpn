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

# NB: this module MUST be imported under its canonical name `app.worker`, not
# as `__main__`. RQ executes jobs by calling importlib.import_module("app.worker"),
# and if the module was originally loaded as `__main__` (via `python -m app.worker`),
# Python re-executes this file on the RQ side — re-registering the metrics
# below into the global CollectorRegistry and crashing every single job with
# "Duplicated timeseries in CollectorRegistry". The container entrypoint
# (Dockerfile.worker) therefore uses `python -c "from app.worker import main; main()"`
# so sys.modules has `app.worker` from the first import and RQ's re-import
# is a no-op. Do NOT revert to `python -m app.worker`.
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


def run_drain_tick() -> dict:
    """Stage 5 — drive the downscale path.

    Two phases per tick:

    1. Mark phase: ``evaluate_all_downscale`` walks every pool and may
       flip one node per pool to ``draining`` (gated by hysteresis +
       min_nodes + the master switch).

    2. Migrate / destroy phase: for every node currently in ``draining``
       state, walk a batch of its active subscriptions and call
       ``migrate_subscription_to_new_node`` on each. If the migration
       leaves the node with zero active subs AND ``draining_at`` was
       set more than ``DRAIN_GRACE_HOURS`` ago, call ``destroy_node``
       to actually delete the VM.

    Self-rescheduling via ``DRAIN_TICK_INTERVAL`` (default 600s).
    """
    from datetime import timedelta

    from .db import SessionLocal
    from .queue import get_queue
    from . import models
    from .services import autoscale
    from .services.node_spawner import NodeSpawnError, destroy_node
    from .services.provisioning import ProvisioningOrchestrator
    from .time_utils import utcnow

    stats: dict = {
        "marked": 0,
        "migrated": 0,
        "migration_errors": 0,
        "destroyed": 0,
        "destroy_errors": 0,
    }
    session = SessionLocal()
    try:
        # Phase 1 — mark.
        try:
            decisions = autoscale.evaluate_all_downscale(session)
            stats["marked"] = sum(1 for d in decisions if d.marked_node_id is not None)
        except Exception:  # noqa: BLE001
            logger.exception("evaluate_all_downscale failed")

        # Phase 2 — migrate + destroy. Re-query each tick so we pick up
        # nodes marked by phase 1 in this same run.
        draining_nodes = (
            session.query(models.VPNNode)
            .filter(models.VPNNode.status == models.VPNNodeStatus.draining)
            .all()
        )
        batch_size = int(os.getenv("DRAIN_MIGRATE_BATCH", "10"))
        grace_hours = int(os.getenv("AUTOSCALE_DRAIN_GRACE_HOURS", "24"))
        orchestrator = ProvisioningOrchestrator(session)

        for node in draining_nodes:
            live_subs = (
                session.query(models.Subscription)
                .filter(
                    models.Subscription.node_id == node.id,
                    models.Subscription.status == models.SubscriptionStatus.active,
                )
                .order_by(models.Subscription.id.asc())
                .limit(batch_size)
                .all()
            )
            autoscale.DRAIN_SUBS_REMAINING.labels(
                pool=node.pool.name if node.pool else "unknown",
                node=node.name,
            ).set(len(live_subs))

            for sub in live_subs:
                try:
                    orchestrator.migrate_subscription_to_new_node(sub)
                    stats["migrated"] += 1
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "migrate_subscription failed for sub %s on node %s",
                        sub.id,
                        node.id,
                    )
                    stats["migration_errors"] += 1

            # Re-count after the batch — if any subs survive, defer
            # destroy to a later tick.
            remaining = (
                session.query(models.Subscription)
                .filter(
                    models.Subscription.node_id == node.id,
                    models.Subscription.status == models.SubscriptionStatus.active,
                )
                .count()
            )
            autoscale.DRAIN_SUBS_REMAINING.labels(
                pool=node.pool.name if node.pool else "unknown",
                node=node.name,
            ).set(remaining)
            if remaining > 0:
                continue

            # Grace check: use ``updated_at`` as a proxy for "when we
            # last touched this node". The status flip in
            # evaluate_pool_downscale stamps it, and so does the final
            # migrate batch — meaning we wait for ``grace_hours`` of
            # quiet, not from the original mark.
            if node.updated_at and (utcnow() - node.updated_at) < timedelta(hours=grace_hours):
                continue

            try:
                destroy_node(session, node)
                stats["destroyed"] += 1
                logger.warning(
                    "Drain tick: destroyed node %s after grace window", node.name
                )
            except NodeSpawnError:
                logger.exception("destroy_node failed for %s", node.name)
                stats["destroy_errors"] += 1
    finally:
        session.close()

    interval = int(os.getenv("DRAIN_TICK_INTERVAL", "600"))
    if interval > 0:
        queue = get_queue()
        if queue is not None:
            try:
                queue.enqueue_in(
                    timedelta(seconds=interval),
                    "app.worker.run_drain_tick",
                    result_ttl=3600,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to re-enqueue drain tick")
    return stats


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


def run_warm_pool_check() -> dict:
    """Periodic job — top up warm credential pools on every active node.

    Stage 2.5 of the WebApp roadmap. Reads ``WARM_POOL_TARGET`` /
    ``WARM_POOL_BATCH_PER_TICK`` for sizing, ``WARM_POOL_CHECK_INTERVAL``
    for scheduling. Self-reschedules at the end so a single startup
    ``enqueue_in`` produces a recurring tick without depending on
    rq-scheduler.

    Returns a ``{node_id: warmed_count}`` summary so the RQ result
    backend captures a useful audit trail per tick.
    """
    from datetime import timedelta

    from .db import SessionLocal
    from .queue import get_queue
    from .services import warm_pool

    session = SessionLocal()
    summary: dict = {}
    try:
        summary = warm_pool.ensure_pool(session)
    except Exception:  # noqa: BLE001
        logger.exception("warm_pool: ensure_pool failed")
    finally:
        session.close()

    interval = int(os.getenv("WARM_POOL_CHECK_INTERVAL", "120"))
    if interval > 0:
        queue = get_queue()
        if queue is not None:
            try:
                queue.enqueue_in(
                    timedelta(seconds=interval),
                    "app.worker.run_warm_pool_check",
                    result_ttl=3600,
                )
            except Exception:  # noqa: BLE001
                logger.exception("warm_pool: failed to re-enqueue tick")
    return summary


_LOW_BALANCE_THRESHOLD_DAYS = int(os.getenv("LOW_BALANCE_WARN_DAYS", "3"))


def _maybe_emit_low_balance_warning(session, sub) -> None:
    """Write an AuditLog notification when the next renewal can't be covered.

    V2: after a successful renew, check whether the user's wallet can
    afford at least one more renewal. If not, warn them to top up.
    Idempotent per calendar day.
    """
    from . import models
    from .services import balance as balance_svc
    from .time_utils import utcnow

    plan = sub.plan
    if not plan:
        return
    user = sub.user
    if not user or not user.telegram_id:
        return

    price = balance_svc.plan_price_kopecks(plan)
    if price <= 0:
        return
    wallet = user.balance_kopecks or 0
    if wallet >= price:
        return  # can afford at least one more renewal

    today = utcnow().date().isoformat()
    existing = (
        session.query(models.AuditLog)
        .filter(
            models.AuditLog.action == "low_balance_warning",
            models.AuditLog.target_type == "user",
            models.AuditLog.target_id == user.id,
            models.AuditLog.created_at >= utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
        )
        .first()
    )
    if existing:
        return

    days_until_expire = 0
    if sub.expires_at:
        delta = (sub.expires_at - utcnow()).total_seconds()
        days_until_expire = max(int(delta // 86400), 0)

    session.add(
        models.AuditLog(
            actor="system",
            actor_type=models.AuditActor.system,
            action="low_balance_warning",
            target_type="user",
            target_id=user.id,
            extra={
                "telegram_id": user.telegram_id,
                "days_remaining": days_until_expire,
                "balance_rub": round(wallet / 100, 2),
                "subscription_id": sub.id,
                "date": today,
            },
        )
    )


def _run_trial_expiry_pass(session, stats: dict) -> None:
    """Trial expiry: T-3 warning, T=0 clawback for non-paying users.

    Called once per ``run_balance_charge_tick``. Idempotent:
      * Warnings dedup on existing ``trial_expiry_warning[:delivered]``
        AuditLog for the user — we never fire twice for the same trial.
      * Clawbacks dedup on the ``trial_expiry_clawback:<uid>`` reference
        and additionally clear ``trial_expires_at`` so the next tick
        doesn't even enter the loop.
    Users with at least one ``kind=topup`` are considered "earned" —
    they keep the bonus and we just clear ``trial_expires_at``.
    """
    from datetime import timedelta

    from . import models
    from .services import balance as balance_svc
    from .time_utils import utcnow

    now = utcnow()
    warn_cutoff = now + timedelta(days=balance_svc.TRIAL_EXPIRY_WARN_DAYS)

    # ── 3a. Warnings ──────────────────────────────────────────────────
    pending_warn = (
        session.query(models.User)
        .filter(
            models.User.trial_expires_at.isnot(None),
            models.User.trial_expires_at <= warn_cutoff,
            models.User.trial_expires_at > now,
            models.User.telegram_id.isnot(None),
        )
        .limit(200)
        .all()
    )
    for u in pending_warn:
        already = (
            session.query(models.AuditLog)
            .filter(
                models.AuditLog.target_type == "user",
                models.AuditLog.target_id == u.id,
                models.AuditLog.action.in_(
                    ["trial_expiry_warning", "trial_expiry_warning:delivered"]
                ),
            )
            .first()
        )
        if already:
            continue
        session.add(
            models.AuditLog(
                actor="system",
                actor_type=models.AuditActor.system,
                action="trial_expiry_warning",
                target_type="user",
                target_id=u.id,
                extra={
                    "telegram_id": u.telegram_id,
                    "expires_at": u.trial_expires_at.isoformat(),
                },
            )
        )
        stats["trial_warned"] = stats.get("trial_warned", 0) + 1
    session.commit()

    # ── 3b. Clawbacks ─────────────────────────────────────────────────
    expired = (
        session.query(models.User)
        .filter(
            models.User.trial_expires_at.isnot(None),
            models.User.trial_expires_at <= now,
            models.User.trial_activated_at.isnot(None),
        )
        .limit(200)
        .all()
    )
    for u in expired:
        # Paying customer? Keep the bonus, just clear the timer so we
        # don't revisit this user every tick.
        has_topup = (
            session.query(models.BalanceTransaction)
            .filter_by(user_id=u.id, kind=models.BalanceTxKind.topup)
            .first()
        )
        if has_topup is not None:
            u.trial_expires_at = None
            session.add(u)
            stats["trial_kept"] = stats.get("trial_kept", 0) + 1
            continue

        ref = f"trial_expiry_clawback:{u.id}"
        prior = (
            session.query(models.BalanceTransaction)
            .filter_by(reference=ref)
            .first()
        )
        if prior is not None:
            u.trial_expires_at = None
            session.add(u)
            continue

        # Clawback sized to the trial amount that's currently live, but
        # capped at the user's balance so we can't push them negative.
        # adjustment() already floors at balance, but we also compute
        # the trial amount here so the ledger note matches what we
        # meant to take.
        trial_amount = balance_svc_trial_amount(session)
        take = min(trial_amount, u.balance_kopecks or 0)
        if take > 0:
            balance_svc.adjustment(
                session,
                u.id,
                -take,
                reference=ref,
                note="trial_expiry_clawback",
            )
            stats["trial_clawback"] = stats.get("trial_clawback", 0) + 1
        u.trial_expires_at = None
        session.add(u)
    session.commit()


def balance_svc_trial_amount(session) -> int:
    """Thin wrapper around ``services.trial.trial_amount_kopecks``.

    Defined at module scope (rather than inlined) so mocking it in
    tests is trivial. Imports lazily to dodge the circular-import risk
    between ``worker`` and ``services.trial`` (both pull ``models``).
    """
    from .services import trial as trial_svc

    return trial_svc.trial_amount_kopecks(session)


def run_balance_charge_tick() -> dict:
    """Periodic job — V2 monthly billing tick.

    Three passes per tick:

      1. **Renew.** Active subs with ``expires_at <= now`` and
         ``auto_renew = True``. Debit ``plan.price`` from wallet,
         extend ``expires_at += plan.duration_days``. On insufficient
         balance → expire.

      2. **Expire non-renewing.** Active subs with ``expires_at <= now``
         and ``auto_renew = False`` → flip to expired.

      3. **Auto-unfreeze.** Frozen subs with ``frozen_until <= now``.

      4. **Trial expiry.** T-3 warning + T=0 clawback.

    Self-reschedules at the end.
    """
    from datetime import timedelta

    from . import models
    from .db import SessionLocal
    from .queue import get_queue
    from .services import balance
    from .time_utils import utcnow

    session = SessionLocal()
    stats = {"renewed": 0, "insufficient": 0, "expired_norenew": 0,
             "unfrozen": 0, "errors": 0}
    try:
        now = utcnow()

        # ── Pass 1: renew due active subs (auto_renew=True) ───────────
        due_renew = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.auto_renew.is_(True),
                models.Subscription.expires_at.isnot(None),
                models.Subscription.expires_at <= now,
            )
            .with_for_update(skip_locked=True)
            .limit(500)
            .all()
        )
        for sub in due_renew:
            try:
                ok = balance.renew_subscription(session, sub)
            except Exception:
                logger.exception("balance: renew failed for sub %s", sub.id)
                stats["errors"] += 1
                session.rollback()
                continue
            if ok:
                stats["renewed"] += 1
                _maybe_emit_low_balance_warning(session, sub)
            else:
                sub.status = models.SubscriptionStatus.expired
                session.add(sub)
                stats["insufficient"] += 1
            session.commit()

        # ── Pass 2: expire non-renewing subs ──────────────────────────
        due_expire = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.auto_renew.is_(False),
                models.Subscription.expires_at.isnot(None),
                models.Subscription.expires_at <= now,
            )
            .with_for_update(skip_locked=True)
            .limit(500)
            .all()
        )
        for sub in due_expire:
            sub.status = models.SubscriptionStatus.expired
            session.add(sub)
            stats["expired_norenew"] += 1
        if due_expire:
            session.commit()

        # ── Pass 3: auto-unfreeze expired pauses ─────────────────────
        expired_freezes = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.frozen,
                models.Subscription.frozen_until.isnot(None),
                models.Subscription.frozen_until <= now,
            )
            .with_for_update(skip_locked=True)
            .limit(100)
            .all()
        )
        for sub in expired_freezes:
            try:
                balance.unfreeze_subscription(session, sub, auto=True)
                stats["unfrozen"] += 1
                session.commit()
            except Exception:
                logger.exception("balance: auto-unfreeze failed for sub %s", sub.id)
                stats["errors"] += 1
                session.rollback()

        # ── Pass 4: trial expiry ──────────────────────────────────────
        try:
            _run_trial_expiry_pass(session, stats)
        except Exception:
            logger.exception("balance: trial expiry pass failed")
            stats["errors"] += 1
            if session.is_active:
                session.rollback()
    except Exception:
        logger.exception("balance charge tick failed")
        stats["errors"] += 1
        if session.is_active:
            session.rollback()
    finally:
        session.close()

    interval = int(os.getenv("BALANCE_CHARGE_INTERVAL", "3600"))
    if interval > 0:
        queue = get_queue()
        if queue is not None:
            try:
                queue.enqueue_in(
                    timedelta(seconds=interval),
                    "app.worker.run_balance_charge_tick",
                    result_ttl=3600,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to re-enqueue balance charge tick")
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

    # Schedule warm-pool check (default: every 2 min). Stage 2.5 of the
    # WebApp roadmap — keeps each active node's pool topped up so user
    # purchases hit a warm bundle instead of paying the Ansible cost.
    warm_interval = int(os.getenv("WARM_POOL_CHECK_INTERVAL", "120"))
    warm_enabled = os.getenv("WARM_POOL_ENABLED", "1").lower() not in {"0", "false", "no"}
    if warm_interval > 0 and warm_enabled:
        try:
            from datetime import timedelta
            queue.enqueue_in(
                timedelta(seconds=min(warm_interval, 30)),
                "app.worker.run_warm_pool_check",
                result_ttl=3600,
            )
            logger.info(
                "Warm pool check bootstrapped: first run in 30s (interval=%ss, target=%s)",
                warm_interval, os.getenv("WARM_POOL_TARGET", "10"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule warm pool check")

    # Schedule balance charge tick (default: hourly). Stage 4 — drives
    # daily-billing ticks for balance subscriptions and auto-unfreezes
    # paused ones whose frozen_until has lapsed.
    balance_interval = int(os.getenv("BALANCE_CHARGE_INTERVAL", "3600"))
    if balance_interval > 0:
        try:
            from datetime import timedelta
            queue.enqueue_in(
                timedelta(seconds=min(balance_interval, 60)),
                "app.worker.run_balance_charge_tick",
                result_ttl=3600,
            )
            logger.info(
                "Balance charge tick bootstrapped: first run in 60s (interval=%ss)",
                balance_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule balance charge tick")

    # Schedule drain tick (default: every 10 min). Stage 5 — drives the
    # downscale path. Even when ``AUTOSCALE_DOWNSCALE_ENABLED=0`` the
    # tick still runs so already-draining nodes (set manually by an
    # operator via the admin) get migrated + destroyed.
    drain_interval = int(os.getenv("DRAIN_TICK_INTERVAL", "600"))
    if drain_interval > 0:
        try:
            from datetime import timedelta
            queue.enqueue_in(
                timedelta(seconds=min(drain_interval, 60)),
                "app.worker.run_drain_tick",
                result_ttl=3600,
            )
            logger.info(
                "Drain tick bootstrapped: first run in 60s (interval=%ss)",
                drain_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule drain tick")

    worker = Worker([queue], connection=connection)
    logger.info("Starting RQ worker on queue %s", queue_name)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
