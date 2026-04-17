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
DLQ_ENTRIES = Counter(
    "vpn_provisioning_dlq_total",
    "Provisioning jobs that exhausted all retries (dead-letter)",
)
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


PENDING_RESCUE = Counter(
    "vpn_provisioning_pending_rescue_total",
    "Pending provisioning tasks re-enqueued by the self-heal tick",
)


def run_pending_rescue_tick() -> dict:
    """Periodic self-heal — re-enqueue provisioning tasks that are stuck.

    When `run_task_async` enqueues into RQ, an exception inside the Redis
    client (transient connection drop, serialization hiccup) can leave the
    ProvisioningTask row committed as ``pending`` without an RQ job
    attached. ``reset_stuck_tasks`` recovers those on backend startup, but
    in steady state they sit forever until the next restart. This tick
    rescans the table every ``PENDING_RESCUE_INTERVAL`` seconds and pushes
    any ``pending`` task older than ``PENDING_RESCUE_AGE`` seconds back
    into the queue; ``enqueue_task`` has deterministic ``job_id`` dedup,
    so already-queued tasks are no-ops.
    """
    from datetime import timedelta

    from .db import SessionLocal
    from . import models
    from .queue import enqueue_task, schedule_tick
    from .time_utils import utcnow

    age = int(os.getenv("PENDING_RESCUE_AGE", "60"))
    rescued = 0
    scanned = 0

    session = SessionLocal()
    try:
        cutoff = utcnow() - timedelta(seconds=age)
        pending = (
            session.query(models.ProvisioningTask)
            .filter(
                models.ProvisioningTask.status
                == models.ProvisioningTaskStatus.pending,
                models.ProvisioningTask.created_at < cutoff,
            )
            .all()
        )
        scanned = len(pending)
        for task in pending:
            try:
                job_id = enqueue_task(task.id, None)
                if job_id:
                    rescued += 1
                    PENDING_RESCUE.inc()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "pending_rescue: failed to re-enqueue task %s", task.id
                )
    finally:
        session.close()

    if rescued:
        logger.warning(
            "pending_rescue: re-enqueued %s/%s stalled pending task(s)",
            rescued, scanned,
        )

    interval = int(os.getenv("PENDING_RESCUE_INTERVAL", "60"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_pending_rescue_tick",
                interval,
                tick_id="tick-pending-rescue",
            )
        except Exception:  # noqa: BLE001
            logger.exception("pending_rescue: failed to re-enqueue tick")

    return {"scanned": scanned, "rescued": rescued}


def run_autoscale_tick() -> list[dict]:
    """Periodic job — walk pools and scale up where needed."""
    from dataclasses import asdict

    from .db import SessionLocal
    from .queue import schedule_tick
    from .services.autoscale import evaluate_all_pools

    session = SessionLocal()
    try:
        decisions = evaluate_all_pools(session)
        result = [asdict(d) for d in decisions]
    finally:
        session.close()

    interval = int(os.getenv("AUTOSCALE_INTERVAL", "0"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_autoscale_tick",
                interval,
                tick_id="tick-autoscale",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to re-enqueue autoscale tick")
    return result


def run_renewal_check() -> dict:
    """Periodic job — find subscriptions expiring within 3 days / 1 day
    and create renewal invoices + notify users. Also expire overdue subs.

    Two reminder horizons:
      - 3 days: ``renewal_reminder`` / ``expiry_reminder`` (first nudge)
      - 1 day:  ``renewal_reminder_1d`` / ``expiry_reminder_1d`` (urgent)

    Respects ``User.notify_renewals`` — users who opted out receive no
    renewal/expiry notifications at all.

    Returns summary stats for the RQ result backend.
    """
    from datetime import timedelta

    from .db import SessionLocal
    from . import models
    from .services.provisioning import ProvisioningOrchestrator
    from .time_utils import utcnow

    session = SessionLocal()
    stats = {"reminded": 0, "reminded_1d": 0, "expired": 0, "revoked": 0, "errors": 0}
    try:
        now = utcnow()
        remind_horizon = now + timedelta(days=3)
        remind_horizon_1d = now + timedelta(days=1)
        revoke_cutoff = now - timedelta(hours=RENEWAL_GRACE_HOURS)

        # ── Mark overdue subscriptions as expired (status flip only). ──
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

        # ── Create renewal invoices for auto_renew subscriptions (3-day) ──
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

            user = session.get(models.User, sub.user_id)
            if user and user.telegram_id and user.notify_renewals:
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

        # ── 1-day urgent reminder for auto_renew subs ──
        expiring_1d = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.auto_renew.is_(True),
                models.Subscription.expires_at <= remind_horizon_1d,
                models.Subscription.expires_at > now,
            )
            .all()
        )
        for sub in expiring_1d:
            user = session.get(models.User, sub.user_id)
            if not user or not user.telegram_id or not user.notify_renewals:
                continue
            existing_log = (
                session.query(models.AuditLog)
                .filter(
                    models.AuditLog.action == "renewal_reminder_1d",
                    models.AuditLog.target_type == "subscription",
                    models.AuditLog.target_id == sub.id,
                )
                .first()
            )
            if existing_log:
                continue
            session.add(models.AuditLog(
                actor="system",
                actor_type=models.AuditActor.system,
                action="renewal_reminder_1d",
                target_type="subscription",
                target_id=sub.id,
                extra={
                    "telegram_id": user.telegram_id,
                    "subscription_id": sub.id,
                    "expires_at": sub.expires_at.isoformat(),
                },
            ))
            stats["reminded_1d"] += 1
        session.commit()

        # ── Remind non-auto-renew users about expiration (3-day) ──
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
            if not user or not user.telegram_id or not user.notify_renewals:
                continue
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
            session.add(models.AuditLog(
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
            ))
        session.commit()

        # ── 1-day urgent reminder for non-auto-renew subs ──
        expiring_manual_1d = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.auto_renew.is_(False),
                models.Subscription.expires_at <= remind_horizon_1d,
                models.Subscription.expires_at > now,
            )
            .all()
        )
        for sub in expiring_manual_1d:
            user = session.get(models.User, sub.user_id)
            if not user or not user.telegram_id or not user.notify_renewals:
                continue
            existing_log = (
                session.query(models.AuditLog)
                .filter(
                    models.AuditLog.action == "expiry_reminder_1d",
                    models.AuditLog.target_type == "subscription",
                    models.AuditLog.target_id == sub.id,
                )
                .first()
            )
            if existing_log:
                continue
            session.add(models.AuditLog(
                actor="system",
                actor_type=models.AuditActor.system,
                action="expiry_reminder_1d",
                target_type="subscription",
                target_id=sub.id,
                extra={
                    "telegram_id": user.telegram_id,
                    "subscription_id": sub.id,
                    "expires_at": sub.expires_at.isoformat(),
                },
            ))
            stats["reminded_1d"] += 1
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
        from .queue import schedule_tick
        try:
            schedule_tick(
                "app.worker.run_renewal_check",
                interval,
                tick_id="tick-renewal",
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
    from .db import SessionLocal
    from .queue import schedule_tick
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
        try:
            schedule_tick(
                "app.worker.run_warm_pool_check",
                interval,
                tick_id="tick-warm-pool",
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
    if not user or not user.telegram_id or not user.notify_renewals:
        return

    base_price = balance_svc.plan_price_kopecks(plan)
    extra_slots = sub.extra_device_slots or 0
    price = base_price + extra_slots * balance_svc.EXTRA_DEVICE_MONTHLY_KOPECKS
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
    from . import models
    from .db import SessionLocal
    from .queue import schedule_tick
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
        try:
            schedule_tick(
                "app.worker.run_balance_charge_tick",
                interval,
                tick_id="tick-balance-charge",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to re-enqueue balance charge tick")
    return stats


def run_traffic_stats_tick() -> dict:
    """Phase B — passive xray traffic stats collector.

    Walks every active/draining node, SSHs in, runs ``xray api
    statsquery --reset`` on each loopback gRPC port, persists one row
    per node into ``node_traffic_samples``. The row's ``interval_seconds``
    is the configured tick interval — the actual delta between two
    rows might drift on a slow tick, but the detector that consumes
    this only cares about per-tick rate so the configured interval is
    a "good enough" anchor.

    Self-rescheduling via ``TRAFFIC_STATS_INTERVAL`` (default 300s).
    Disabled when the env var is 0.

    Errors per node never abort the whole tick — they're logged in
    ``services.traffic_stats.collect_and_persist`` and the row is
    skipped. The next tick retries.
    """
    from .db import SessionLocal
    from .queue import schedule_tick
    from .services import traffic_stats

    interval = int(os.getenv("TRAFFIC_STATS_INTERVAL", "300"))
    summary: dict = {"collected": 0, "nodes": [], "traffic_drops": []}
    session = SessionLocal()
    try:
        rows = traffic_stats.collect_all_active_nodes(session, interval)
        summary["collected"] = len(rows)
        summary["nodes"] = rows

        # Phase D — traffic-drop detector: detect nodes where users
        # suddenly disappeared (TSPU block) and migrate to another region.
        # DISABLED 2026-04-15: детектор срабатывал на обычные idle-окна
        # и ломал активные коннекты юзерам каждые 5-10 минут. Сама
        # функция `detect_traffic_drops` возвращает пустой список, но
        # на всякий случай не вызываем её вовсе, чтобы не жечь запросы.
        # if int(os.getenv("TRAFFIC_DROP_ENABLED", "1")):
        #     try:
        #         drops = traffic_stats.detect_traffic_drops(session, rows)
        #         summary["traffic_drops"] = drops
        #         if drops:
        #             logger.warning("traffic_stats: traffic drops detected: %s", drops)
        #     except Exception:  # noqa: BLE001
        #         logger.exception("traffic_stats: traffic drop detector failed")
        #         if session.is_active:
        #             session.rollback()
    except Exception:  # noqa: BLE001
        logger.exception("traffic_stats: tick failed")
        if session.is_active:
            session.rollback()
    finally:
        session.close()

    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_traffic_stats_tick",
                interval,
                tick_id="tick-traffic-stats",
            )
        except Exception:  # noqa: BLE001
            logger.exception("traffic_stats: failed to re-enqueue tick")
    return summary


def run_relay_link_health_tick() -> dict:
    """Периодический WG-handshake poll для relay_exit_links.

    Раз в RELAY_LINK_HEALTH_INTERVAL секунд (default 300) обходит
    каждый relay с привязанными links, SSH'ит туда и читает
    ``wg show all dump``. Результат пишется в колонки
    ``relay_exit_links.last_handshake_at / last_rx_bytes /
    last_tx_bytes / last_observed_at``, откуда админка читает их
    для цветного индикатора «жив ли туннель».

    Ошибки per-relay не валят тик — просто пропускают этот relay;
    см. ``services.relay_link_health.collect_all_relay_links``.

    Self-reschedule через RELAY_LINK_HEALTH_INTERVAL. Disabled при 0.
    """
    from .db import SessionLocal
    from .queue import schedule_tick
    from .services import relay_link_health

    interval = int(os.getenv("RELAY_LINK_HEALTH_INTERVAL", "300"))
    summary: dict = {}
    session = SessionLocal()
    try:
        summary = relay_link_health.collect_all_relay_links(session)
    except Exception:  # noqa: BLE001
        logger.exception("relay_link_health: tick failed")
        if session.is_active:
            session.rollback()
    finally:
        session.close()

    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_relay_link_health_tick",
                interval,
                tick_id="tick-relay-link-health",
            )
        except Exception:  # noqa: BLE001
            logger.exception("relay_link_health: failed to re-enqueue tick")
    return summary


def run_user_health_ping_tick() -> dict:
    """Phase C — bot health-ping with consent + 24h debounce.

    Picks active subs whose user has not opted out and was not pinged
    in the last 24h. Writes one ``health_ping_request`` AuditLog row
    per chosen user (the bot's notification poller picks them up and
    delivers an inline keyboard with three buttons:
    works / doesn't work / never ask me again).

    Per-tick cap (USER_HEALTH_PING_BATCH, default 50) protects against
    a once-a-day flood when the table grows. We update
    ``User.health_ping_last_at`` *before* committing the row so a
    Telegram retry loop never double-sends to the same user — the
    next tick filters them out by the timestamp.

    Self-rescheduling via ``USER_HEALTH_PING_INTERVAL`` (default 1800s).
    Disabled when the env var is 0.
    """
    from datetime import timedelta
    from sqlalchemy import or_

    from . import models
    from .db import SessionLocal
    from .queue import schedule_tick
    from .time_utils import utcnow

    interval = int(os.getenv("USER_HEALTH_PING_INTERVAL", "1800"))
    batch = int(os.getenv("USER_HEALTH_PING_BATCH", "50"))
    debounce_hours = int(os.getenv("USER_HEALTH_PING_DEBOUNCE_HOURS", "24"))

    # Only send health pings during MSK lunch window (11:00–14:00)
    # to avoid waking users at night. Configurable via env.
    ping_hour_start = int(os.getenv("HEALTH_PING_HOUR_START", "11"))
    ping_hour_end = int(os.getenv("HEALTH_PING_HOUR_END", "14"))

    summary = {"queued": 0, "skipped": 0}
    session = SessionLocal()
    try:
        if interval <= 0 or batch <= 0:
            return summary

        now = utcnow()

        # MSK = UTC+3. Check current hour in MSK.
        msk_hour = (now.hour + 3) % 24
        if not (ping_hour_start <= msk_hour < ping_hour_end):
            logger.debug(
                "user_health_ping: outside MSK window (%02d:00–%02d:00), "
                "current MSK hour=%02d — skipping",
                ping_hour_start, ping_hour_end, msk_hour,
            )
            return summary

        debounce_cutoff = now - timedelta(hours=debounce_hours)

        # Pick users who:
        #   - have at least one active sub
        #   - have a telegram_id (otherwise the bot can't reach them)
        #   - have NOT opted out
        #   - were never pinged OR last ping is older than the debounce window
        subs = (
            session.query(models.Subscription)
            .join(models.User, models.User.id == models.Subscription.user_id)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.User.telegram_id.isnot(None),
                models.User.health_ping_opt_out.is_(False),
                or_(
                    models.User.health_ping_last_at.is_(None),
                    models.User.health_ping_last_at <= debounce_cutoff,
                ),
            )
            .order_by(models.User.health_ping_last_at.asc().nulls_first())
            .limit(batch)
            .all()
        )

        seen_users: set[int] = set()
        for sub in subs:
            user = sub.user
            if user is None or user.id in seen_users:
                summary["skipped"] += 1
                continue
            seen_users.add(user.id)

            node = sub.node
            session.add(
                models.AuditLog(
                    actor="system",
                    actor_type=models.AuditActor.system,
                    action="health_ping_request",
                    target_type="subscription",
                    target_id=sub.id,
                    extra={
                        "telegram_id": user.telegram_id,
                        "subscription_id": sub.id,
                        "node_id": node.id if node else None,
                        "node_name": node.name if node else None,
                    },
                )
            )
            user.health_ping_last_at = now
            session.add(user)
            summary["queued"] += 1

        if summary["queued"] > 0:
            session.commit()
    except Exception:
        logger.exception("user_health_ping: tick failed")
        if session.is_active:
            session.rollback()
    finally:
        session.close()

    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_user_health_ping_tick",
                interval,
                tick_id="tick-health-ping",
            )
        except Exception:  # noqa: BLE001
            logger.exception("user_health_ping: failed to re-enqueue tick")
    return summary


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


def dlq_exception_handler(job, exc_type, exc_value, tb):  # noqa: ARG001
    """Called by RQ when a job permanently fails (all retries exhausted).

    Writes an AuditLog entry so ops can see the failure in the admin UI
    without digging through Redis.  Also bumps the Prometheus counter.
    """
    DLQ_ENTRIES.inc()
    task_id = job.args[0] if job.args else None
    logger.error(
        "Provisioning job %s (task %s) dead-lettered after retries: %s",
        job.id, task_id, exc_value,
    )
    if task_id is None:
        return True  # let RQ continue its normal failure flow

    try:
        from .db import SessionLocal
        from . import models

        session = SessionLocal()
        try:
            session.add(
                models.AuditLog(
                    actor="rq_worker",
                    actor_type=models.AuditActor.system,
                    action="provisioning_dlq",
                    target_type="provisioning_task",
                    target_id=task_id,
                    extra={
                        "job_id": job.id,
                        "error": str(exc_value)[:500],
                        "exc_type": exc_type.__name__ if exc_type else None,
                    },
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write DLQ audit log for task %s", task_id)

    return True  # let RQ continue its normal failure flow


def main() -> None:
    from .logging_config import configure_logging

    configure_logging()
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

    # Schedule pending-task rescue (default: every minute). Re-enqueues
    # provisioning tasks stuck in ``pending`` — typically because an RQ
    # enqueue hit a transient Redis error at creation time and the task
    # row was committed without a job attached. Short interval by design:
    # the impact of a stalled user-facing provisioning is high.
    #
    # All bootstraps below use ``schedule_tick`` (deterministic job_id per
    # tick). Pre-fix, raw ``queue.enqueue_in`` generated a new UUID on
    # each worker restart, so restart-triggered bootstrap enqueued a job
    # *alongside* the still-scheduled one from the prior incarnation —
    # N restarts → N parallel chains per tick. See ``queue.schedule_tick``
    # and ``docs/components/worker.md`` § Дедупликация тиков.
    pending_rescue_interval = int(os.getenv("PENDING_RESCUE_INTERVAL", "60"))
    if pending_rescue_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_pending_rescue_tick",
                min(pending_rescue_interval, 30),
                tick_id="tick-pending-rescue",
            )
            logger.info(
                "Pending-rescue tick bootstrapped: first run in 30s "
                "(interval=%ss)", pending_rescue_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule pending-rescue tick")

    # Schedule autoscale tick
    autoscale_interval = int(os.getenv("AUTOSCALE_INTERVAL", "0"))
    if autoscale_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_autoscale_tick",
                autoscale_interval,
                tick_id="tick-autoscale",
            )
            logger.info("Autoscale bootstrapped: first tick in %ss", autoscale_interval)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule autoscale tick")

    # Schedule renewal check (default: every hour)
    renewal_interval = int(os.getenv("RENEWAL_CHECK_INTERVAL", "3600"))
    if renewal_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_renewal_check",
                min(renewal_interval, 60),
                tick_id="tick-renewal",
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
            schedule_tick(
                "app.worker.run_warm_pool_check",
                min(warm_interval, 30),
                tick_id="tick-warm-pool",
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
            schedule_tick(
                "app.worker.run_balance_charge_tick",
                min(balance_interval, 60),
                tick_id="tick-balance-charge",
            )
            logger.info(
                "Balance charge tick bootstrapped: first run in 60s (interval=%ss)",
                balance_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule balance charge tick")

    # Phase B — passive xray stats collector. SSHs into each active
    # node every TRAFFIC_STATS_INTERVAL seconds (default 300) and
    # writes a row into node_traffic_samples. Disabled when set to 0.
    traffic_stats_interval = int(os.getenv("TRAFFIC_STATS_INTERVAL", "300"))
    if traffic_stats_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_traffic_stats_tick",
                min(traffic_stats_interval, 60),
                tick_id="tick-traffic-stats",
            )
            logger.info(
                "Traffic stats tick bootstrapped: first run in 60s (interval=%ss)",
                traffic_stats_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule traffic stats tick")

    # Health relay↔exit tunnels. Читает wg show all dump с каждого
    # relay, апдейтит last_handshake_at / rx / tx / observed_at в
    # relay_exit_links. Интервал RELAY_LINK_HEALTH_INTERVAL (default
    # 300s), Disabled at 0.
    relay_link_health_interval = int(os.getenv("RELAY_LINK_HEALTH_INTERVAL", "300"))
    if relay_link_health_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_relay_link_health_tick",
                min(relay_link_health_interval, 60),
                tick_id="tick-relay-link-health",
            )
            logger.info(
                "Relay-link health tick bootstrapped: first run in 60s (interval=%ss)",
                relay_link_health_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule relay-link health tick")

    # Phase C — bot health-ping with consent. Queues a friendly
    # "помогите нам улучшить сервис" prompt to active users at most
    # once per USER_HEALTH_PING_DEBOUNCE_HOURS, capped at
    # USER_HEALTH_PING_BATCH per tick. Disabled when set to 0.
    health_ping_interval = int(os.getenv("USER_HEALTH_PING_INTERVAL", "1800"))
    if health_ping_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_user_health_ping_tick",
                min(health_ping_interval, 60),
                tick_id="tick-health-ping",
            )
            logger.info(
                "User health-ping tick bootstrapped: first run in 60s (interval=%ss)",
                health_ping_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule user health-ping tick")

    worker = Worker(
        [queue],
        connection=connection,
        exception_handlers=[dlq_exception_handler],
    )
    logger.info("Starting RQ worker on queue %s", queue_name)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
