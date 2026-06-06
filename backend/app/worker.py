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

    # Перепланируем ДО начала работы. Если body упадёт / будет убит по
    # job_timeout — следующий запуск уже в ScheduledJobRegistry. replace=True
    # обязателен: текущий job в "started", default-dedup вернул бы early.
    interval = int(os.getenv("PENDING_RESCUE_INTERVAL", "60"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_pending_rescue_tick",
                interval,
                tick_id="tick-pending-rescue",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("pending_rescue: failed to re-enqueue tick (at start)")

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

    return {"scanned": scanned, "rescued": rescued}


def run_autoscale_tick() -> list[dict]:
    """Periodic job — walk pools and scale up where needed."""
    from dataclasses import asdict

    from .db import SessionLocal
    from .queue import schedule_tick
    from .services.autoscale import evaluate_all_pools

    # Reschedule в начале — см. run_pending_rescue_tick.
    interval = int(os.getenv("AUTOSCALE_INTERVAL", "0"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_autoscale_tick",
                interval,
                tick_id="tick-autoscale",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to re-enqueue autoscale tick (at start)")

    session = SessionLocal()
    try:
        decisions = evaluate_all_pools(session)
        result = [asdict(d) for d in decisions]
    finally:
        session.close()

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
    from .queue import schedule_tick
    from .services.provisioning import ProvisioningOrchestrator
    from .time_utils import utcnow

    # Reschedule в начале — см. run_pending_rescue_tick.
    interval = int(os.getenv("RENEWAL_CHECK_INTERVAL", "3600"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_renewal_check",
                interval,
                tick_id="tick-renewal",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to re-enqueue renewal check (at start)")

    session = SessionLocal()
    stats = {"reminded": 0, "reminded_1d": 0, "expired": 0, "revoked": 0, "errors": 0}
    try:
        now = utcnow()
        remind_horizon = now + timedelta(days=3)
        remind_horizon_1d = now + timedelta(days=1)
        revoke_cutoff = now - timedelta(hours=RENEWAL_GRACE_HOURS)

        # ── Mark overdue subscriptions as expired (status flip only). ──
        # Для auto_renew=True сначала пробуем V2 balance.renew_subscription:
        # `run_renewal_check` живёт на 5-min cadence, `run_balance_charge_tick`
        # на часовом — без этого race-fix-а час между balance-тиками означает,
        # что мы успеваем флипнуть в expired раньше, чем balance успеет
        # списать с кошелька, и подписка с достаточным балансом и тумблером
        # auto_renew=ON всё равно не продлевается.
        from .services import balance as balance_svc
        overdue = (
            session.query(models.Subscription)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.expires_at < now,
            )
            .all()
        )
        for sub in overdue:
            if sub.auto_renew:
                try:
                    if balance_svc.renew_subscription(session, sub):
                        stats["renewed"] = stats.get("renewed", 0) + 1
                        continue
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "renewal_check: balance.renew failed sub=%s", sub.id
                    )
                    if session.is_active:
                        session.rollback()
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
            if not user or not user.telegram_id or not user.notify_renewals:
                stats["reminded"] += 1
                continue
            # Skip notification if balance covers next renewal — V2
            # balance tick will silently auto-renew, no need to bug user.
            wallet = user.balance_kopecks or 0
            cost = balance_svc.total_renewal_cost_kopecks(sub)
            if cost > 0 and wallet >= cost:
                stats["reminded"] += 1
                continue
            # Idempotency in addition to invoice-check: invoice can be
            # marked paid/cancelled by admin, after which the existing
            # invoice query returns nothing and we'd otherwise re-spam.
            existing_log = (
                session.query(models.AuditLog)
                .filter(
                    models.AuditLog.action.in_(
                        ["renewal_reminder", "renewal_reminder:delivered"]
                    ),
                    models.AuditLog.target_type == "subscription",
                    models.AuditLog.target_id == sub.id,
                )
                .first()
            )
            if existing_log:
                stats["reminded"] += 1
                continue
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
        logger.info(
            "renewal_check.expiring_1d: %d sub(s) in window [now, now+1d]",
            len(expiring_1d),
        )
        for sub in expiring_1d:
            user = session.get(models.User, sub.user_id)
            if not user or not user.telegram_id or not user.notify_renewals:
                logger.info(
                    "renewal_check.1d: sub=%s skip (no user/tg/notify_renewals)",
                    sub.id,
                )
                continue
            # Same balance-gate as 3-day: silent auto-renew, no need to bug.
            wallet = user.balance_kopecks or 0
            cost = balance_svc.total_renewal_cost_kopecks(sub)
            if cost > 0 and wallet >= cost:
                logger.info(
                    "renewal_check.1d: sub=%s skip balance-gate wallet=%s cost=%s",
                    sub.id, wallet, cost,
                )
                continue
            # `.in_(...)` covers post-ACK state: bot's POST /ack appends
            # `:delivered` to action, so a plain `== "renewal_reminder_1d"`
            # check would miss the prior log and re-spam every 5-min tick.
            existing_log = (
                session.query(models.AuditLog)
                .filter(
                    models.AuditLog.action.in_(
                        ["renewal_reminder_1d", "renewal_reminder_1d:delivered"]
                    ),
                    models.AuditLog.target_type == "subscription",
                    models.AuditLog.target_id == sub.id,
                )
                .first()
            )
            # Дополнительная диагностика для разбора майского спама: смотрим
            # ВСЁ, что лежит в audit_logs по этой подписке, чтобы поймать
            # action со странным суффиксом (типа ":delivered:delivered"),
            # mismatched target_id, NULL target_id и т.д.
            all_for_sub = (
                session.query(models.AuditLog.id, models.AuditLog.action)
                .filter(
                    models.AuditLog.target_type == "subscription",
                    models.AuditLog.target_id == sub.id,
                )
                .order_by(models.AuditLog.created_at.desc())
                .limit(10)
                .all()
            )
            if existing_log:
                logger.info(
                    "renewal_check.1d: sub=%s SKIP dedup matched log_id=%s action=%r "
                    "(all_recent=%s)",
                    sub.id, existing_log.id, existing_log.action,
                    [(i, a) for (i, a) in all_for_sub],
                )
                continue
            new_log = models.AuditLog(
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
            )
            session.add(new_log)
            logger.warning(
                "renewal_check.1d: sub=%s CREATED new log user=%s wallet=%s cost=%s "
                "(all_recent_for_sub=%s) — investigate why dedup missed",
                sub.id, user.id, wallet, cost,
                [(i, a) for (i, a) in all_for_sub],
            )
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
                    models.AuditLog.action.in_(
                        ["expiry_reminder", "expiry_reminder:delivered"]
                    ),
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
        logger.info(
            "renewal_check.expiring_manual_1d: %d sub(s) in window",
            len(expiring_manual_1d),
        )
        for sub in expiring_manual_1d:
            user = session.get(models.User, sub.user_id)
            if not user or not user.telegram_id or not user.notify_renewals:
                logger.info(
                    "renewal_check.manual_1d: sub=%s skip (no user/tg/notify)",
                    sub.id,
                )
                continue
            existing_log = (
                session.query(models.AuditLog)
                .filter(
                    models.AuditLog.action.in_(
                        ["expiry_reminder_1d", "expiry_reminder_1d:delivered"]
                    ),
                    models.AuditLog.target_type == "subscription",
                    models.AuditLog.target_id == sub.id,
                )
                .first()
            )
            all_for_sub = (
                session.query(models.AuditLog.id, models.AuditLog.action)
                .filter(
                    models.AuditLog.target_type == "subscription",
                    models.AuditLog.target_id == sub.id,
                )
                .order_by(models.AuditLog.created_at.desc())
                .limit(10)
                .all()
            )
            if existing_log:
                logger.info(
                    "renewal_check.manual_1d: sub=%s SKIP dedup matched "
                    "log_id=%s action=%r (all_recent=%s)",
                    sub.id, existing_log.id, existing_log.action,
                    [(i, a) for (i, a) in all_for_sub],
                )
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
            logger.warning(
                "renewal_check.manual_1d: sub=%s CREATED new log "
                "(all_recent_for_sub=%s) — investigate why dedup missed",
                sub.id, [(i, a) for (i, a) in all_for_sub],
            )
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

    # Reschedule в начале — см. run_pending_rescue_tick.
    interval = int(os.getenv("WARM_POOL_CHECK_INTERVAL", "120"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_warm_pool_check",
                interval,
                tick_id="tick-warm-pool",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("warm_pool: failed to re-enqueue tick (at start)")

    session = SessionLocal()
    summary: dict = {}
    try:
        summary = warm_pool.ensure_pool(session)
    except Exception:  # noqa: BLE001
        logger.exception("warm_pool: ensure_pool failed")
    finally:
        session.close()

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

    price = balance_svc.total_renewal_cost_kopecks(sub)
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

    # Reschedule в начале — см. run_pending_rescue_tick.
    interval = int(os.getenv("BALANCE_CHARGE_INTERVAL", "3600"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_balance_charge_tick",
                interval,
                tick_id="tick-balance-charge",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to re-enqueue balance charge tick (at start)")

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

    # Reschedule в начале — см. run_pending_rescue_tick. Для traffic-stats
    # это особенно критично: SSH-сессии по всем нодам регулярно зависают
    # и job убивается по job_timeout=120s, end-of-body reschedule бы не
    # выполнился.
    interval = int(os.getenv("TRAFFIC_STATS_INTERVAL", "300"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_traffic_stats_tick",
                interval,
                tick_id="tick-traffic-stats",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("traffic_stats: failed to re-enqueue tick (at start)")

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

    return summary


def _auto_diagnose_stale_links(session) -> dict:
    """Auto-trigger diagnostics for relay→exit WG links with stale handshakes.

    Сценарий: WG-туннель прицеплен, last_observed_at свежий (= relay
    отвечает на SSH тик и `wg show all dump` парсится), но
    last_handshake_at либо NULL (handshake никогда не проходил), либо
    отстал больше чем AUTO_DIAGNOSE_HANDSHAKE_STALE_MIN минут. Это
    тот самый паттерн, который оператор ловил руками на новой ноде
    после attach к exit'у.

    Каждому подозрительному линку ENQUEUE'ится diagnose-task с урезанным
    набором check_types (handshake-side только — full набор ~20s, этот
    ~5s) и audit_log запись `symptom_detected`, по которой потом дебаунс
    решает «уже бежал недавно, ждём результата».

    Env vars:
      AUTO_DIAGNOSE_ENABLED                 default true
      AUTO_DIAGNOSE_HANDSHAKE_STALE_MIN     default 10
      AUTO_DIAGNOSE_OBSERVED_FRESH_MIN      default 8  (=relay тик прошёл недавно)
      AUTO_DIAGNOSE_DEBOUNCE_MIN            default 30 (per-link дедуп)
      AUTO_DIAGNOSE_MAX_PER_TICK            default 3  (rate-limit на tick)
    """
    if os.getenv("AUTO_DIAGNOSE_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return {"enabled": False, "enqueued": []}

    from datetime import timedelta

    from sqlalchemy import func as sa_func

    from . import models
    from .services.provisioning import ProvisioningOrchestrator
    from .time_utils import utcnow

    stale_hs_min = int(os.getenv("AUTO_DIAGNOSE_HANDSHAKE_STALE_MIN", "10"))
    observed_fresh_min = int(os.getenv("AUTO_DIAGNOSE_OBSERVED_FRESH_MIN", "8"))
    debounce_min = int(os.getenv("AUTO_DIAGNOSE_DEBOUNCE_MIN", "30"))
    max_per_tick = int(os.getenv("AUTO_DIAGNOSE_MAX_PER_TICK", "3"))

    now = utcnow()
    observed_cutoff = now - timedelta(minutes=observed_fresh_min)
    stale_hs_cutoff = now - timedelta(minutes=stale_hs_min)
    debounce_cutoff = now - timedelta(minutes=debounce_min)

    # Кандидаты: тик отработал по этому relay недавно, handshake либо
    # пустой, либо старый. last_observed_at IS NULL отсекаем — пока
    # один SSH ни разу не прошёл, диагностика всё равно не сможет
    # сделать `wg show` со стороны jump.
    #
    # JOIN с VPNNode + filter VPNNode.diagnostics_disabled_at IS NULL —
    # оператор выключил диагностику ноды (новый hard-тумблер, migration
    # 0039; старый auto_diagnose_disabled_at в неё забэкфилен). getattr-
    # guard для backward-compat: если 0039 ещё не накатилась, фильтр
    # пропускаем (worst case: лишние diagnose тики, но не падение).
    node_disabled_col = getattr(models.VPNNode, "diagnostics_disabled_at", None)

    # ORDER BY (handshake_at IS NULL DESC, handshake_at ASC) — NULL первыми
    # (never observed = worst), потом самые stale → за один tick покрываем
    # худшие случаи раньше всего. LIMIT max_per_tick прямо в запросе —
    # candidate-list не превышает rate-limit, дебаунс не нужно скипать
    # лишние раз. Остальные unhealthy подождут следующего tick (через
    # 5 мин, что safely ниже debounce window 30 мин).
    candidates_q = (
        session.query(models.RelayExitLink)
        .join(models.VPNNode, models.VPNNode.id == models.RelayExitLink.relay_node_id)
        .filter(models.RelayExitLink.last_observed_at.isnot(None))
        .filter(models.RelayExitLink.last_observed_at >= observed_cutoff)
        .filter(
            (models.RelayExitLink.last_handshake_at.is_(None))
            | (models.RelayExitLink.last_handshake_at < stale_hs_cutoff)
        )
    )
    if node_disabled_col is not None:
        candidates_q = candidates_q.filter(node_disabled_col.is_(None))
    candidates = (
        candidates_q
        .order_by(
            sa_func.coalesce(
                models.RelayExitLink.last_handshake_at,
                # epoch для NULL — на 1970-01-01 (=максимальная stale)
                # чтобы они отсортировались первыми.
                sa_func.cast("1970-01-01", models.RelayExitLink.last_handshake_at.type),
            ).asc(),
            models.RelayExitLink.id.asc(),
        )
        .limit(max_per_tick * 4)  # с запасом — дебаунс может выкинуть часть
        .all()
    )

    enqueued: list[dict] = []
    skipped_debounced: list[int] = []
    skipped_disabled: list[int] = []
    auto_check_types = ["peer_on_jump", "handshake_age", "ping_endpoint"]

    for link in candidates:
        if len(enqueued) >= max_per_tick:
            break
        # Debounce: пропускаем, если symptom_detected уже логнут
        # за последние debounce_min минут.
        recent = (
            session.query(models.AuditLog)
            .filter(models.AuditLog.target_type == "relay_exit_link")
            .filter(models.AuditLog.target_id == link.id)
            .filter(models.AuditLog.action == "symptom_detected")
            .filter(models.AuditLog.created_at >= debounce_cutoff)
            .first()
        )
        if recent:
            skipped_debounced.append(link.id)
            continue

        last_hs = link.last_handshake_at
        hs_age_min = (
            int((now - last_hs).total_seconds() // 60) if last_hs else None
        )
        symptom = "no_handshake" if last_hs is None else "stale_handshake"

        orchestrator = ProvisioningOrchestrator(session)
        task = orchestrator.create_task(
            "relay_tunnel",
            link.relay_node_id,
            "diagnose",
            {
                "link_id": link.id,
                "exit_id": link.exit_id,
                "check_types": auto_check_types,
                "auto_triggered": True,
            },
        )
        session.add(
            models.AuditLog(
                actor="auto-diagnose",
                actor_type=models.AuditActor.system,
                action="symptom_detected",
                target_type="relay_exit_link",
                target_id=link.id,
                extra={
                    "symptom": symptom,
                    "last_hs_age_min": hs_age_min,
                    "link_id": link.id,
                    "relay_node_id": link.relay_node_id,
                    "exit_id": link.exit_id,
                    "action_taken": f"enqueued_task:{task.id}",
                    "check_types": auto_check_types,
                    "stale_threshold_min": stale_hs_min,
                },
            )
        )
        session.commit()
        # run_task_async читает task.id из DB — коммит перед enqueue обязателен.
        orchestrator.run_task_async(task)
        enqueued.append({
            "link_id": link.id,
            "task_id": task.id,
            "symptom": symptom,
            "hs_age_min": hs_age_min,
        })
        logger.info(
            "auto_diagnose: link=%s relay=%s exit=%s symptom=%s task=%s",
            link.id, link.relay_node_id, link.exit_id, symptom, task.id,
        )

    return {
        "enabled": True,
        "candidates": len(candidates),
        "enqueued": enqueued,
        "skipped_debounced": skipped_debounced,
        "skipped_disabled": skipped_disabled,
        "max_per_tick": max_per_tick,
    }


def _auto_diagnose_unreachable_nodes(
    session, failed_relay_names: list[str]
) -> dict:
    """Auto-trigger node-level diagnose для нод, на которые SSH не дошёл.

    Сценарий: relay_link_health tick попытался достучаться до relay по
    SSH (paramiko в collect_all_relay_links), упал — записал имя в
    `failed_relay_names`. Это node-level симптом: «нода не отвечает по
    SSH» — link-level smart-diagnose не поможет (ему тоже нужен SSH).

    Что делаем: per-failed-node enqueue ProvisioningTask с target_type=
    'node', action='diagnose' (это уже work'ает — `playbooks/diagnose_
    node.yml` гоняет `check_node_health` через ansible, который сам
    retry'ит SSH с другими таймаутами + соберёт listening sockets и
    systemd state если хоть на чуть дотянется). + audit_log symptom_
    detected с target_type='vpn_node'.

    Mute: skip nodes где VPNNode.auto_diagnose_disabled_at IS NOT NULL.
    Debounce: 30 мин per-node через audit_log lookup — иначе при stale
    SSH-проблеме каждые 5 мин будем спамить tasks.

    Env vars:
      AUTO_DIAGNOSE_NODE_ENABLED         default true
      AUTO_DIAGNOSE_NODE_DEBOUNCE_MIN    default 30
      AUTO_DIAGNOSE_NODE_MAX_PER_TICK    default 2
    """
    if not failed_relay_names:
        return {"enabled": True, "enqueued": [], "candidates": 0}
    if os.getenv("AUTO_DIAGNOSE_NODE_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return {"enabled": False, "enqueued": []}

    from datetime import timedelta

    from . import models
    from .services.provisioning import ProvisioningOrchestrator
    from .time_utils import utcnow

    debounce_min = int(os.getenv("AUTO_DIAGNOSE_NODE_DEBOUNCE_MIN", "30"))
    max_per_tick = int(os.getenv("AUTO_DIAGNOSE_NODE_MAX_PER_TICK", "2"))

    now = utcnow()
    debounce_cutoff = now - timedelta(minutes=debounce_min)

    # Resolve names → VPNNode rows. Filter muted сразу — saves audit query.
    disabled_col = getattr(models.VPNNode, "auto_diagnose_disabled_at", None)
    nodes_q = (
        session.query(models.VPNNode)
        .filter(models.VPNNode.name.in_(failed_relay_names))
    )
    if disabled_col is not None:
        nodes_q = nodes_q.filter(disabled_col.is_(None))
    nodes = nodes_q.all()

    enqueued: list[dict] = []
    skipped_debounced: list[int] = []

    for node in nodes:
        if len(enqueued) >= max_per_tick:
            break
        # Per-node debounce. Action имя другое чем у link-level
        # ('node_unreachable_detected'), чтобы две дебаунс-зоны не
        # пересекались — link-level и node-level могут срабатывать
        # независимо для одной и той же ноды.
        recent = (
            session.query(models.AuditLog)
            .filter(models.AuditLog.target_type == "vpn_node")
            .filter(models.AuditLog.target_id == node.id)
            .filter(models.AuditLog.action == "node_unreachable_detected")
            .filter(models.AuditLog.created_at >= debounce_cutoff)
            .first()
        )
        if recent:
            skipped_debounced.append(node.id)
            continue

        orchestrator = ProvisioningOrchestrator(session)
        task = orchestrator.create_task(
            "node",
            node.id,
            "diagnose",
            {"auto_triggered": True, "symptom": "node_unreachable"},
        )
        session.add(
            models.AuditLog(
                actor="auto-diagnose",
                actor_type=models.AuditActor.system,
                action="node_unreachable_detected",
                target_type="vpn_node",
                target_id=node.id,
                extra={
                    "symptom": "node_unreachable",
                    "node_name": node.name,
                    "action_taken": f"enqueued_task:{task.id}",
                    "trigger": "relay_link_health_ssh_fail",
                },
            )
        )
        session.commit()
        orchestrator.run_task_async(task, node=node)
        enqueued.append({
            "node_id": node.id,
            "node_name": node.name,
            "task_id": task.id,
        })
        logger.info(
            "auto_diagnose_node: node=%s (%s) unreachable → diagnose task=%s",
            node.id, node.name, task.id,
        )

    return {
        "enabled": True,
        "candidates": len(nodes),
        "enqueued": enqueued,
        "skipped_debounced": skipped_debounced,
        "max_per_tick": max_per_tick,
    }


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

    # Reschedule в начале — см. run_pending_rescue_tick. Аналогично
    # traffic-stats, SSH ходит по всем relay нодам и периодически
    # зависает, поэтому reschedule ДО работы обязателен.
    interval = int(os.getenv("RELAY_LINK_HEALTH_INTERVAL", "300"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_relay_link_health_tick",
                interval,
                tick_id="tick-relay-link-health",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("relay_link_health: failed to re-enqueue tick (at start)")

    summary: dict = {}
    session = SessionLocal()
    try:
        summary = relay_link_health.collect_all_relay_links(session)

        # Smart-trigger автодиагностики для stale-handshake линков. Тик уже
        # обновил last_handshake_at/last_observed_at — на их основе мы
        # выявляем links где WG явно жив со стороны мониторинга (SSH
        # прошёл = last_observed_at свежий), но handshake'и не текут.
        # Это симптом «attach прошёл, но trafficу не идёт» — exactly
        # тот сценарий, что юзер ловил руками на ru-adminvps-01.
        try:
            auto_summary = _auto_diagnose_stale_links(session)
            if auto_summary.get("enqueued"):
                summary["auto_diagnose"] = auto_summary
        except Exception:  # noqa: BLE001
            logger.exception("relay_link_health: auto_diagnose failed")

        # Node/exit down-detection + the admin push moved to
        # ``run_node_reachability_tick``: it probes ALL active nodes + exits
        # (not just relays with exit-links) and emits a SPEAKING diagnosis
        # push with ack/mute/follow inline buttons instead of the old static
        # "Проверь /admin/nodes" alert, with the once-per-incident anti-spam
        # gate in ``diagnostics_state``. The relay tick now only polls WG
        # handshakes and auto-diagnoses stale LINKS (a signal reachability
        # can't see — a relay can be ssh-up while its tunnel to an exit is
        # dead). ``failed_relay_names`` stays in ``summary`` for telemetry.
    except Exception:  # noqa: BLE001
        logger.exception("relay_link_health: tick failed")
        if session.is_active:
            session.rollback()
    finally:
        session.close()

    return summary


def run_node_reachability_tick() -> dict:
    """Controller→host reachability for ALL active VPN nodes + WG exits.

    The single owner of node/exit down-detection (the old relay tick only
    saw relay nodes with exit-links). Per target runs a staged local probe
    (ping → tcp:ssh → ssh-pong); on DOWN it consults
    ``diagnostics_state.should_diagnose`` (the once-per-incident anti-spam
    gate), and when that fires: opens the incident, sends a SPEAKING admin
    push built from the probe checks (unless alerts muted), and enqueues the
    full on-host diagnose task for the detailed /tasks checklist. On RECOVERY
    it closes the incident so the next outage is fresh.

    Self-reschedules every NODE_REACHABILITY_INTERVAL sec (default 300).
    Disabled at 0. NODE_REACHABILITY_MAX_DIAGNOSE_PER_TICK (default 4) caps
    how many full diagnoses we kick off per tick so a multi-node outage
    can't dogpile ansible.
    """
    from .db import SessionLocal
    from .queue import schedule_tick
    from . import models
    from .services import diagnostics, diagnostics_state
    from .services.admin_notify import notify_node_diagnosis
    from .services.provisioning import ProvisioningOrchestrator
    from .time_utils import utcnow

    # Reschedule first (SSH can hang) — same pattern as the other ticks.
    interval = int(os.getenv("NODE_REACHABILITY_INTERVAL", "300"))
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_node_reachability_tick",
                interval,
                tick_id="tick-node-reachability",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("node_reachability: failed to re-enqueue tick (at start)")

    if os.getenv("NODE_REACHABILITY_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return {"enabled": False}

    max_diag = int(os.getenv("NODE_REACHABILITY_MAX_DIAGNOSE_PER_TICK", "4"))
    summary: dict = {
        "enabled": True,
        "checked": 0,
        "down": [],
        "recovered": [],
        "diagnosed": [],
        "pushed": [],
    }
    session = SessionLocal()
    try:
        targets: list[tuple[str, object]] = []
        for n in (
            session.query(models.VPNNode)
            .filter(models.VPNNode.is_active.is_(True))
            .all()
        ):
            targets.append(("node", n))
        for e in (
            session.query(models.WGExitNode)
            .filter(models.WGExitNode.is_active.is_(True))
            .all()
        ):
            targets.append(("exit", e))

        import time as _time

        # Per-tick wall-clock budget. Serial ping/ssh per DOWN target costs
        # seconds (each blackholed probe waits its timeout) and the RQ job
        # timeout is tick-node-reachability=240s. Stop probing once we near
        # it — the tick already self-rescheduled at the top, so the unprobed
        # tail is picked up next cycle instead of the whole tick getting
        # killed mid-loop by the RQ kill-horse (which would also rotate which
        # targets get starved). Default 200s leaves headroom under 240s.
        budget_s = int(os.getenv("NODE_REACHABILITY_BUDGET_SEC", "200"))
        started = _time.monotonic()
        diagnosed = 0
        for kind, target in targets:
            if _time.monotonic() - started > budget_s:
                summary["budget_exceeded_after"] = summary["checked"]
                logger.warning(
                    "node_reachability: wall-clock budget %ss hit after %s targets — "
                    "deferring rest to next tick",
                    budget_s, summary["checked"],
                )
                break
            host = getattr(target, "host", None)
            if not host:
                continue
            summary["checked"] += 1
            try:
                probe = diagnostics.run_local_path_probe(
                    host,
                    ssh_port=getattr(target, "ssh_port", 22) or 22,
                    # No traceroute in the sweep — it adds ~29s per DOWN
                    # target. The detailed on-host diagnose task enqueued
                    # below runs the full probe WITH traceroute for /tasks.
                    traceroute_on_fail=False,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "node_reachability: probe crashed for %s:%s", kind, target.id
                )
                continue
            now = utcnow()
            ref = f"{kind}:{target.id}"

            if probe.ssh_ok:
                target.last_probe_at = now
                target.last_probe_status = "ok"
                if diagnostics_state.close_incident(target):
                    summary["recovered"].append(ref)
                session.commit()
                continue

            # ── DOWN ────────────────────────────────────────────────────
            target.last_probe_at = now
            target.last_probe_status = "unreachable"
            summary["down"].append(ref)

            do_diag, reason = diagnostics_state.should_diagnose(target, now)
            if not do_diag or diagnosed >= max_diag:
                # Either gated (disabled / acked / once-done / backoff) or we
                # already kicked off enough diagnoses this tick.
                session.commit()
                continue

            diagnosed += 1
            diagnostics_state.mark_diagnosed(target, now)
            summary["diagnosed"].append(ref)

            # Speaking push from the probe checks (immediate), unless muted.
            if not diagnostics_state.is_alerts_muted(target, now):
                try:
                    notify_node_diagnosis(
                        session, target_kind=kind, target=target,
                        checks=probe.checks, autocommit=False,
                    )
                    summary["pushed"].append(ref)
                except Exception:  # noqa: BLE001
                    logger.exception("node_reachability: push failed for %s", ref)

            # Enqueue the full on-host diagnose for the detailed /tasks
            # checklist (should_diagnose already cleared the hard toggle).
            try:
                orchestrator = ProvisioningOrchestrator(session)
                task = orchestrator.create_task(
                    "node" if kind == "node" else "exit",
                    target.id,
                    "diagnose",
                    {"auto_triggered": True, "symptom": "unreachable"},
                )
                session.commit()
                orchestrator.run_task_async(
                    task, node=target if kind == "node" else None
                )
            except Exception:  # noqa: BLE001
                logger.exception("node_reachability: diagnose enqueue failed for %s", ref)
                if session.is_active:
                    session.rollback()
    except Exception:  # noqa: BLE001
        logger.exception("node_reachability: tick failed")
        if session.is_active:
            session.rollback()
    finally:
        session.close()

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

    import random as _random

    interval = int(os.getenv("USER_HEALTH_PING_INTERVAL", "1800"))
    batch = int(os.getenv("USER_HEALTH_PING_BATCH", "50"))
    # Базовая дебаунс-дельта = минимум между ping'ами. Jitter (random
    # forward-offset) добавляется к `health_ping_last_at` при записи,
    # чтобы фактический интервал растянулся в [base, base+jitter] на
    # юзера. Дефолты: 168ч (7 дн.) base + 168ч (7 дн.) jitter → ping
    # каждые 7-14 дней случайно. Раньше было 24ч fixed — юзеры жали
    # «не работает» в игнор из-за фоновой усталости, и реальные
    # жалобы тонули в шуме.
    debounce_hours = int(os.getenv("USER_HEALTH_PING_DEBOUNCE_HOURS", "168"))
    jitter_hours = int(os.getenv("USER_HEALTH_PING_DEBOUNCE_JITTER_HOURS", "168"))

    # Reschedule в начале — см. run_pending_rescue_tick. Важно: ставим
    # reschedule ДО early-return по MSK-окну, иначе вне окна тик умрёт.
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_user_health_ping_tick",
                interval,
                tick_id="tick-health-ping",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("user_health_ping: failed to re-enqueue tick (at start)")

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
            # Future-time stretch: записываем не `now`, а `now + random(0, jitter)`.
            # Поле `health_ping_last_at` дальше сравнивается только с
            # `now - debounce_hours` в фильтре отбора, поэтому смещение
            # вперёд эквивалентно отсрочке «следующего eligible» на ту же
            # величину. Range: [debounce_hours, debounce_hours + jitter_hours].
            jitter_offset = timedelta(
                hours=_random.uniform(0.0, max(0, jitter_hours))
            ) if jitter_hours > 0 else timedelta(0)
            user.health_ping_last_at = now + jitter_offset
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

    return summary


def run_broadcast_dispatch_tick() -> dict:
    """Admin broadcast dispatcher — батч-рассылка юзерам из Broadcast-очереди.

    Раз в BROADCAST_DISPATCH_INTERVAL (default 10s) проверяет, есть ли
    broadcast в `queued/sending`. Берёт один (старый сначала) и режет
    юзеров по target_filter'у батчем BROADCAST_BATCH_SIZE (default 50).
    На каждого пишет AuditLog(admin_broadcast), который поднимет
    bot-поллер и доставит через `send_message`.

    Почему один broadcast за тик, а не все сразу
    ---------------------------------------------
    Тик запускается каждые 10s. Если броадкастов в очереди два, второй
    подождёт 10s — это OK, bot всё равно через rate-limit не пропустит
    два залпа одновременно (0.05s sleep между send_message для
    admin_broadcast).

    Курсор: `last_user_id_cursor` монотонно растёт. Исчерпали batch
    (len < BROADCAST_BATCH_SIZE) → `status=completed`, `completed_at=now`.
    Отменённый broadcast (`status=cancelled`) просто пропускается — ни
    один ещё-не-отправленный батч ему не уйдёт.

    Self-reschedule в начале тела (см. run_pending_rescue_tick).
    """
    from . import models
    from .api.broadcasts import resolve_target_query, TargetFilter
    from .db import SessionLocal
    from .queue import schedule_tick
    from .time_utils import utcnow

    interval = int(os.getenv("BROADCAST_DISPATCH_INTERVAL", "10"))
    batch_size = int(os.getenv("BROADCAST_BATCH_SIZE", "50"))

    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_broadcast_dispatch_tick",
                interval,
                tick_id="tick-broadcast-dispatch",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("broadcast_dispatch: failed to re-enqueue tick")

    summary: dict = {
        "processed": 0,
        "sent": 0,
        "completed": 0,
    }
    session = SessionLocal()
    try:
        bc = (
            session.query(models.Broadcast)
            .filter(
                models.Broadcast.status.in_(
                    [
                        models.BroadcastStatus.queued,
                        models.BroadcastStatus.sending,
                    ]
                )
            )
            .order_by(models.Broadcast.id.asc())
            .first()
        )
        if bc is None:
            return summary

        now = utcnow()
        if bc.status == models.BroadcastStatus.queued:
            bc.status = models.BroadcastStatus.sending
            bc.started_at = now

        try:
            tf = TargetFilter(**bc.target_filter)
        except Exception:  # noqa: BLE001
            # Шейп в БД битый — помечаем failed, больше не трогаем.
            logger.exception(
                "broadcast_dispatch: bad target_filter on broadcast %s: %r",
                bc.id, bc.target_filter,
            )
            bc.status = models.BroadcastStatus.failed
            bc.completed_at = now
            session.commit()
            return summary

        q = resolve_target_query(session, tf)
        users = (
            q.filter(models.User.id > bc.last_user_id_cursor)
            .order_by(models.User.id.asc())
            .limit(batch_size)
            .all()
        )

        if not users:
            # Исчерпали фильтр — финализируем.
            bc.status = models.BroadcastStatus.completed
            bc.completed_at = now
            session.commit()
            summary["completed"] = 1
            summary["processed"] = 1
            return summary

        for u in users:
            session.add(
                models.AuditLog(
                    actor="broadcast_dispatch",
                    actor_type=models.AuditActor.system,
                    action="admin_broadcast",
                    target_type="broadcast",
                    target_id=bc.id,
                    extra={
                        "telegram_id": str(u.telegram_id),
                        "text": bc.text,
                        "broadcast_id": bc.id,
                    },
                )
            )
            bc.sent_count += 1
            bc.last_user_id_cursor = max(bc.last_user_id_cursor, u.id)

        summary["sent"] = len(users)
        summary["processed"] = 1
        session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("broadcast_dispatch: tick failed")
        if session.is_active:
            session.rollback()
    finally:
        session.close()

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

            # Admin push-алерт на DLQ-событие. Дедуп по task_id —
            # разные таски пушатся независимо, повтор того же task_id
            # за ADMIN_ALERT_DLQ_WINDOW_SEC (5 мин по умолчанию) —
            # подавляется. Окно короче общего ADMIN_ALERT_DEDUP_WINDOW_SEC,
            # потому что повтор DLQ по тому же task_id — патологический
            # случай, но хоть один такой за 5 минут интересен ровно
            # один раз.
            try:
                from .services.admin_notify import notify_admins

                exc_name = exc_type.__name__ if exc_type else "?"
                err_preview = str(exc_value)[:300]
                dlq_text = (
                    f"❌ Провижининг упал: task={task_id} "
                    f"job={job.id}\n"
                    f"{exc_name}: {err_preview}"
                )
                notify_admins(
                    session,
                    kind="infra_dlq",
                    text=dlq_text,
                    dedup_key={"task_id": task_id},
                    extra={
                        "job_id": job.id,
                        "exc_type": exc_name,
                    },
                    window_sec=int(
                        os.getenv("ADMIN_ALERT_DLQ_WINDOW_SEC", "300")
                    ),
                    autocommit=True,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "notify_admins не отработал для DLQ task %s", task_id
                )
        finally:
            session.close()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write DLQ audit log for task %s", task_id)

    return True  # let RQ continue its normal failure flow


def main() -> None:
    from .logging_config import configure_logging
    # ``schedule_tick`` тут критически важный импорт: все 8 bootstrap'ов
    # ниже обёрнуты в ``try/except Exception`` — без явного импорта
    # ``schedule_tick`` в scope main() NameError молча глотался, тики
    # НИКОГДА не становились в scheduled при старте воркера, и
    # периодический автохелз работал ТОЛЬКО после ручного
    # force-refresh через UI (который стартует цепочку self-reschedule
    # из тела самого tick'а, где импорт локальный). После рестарта
    # воркера цепочка рвалась — админ видел "последний SSH 600 мин"
    # и должен был кликать кнопку заново.
    from .queue import schedule_tick

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

    # WORKER_ROLE разделяет один и тот же образ на два docker-сервиса:
    # * "ticks" — слушает только тиковую очередь, включает with_scheduler,
    #   бутстрапит все периодики. Лёгкие задачи (секунды), не должен
    #   забиваться ansible-run'ами.
    # * "provisioning" — слушает только provisioning-очередь, без
    #   scheduler'а, без bootstrap'а. Долгие ansible-run'ы живут тут.
    # * unset (legacy/single-container) — слушает обе очереди,
    #   bootstrap + scheduler. Старая конфигурация.
    role = os.getenv("WORKER_ROLE", "").strip().lower()
    provisioning_queue_name = os.getenv("RQ_QUEUE", "vpn-provisioning")
    ticks_queue_name = os.getenv("RQ_TICKS_QUEUE", "vpn-ticks")

    connection = Redis.from_url(url)
    connection.ping()

    try:
        from .migrations import run_migrations
        run_migrations()
    except Exception:  # noqa: BLE001
        logger.exception("Worker failed to run migrations at startup")

    provisioning_queue = Queue(provisioning_queue_name, connection=connection)
    ticks_queue = Queue(ticks_queue_name, connection=connection)

    if role == "ticks":
        queues_to_listen = [ticks_queue]
        do_bootstrap = True
        with_scheduler = True
    elif role == "provisioning":
        queues_to_listen = [provisioning_queue]
        do_bootstrap = False
        with_scheduler = False
    else:
        # Legacy: один воркер на всё.
        queues_to_listen = [provisioning_queue, ticks_queue]
        do_bootstrap = True
        with_scheduler = True

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
    if do_bootstrap and pending_rescue_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_pending_rescue_tick",
                min(pending_rescue_interval, 30),
                tick_id="tick-pending-rescue",
                replace=True,
            )
            logger.info(
                "Pending-rescue tick bootstrapped: first run in 30s "
                "(interval=%ss)", pending_rescue_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule pending-rescue tick")

    # Schedule autoscale tick
    autoscale_interval = int(os.getenv("AUTOSCALE_INTERVAL", "0"))
    if do_bootstrap and autoscale_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_autoscale_tick",
                autoscale_interval,
                tick_id="tick-autoscale",
                replace=True,
            )
            logger.info("Autoscale bootstrapped: first tick in %ss", autoscale_interval)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule autoscale tick")

    # Schedule renewal check (default: every hour)
    renewal_interval = int(os.getenv("RENEWAL_CHECK_INTERVAL", "3600"))
    if do_bootstrap and renewal_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_renewal_check",
                min(renewal_interval, 60),
                tick_id="tick-renewal",
                replace=True,
            )
            logger.info("Renewal check bootstrapped: first run in 60s (interval=%ss)", renewal_interval)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule renewal check")

    # Schedule warm-pool check (default: every 2 min). Stage 2.5 of the
    # WebApp roadmap — keeps each active node's pool topped up so user
    # purchases hit a warm bundle instead of paying the Ansible cost.
    warm_interval = int(os.getenv("WARM_POOL_CHECK_INTERVAL", "120"))
    warm_enabled = os.getenv("WARM_POOL_ENABLED", "1").lower() not in {"0", "false", "no"}
    if do_bootstrap and warm_interval > 0 and warm_enabled:
        try:
            schedule_tick(
                "app.worker.run_warm_pool_check",
                min(warm_interval, 30),
                tick_id="tick-warm-pool",
                replace=True,
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
    if do_bootstrap and balance_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_balance_charge_tick",
                min(balance_interval, 60),
                tick_id="tick-balance-charge",
                replace=True,
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
    if do_bootstrap and traffic_stats_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_traffic_stats_tick",
                min(traffic_stats_interval, 60),
                tick_id="tick-traffic-stats",
                replace=True,
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
    if do_bootstrap and relay_link_health_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_relay_link_health_tick",
                min(relay_link_health_interval, 60),
                tick_id="tick-relay-link-health",
                replace=True,
            )
            logger.info(
                "Relay-link health tick bootstrapped: first run in 60s (interval=%ss)",
                relay_link_health_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule relay-link health tick")

    # Diagnostics overhaul — controller→host reachability probe across ALL
    # active VPN nodes + WG exits (ping/tcp/ssh). Owns node/exit down-
    # detection, the once-per-incident anti-spam gate, the speaking admin
    # push and the on-host diagnose enqueue. Interval
    # NODE_REACHABILITY_INTERVAL (default 300s), disabled at 0.
    node_reach_interval = int(os.getenv("NODE_REACHABILITY_INTERVAL", "300"))
    if do_bootstrap and node_reach_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_node_reachability_tick",
                min(node_reach_interval, 60),
                tick_id="tick-node-reachability",
                replace=True,
            )
            logger.info(
                "Node-reachability tick bootstrapped: first run in 60s (interval=%ss)",
                node_reach_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule node-reachability tick")

    # Phase C — bot health-ping with consent. Queues a friendly
    # "помогите нам улучшить сервис" prompt to active users at most
    # once per USER_HEALTH_PING_DEBOUNCE_HOURS, capped at
    # USER_HEALTH_PING_BATCH per tick. Disabled when set to 0.
    health_ping_interval = int(os.getenv("USER_HEALTH_PING_INTERVAL", "1800"))
    if do_bootstrap and health_ping_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_user_health_ping_tick",
                min(health_ping_interval, 60),
                tick_id="tick-health-ping",
                replace=True,
            )
            logger.info(
                "User health-ping tick bootstrapped: first run in 60s (interval=%ss)",
                health_ping_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule user health-ping tick")

    # Admin broadcast dispatcher — тянет Broadcast-рассылки батчами из
    # таблицы broadcasts и пишет AuditLog(admin_broadcast) по одной
    # строке на юзера. Интервал BROADCAST_DISPATCH_INTERVAL (default 10s),
    # disabled при 0.
    broadcast_interval = int(os.getenv("BROADCAST_DISPATCH_INTERVAL", "10"))
    if do_bootstrap and broadcast_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_broadcast_dispatch_tick",
                min(broadcast_interval, 30),
                tick_id="tick-broadcast-dispatch",
                replace=True,
            )
            logger.info(
                "Broadcast dispatch tick bootstrapped: first run in %ss (interval=%ss)",
                min(broadcast_interval, 30),
                broadcast_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule broadcast dispatch tick")

    worker = Worker(
        queues_to_listen,
        connection=connection,
        exception_handlers=[dlq_exception_handler],
    )
    logger.info(
        "Starting RQ worker role=%s queues=%s scheduler=%s",
        role or "legacy-all",
        [q.name for q in queues_to_listen],
        with_scheduler,
    )
    worker.work(with_scheduler=with_scheduler)


if __name__ == "__main__":
    main()
