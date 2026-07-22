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


def _env_int(name: str, default: int) -> int:
    """``int(os.getenv(name))`` с защитой от мусорного значения.

    Голый ``int(os.getenv(...))`` на невалидной переменной кидает ValueError.
    На module-level это роняет импорт ``app.worker`` — а его импортит КАЖДАЯ
    RQ-джоба и ``main()``, поэтому одна опечатка в .env (``FOO=24h``) кладёт
    весь фоновый контур в crash-loop. В теле тика ValueError до self-reschedule
    убивает цепочку периодики молча. Здесь мусор/пустая строка → warning +
    дефолт, чтобы контур пережил кривую переменную.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        logger.warning(
            "env %s=%r не парсится как int — использую дефолт %s",
            name, raw, default,
        )
        return default


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
RENEWAL_GRACE_HOURS = _env_int("RENEWAL_GRACE_HOURS", 24)

# Верхняя граница числа подписок, обрабатываемых за один тик продлений в
# каждом окне. Без лимита выборки .all() тянут всё окно, а per-sub цикл делает
# по несколько запросов на подписку — при тысячах истекающих тик упирается в
# job_timeout и хвост окна не обрабатывается. Обходим окна в порядке
# expires_at ASC (самые срочные первыми), остаток донесётся следующим тиком.
RENEWAL_WINDOW_LIMIT = _env_int("RENEWAL_WINDOW_LIMIT", 2000)


PENDING_RESCUE = Counter(
    "vpn_provisioning_pending_rescue_total",
    "Pending provisioning tasks re-enqueued by the self-heal tick",
)

# Reconciler watchdog gauges — set each reconcile tick (no-op while
# RECONCILER_ENABLED is off). A wedged scheduler stops updating these, so an
# external staleness alert on the metric catches the "stuck tick strands a
# fresh node" failure that no in-tick check can see.
RECONCILE_PENDING_NODES = Gauge(
    "vpn_reconcile_pending_nodes",
    "Nodes with desired_generation > reconciled_generation awaiting reconcile",
)
RECONCILE_OLDEST_OVERDUE = Gauge(
    "vpn_reconcile_oldest_overdue_seconds",
    "Age (s) of the oldest overdue (due_at<=now) pending-reconcile node",
)

# Reachability-тик: сколько целей (nodes+exits) не пробовано дольше
# NODE_REACHABILITY_STALE_MIN минут. >0 на стабильном флоте — сигнал, что
# wall-clock бюджета тика не хватает на всех (голодание хвоста).
REACHABILITY_STALE_TARGETS = Gauge(
    "vpn_reachability_stale_targets",
    "Reachability targets not probed for longer than the staleness window",
)

# Cloud billing gauges — set each cloud-billing tick.
PROVIDER_BALANCE = Gauge(
    "vpn_cloud_provider_balance",
    "Account balance at a cloud provider (units per provider)",
    ["provider"],
)
FLEET_MONTHLY_COST = Gauge(
    "vpn_fleet_monthly_cost",
    "Sum of monthly_cost over active VPN nodes",
)


def run_cloud_billing_tick() -> dict:
    """Periodic billing guard for cloud-provisioned fleet:
    * per active CloudProvider — pull balance → gauge + low-balance admin alert;
    * export fleet monthly-cost gauge.

    Авто-продление само живёт на стороне провайдера (4vps autoprolong,
    включается при spawn) — этот тик СТРАЖ, чтобы баланс не иссяк молча и ноды
    не удалились в конце периода. Self-reschedules. No-op без cloud-провайдеров.
    """
    from sqlalchemy import func as sa_func

    from . import models
    from .db import SessionLocal
    from .queue import schedule_tick
    from .services.admin_notify import notify_admins
    from .services.cloud.base import DriverError, get_driver

    interval = _env_int("CLOUD_BILLING_INTERVAL", 3600)
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_cloud_billing_tick",
                interval,
                tick_id="tick-cloud-billing",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("cloud-billing: failed to re-enqueue tick")

    threshold = float(os.getenv("CLOUD_BALANCE_ALERT_THRESHOLD", "0"))
    session = SessionLocal()
    checked = 0
    alerts = 0
    try:
        providers = (
            session.query(models.CloudProvider)
            .filter(models.CloudProvider.is_active.is_(True))
            .all()
        )
        for p in providers:
            try:
                driver = get_driver(p)
            except DriverError:
                continue
            if not hasattr(driver, "get_balance"):
                continue
            try:
                bal = driver.get_balance()
            except DriverError as exc:
                logger.warning(
                    "cloud-billing: balance fetch failed for %s: %s", p.name, exc
                )
                continue
            checked += 1
            if bal is None:
                continue
            PROVIDER_BALANCE.labels(provider=p.name).set(bal)
            if threshold > 0 and bal < threshold:
                alerts += 1
                notify_admins(
                    session,
                    kind="cloud_balance_low",
                    text=(
                        f"⚠️ Низкий баланс у облачного провайдера {p.name}: "
                        f"{bal}. Ноды могут не продлиться (autoprolong) — "
                        f"пополни баланс."
                    ),
                    dedup_key={"provider_id": p.id},
                    autocommit=True,
                )
        total = (
            session.query(
                sa_func.coalesce(sa_func.sum(models.VPNNode.monthly_cost), 0)
            )
            .filter(models.VPNNode.is_active.is_(True))
            .scalar()
        ) or 0
        FLEET_MONTHLY_COST.set(float(total))
        return {
            "providers_checked": checked,
            "alerts": alerts,
            "fleet_monthly_cost": float(total),
        }
    finally:
        session.close()


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
    interval = _env_int("PENDING_RESCUE_INTERVAL", 60)
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

    age = _env_int("PENDING_RESCUE_AGE", 60)
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


def run_lava_reconcile_tick() -> dict:
    """Webhook-independent reconcile для карточных платежей lava.top.

    Доставка вебхуков lava — best-effort (до 20 ретраев по докам; в проде
    наблюдалось, что POST не приходит вовсе — счёт остаётся pending, деньги
    у клиента списаны). Этот тик раз в ``LAVA_TOP_RECONCILE_INTERVAL`` секунд
    опрашивает ``GET /api/v2/invoices`` и зачисляет любой pending-счёт, чья
    продажа у lava уже COMPLETED (матч по ``clientUtm.utm_content`` = наш
    invoice_id). Идемпотентно: уже-paid счета пропускаются, а если вебхук
    всё-таки долетит — ``_mark_invoice_paid_core`` дедупит по
    ``reference=invoice:{id}``. No-op пока lava_top не сконфигурирован.
    """
    from .db import SessionLocal
    from . import models
    from .queue import schedule_tick
    from .services.payments import ProviderError, get_provider
    from .api.invoices import _mark_invoice_paid_core

    # Перепланируем ДО работы (как остальные тики), чтобы падение тела не
    # оборвало периодику.
    interval = _env_int("LAVA_TOP_RECONCILE_INTERVAL", 60)
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_lava_reconcile_tick",
                interval,
                tick_id="tick-lava-reconcile",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("lava_reconcile: failed to re-enqueue tick (at start)")

    if not os.getenv("LAVA_TOP_API_KEY"):
        return {"skipped": "not_configured"}
    try:
        provider = get_provider("lava_top")
    except ProviderError:
        return {"skipped": "not_configured"}

    try:
        sales = provider.list_recent_invoices()
    except ProviderError as exc:
        logger.warning("lava_reconcile: list invoices failed: %s", exc)
        return {"error": "list_failed"}

    checked = 0
    credited = 0
    session = SessionLocal()
    try:
        for sale in sales:
            if not sale.get("completed") or not sale.get("invoice_id"):
                continue
            inv_id = sale["invoice_id"]
            invoice = session.get(models.Invoice, inv_id)
            if invoice is None or invoice.status != models.InvoiceStatus.pending:
                continue
            checked += 1
            # Сверка суммы (RUB↔RUB, как в webhook): продажа lava должна
            # покрывать сумму счёта — иначе не зачисляем (частичная оплата).
            sale_amount = sale.get("amount")
            if sale_amount is not None and float(sale_amount) + 0.01 < float(invoice.amount):
                logger.warning(
                    "lava_reconcile: invoice %s underpaid (lava=%s, invoice=%s) — skip",
                    inv_id, sale_amount, invoice.amount,
                )
                continue
            # Помечаем ту же pending Payment-строку, что создал webapp_topup
            # (provider=lava_top), чтобы её external_id/статус сошлись.
            pending_payment = (
                session.query(models.Payment)
                .filter(
                    models.Payment.invoice_id == inv_id,
                    models.Payment.provider == "lava_top",
                    models.Payment.status == models.PaymentStatus.pending,
                )
                .order_by(models.Payment.id.desc())
                .first()
            )
            payment_id = pending_payment.id if pending_payment else None
            try:
                _mark_invoice_paid_core(
                    session,
                    inv_id,
                    actor="lava_top:reconcile",
                    actor_type=models.AuditActor.system,
                    payment_id=payment_id,
                )
                credited += 1
                logger.warning(
                    "lava_reconcile: credited invoice %s from lava sale %s (webhook missed)",
                    inv_id, sale.get("contract_id"),
                )
            except Exception:  # noqa: BLE001
                session.rollback()
                logger.exception("lava_reconcile: failed to credit invoice %s", inv_id)
    finally:
        session.close()

    return {"checked": checked, "credited": credited}


def run_operator_report_watch_tick() -> dict:
    """Periodic — resolve operator-routing reports by observed reconnect.

    Phase 1: flips ``OperatorNodeReport`` rows from ``pending`` to
    ``ok``/``inconclusive`` once ``T_RECONNECT`` (default 15 min) has
    elapsed, by checking whether the user reconnected on the target node
    (``NodeTrafficSample``). See services.operator_reports +
    docs/operations/operator_routing_roadmap.md.
    """
    from .db import SessionLocal
    from .queue import schedule_tick
    from .services.operator_reports import resolve_pending_reports

    # Reschedule в начале — см. run_pending_rescue_tick.
    interval = _env_int("OPERATOR_REPORT_WATCH_INTERVAL", 300)
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_operator_report_watch_tick",
                interval,
                tick_id="tick-operator-report-watch",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "operator_report_watch: failed to re-enqueue tick (at start)"
            )

    session = SessionLocal()
    try:
        return resolve_pending_reports(session)
    finally:
        session.close()


def run_reconcile_tick() -> dict:
    """Phase 3 reconcile-тик: сходит ноды по desired-state generations (одним
    coalesced bootstrap'ом на ноду, у которой due наступил и desired >
    reconciled). No-op если RECONCILER_ENABLED выключен (provision идёт по
    Phase-0 immediate-модели). Self-reschedules. См. reconciler_epic.md."""
    from .db import SessionLocal
    from .queue import schedule_tick
    from .services.provisioning import ProvisioningOrchestrator

    interval = _env_int("RECONCILE_INTERVAL", 3)
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_reconcile_tick",
                interval,
                tick_id="tick-reconcile",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("reconcile: failed to re-enqueue tick (at start)")

    session = SessionLocal()
    try:
        result = ProvisioningOrchestrator(session).reconcile_due_nodes()
        # Watchdog-гейджи: пока reconciler включён, reconcile_due_nodes
        # возвращает pending_total/oldest_overdue_s. Если scheduler завис и
        # тик перестал бежать, гейджи протухают → external staleness-alert.
        if "pending_total" in result:
            RECONCILE_PENDING_NODES.set(result["pending_total"])
            RECONCILE_OLDEST_OVERDUE.set(result["oldest_overdue_s"])
        return result
    finally:
        session.close()


def _resolve_mgmt_host() -> str:
    """Pull the mgmt host from the ansible inventory (db_host → mgmt-1).

    Mirrors ``scripts/workers.sh``. ``MGMT_HOST`` env overrides this.
    """
    import yaml

    root = os.getenv("ANSIBLE_ROOT", "/app/infra/ansible")
    path = os.path.join(root, "inventories", "prod", "hosts.yml")
    with open(path) as fh:
        inv = yaml.safe_load(fh)
    return inv["all"]["children"]["db_host"]["hosts"]["mgmt-1"]["ansible_host"]


def run_scale_workers(replicas: int) -> dict:
    """Scale the worker container replicas on the mgmt host via SSH.

    Runs on a worker (the API image carries no ssh/key) — mirrors
    ``scripts/workers.sh``: ssh to the mgmt host, persist
    ``WORKER_REPLICAS`` in ``.env`` (so it survives a plain
    ``docker compose up``), then ``docker compose up -d --scale
    worker=N worker``. The docker command runs ON the host, so even a
    scale-DOWN that kills this very worker mid-run still completes.

    Returns ``{replicas, ok, rc, stdout, stderr}`` (output tails). Raises
    ``ValueError`` on an out-of-range count (the API also validates).
    """
    import subprocess

    n = int(replicas)
    if not (1 <= n <= 20):
        raise ValueError(f"replicas {n} out of range 1..20")

    mgmt_host = os.getenv("MGMT_HOST") or _resolve_mgmt_host()
    mgmt_user = os.getenv("MGMT_USER", "root")
    stack_dir = os.getenv("MGMT_STACK_DIR", "/opt/vpn")
    key = os.getenv("ANSIBLE_PRIVATE_KEY_FILE", "/run/secrets/provisioning_key")

    remote = (
        f"set -e; cd {stack_dir}; "
        f"if grep -q '^WORKER_REPLICAS=' .env 2>/dev/null; then "
        f"sed -i 's/^WORKER_REPLICAS=.*/WORKER_REPLICAS={n}/' .env; "
        f"else echo 'WORKER_REPLICAS={n}' >> .env; fi; "
        f"docker compose up -d --scale worker={n} worker; "
        f"docker compose ps worker --format '{{{{.Name}}}} {{{{.State}}}}'"
    )
    cmd = [
        "ssh",
        "-i", key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=20",
        "-o", "BatchMode=yes",
        f"{mgmt_user}@{mgmt_host}",
        remote,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        logger.error("scale_workers: ssh to %s timed out", mgmt_host)
        return {
            "replicas": n,
            "ok": False,
            "rc": -1,
            "stdout": "",
            "stderr": "ssh timeout (150s)",
        }

    ok = proc.returncode == 0
    if not ok:
        logger.error(
            "scale_workers rc=%s stderr=%s", proc.returncode, proc.stderr[-500:]
        )
    else:
        logger.info("scale_workers: set worker replicas = %s on %s", n, mgmt_host)
    return {
        "replicas": n,
        "ok": ok,
        "rc": proc.returncode,
        "stdout": proc.stdout[-1500:],
        "stderr": proc.stderr[-1500:],
    }


def run_autoscale_tick() -> list[dict]:
    """Periodic job — walk pools and scale up where needed."""
    from dataclasses import asdict

    from .db import SessionLocal
    from .queue import schedule_tick
    from .services.autoscale import evaluate_all_pools

    # Reschedule в начале — см. run_pending_rescue_tick.
    interval = _env_int("AUTOSCALE_INTERVAL", 0)
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
    interval = _env_int("RENEWAL_CHECK_INTERVAL", 3600)
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
                # Битая строка (план удалён): renew_subscription кидал бы
                # RuntimeError на каждом тике. Не экспайрим платящего юзера —
                # алертим админа (с суточным дедупом) и ждём ручного разбора.
                if sub.plan is None:
                    logger.error(
                        "renewal_check: sub=%s auto_renew=ON без плана — "
                        "продление невозможно, нужен ручной разбор", sub.id
                    )
                    try:
                        from .services.admin_notify import notify_admins
                        notify_admins(
                            session,
                            kind="renewal_broken_sub",
                            text=(
                                f"⚠️ Подписка #{sub.id} (auto_renew=ON) без "
                                f"плана — авто-продление невозможно, нужен "
                                f"ручной разбор."
                            ),
                            dedup_key={"subscription_id": sub.id},
                            window_sec=86400,
                            autocommit=True,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "renewal_check: broken-sub alert failed sub=%s",
                            sub.id,
                        )
                    stats["errors"] += 1
                    continue
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
                    stats["errors"] += 1
                    # Транзиентный сбой (обрыв БД, deadlock) — НЕ повод
                    # экспайрить оплаченную подписку: пропускаем, следующий
                    # тик повторит попытку. Экспайр только при явном
                    # «недостаточно средств» (renew_subscription вернул False).
                    continue
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
            .order_by(models.Subscription.expires_at.asc())
            .limit(RENEWAL_WINDOW_LIMIT)
            .all()
        )
        for sub in expiring_soon:
            # Per-item SAVEPOINT-изоляция: битая строка (IntegrityError на
            # flush инвойса, обрыв соединения посреди цикла) откатывается
            # только сама и не рушит ни остальные подписки этого пасса, ни
            # последующие пассы (1-day/manual напоминания). Без этого одна
            # запись глушила напоминания всем.
            try:
                with session.begin_nested():
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
            except Exception:  # noqa: BLE001
                logger.exception(
                    "renewal_check: 3d reminder pass failed for sub %s", sub.id
                )
                stats["errors"] += 1
                continue
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
            .order_by(models.Subscription.expires_at.asc())
            .limit(RENEWAL_WINDOW_LIMIT)
            .all()
        )
        logger.info(
            "renewal_check.expiring_1d: %d sub(s) in window [now, now+1d]",
            len(expiring_1d),
        )
        for sub in expiring_1d:
            try:
                with session.begin_nested():
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
                    if existing_log:
                        logger.info(
                            "renewal_check.1d: sub=%s SKIP dedup matched log_id=%s "
                            "action=%r",
                            sub.id, existing_log.id, existing_log.action,
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
                    stats["reminded_1d"] += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "renewal_check: 1d reminder pass failed for sub %s", sub.id
                )
                stats["errors"] += 1
                continue
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
            .order_by(models.Subscription.expires_at.asc())
            .limit(RENEWAL_WINDOW_LIMIT)
            .all()
        )
        for sub in expiring_manual:
            try:
                with session.begin_nested():
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
            except Exception:  # noqa: BLE001
                logger.exception(
                    "renewal_check: 3d manual reminder pass failed for sub %s",
                    sub.id,
                )
                stats["errors"] += 1
                continue
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
            .order_by(models.Subscription.expires_at.asc())
            .limit(RENEWAL_WINDOW_LIMIT)
            .all()
        )
        logger.info(
            "renewal_check.expiring_manual_1d: %d sub(s) in window",
            len(expiring_manual_1d),
        )
        for sub in expiring_manual_1d:
            try:
                with session.begin_nested():
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
                    if existing_log:
                        logger.info(
                            "renewal_check.manual_1d: sub=%s SKIP dedup matched "
                            "log_id=%s action=%r",
                            sub.id, existing_log.id, existing_log.action,
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
                    stats["reminded_1d"] += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "renewal_check: 1d manual reminder pass failed for sub %s",
                    sub.id,
                )
                stats["errors"] += 1
                continue
        session.commit()

        RENEWAL_RUNS.labels(outcome="ok").inc()
        RENEWAL_LAST_RUN.set(now.timestamp())
    except Exception:  # noqa: BLE001
        logger.exception("Renewal check failed")
        stats["errors"] += 1
        RENEWAL_RUNS.labels(outcome="error").inc()
        if session.is_active:
            session.rollback()
        # Ре-бросаем: self-reschedule уже сделан в начале тика, поэтому
        # периодичность не пострадает, а джоба честно уйдёт в failed и
        # подсветится в /ops (иначе проглоченное падение неотличимо от
        # успеха — статус RQ остаётся finished).
        raise
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
    interval = _env_int("WARM_POOL_CHECK_INTERVAL", 120)
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
        # Ре-бросаем — см. run_renewal_check: падение тика должно уйти в
        # failed и подсветиться в /ops, а не молча вернуть finished.
        raise
    finally:
        session.close()

    return summary


def run_warm_pool_revoke_tick() -> dict:
    """Стадия 2 отзыва warm-пула — физически снять revoked-бандлы с нод.

    Драйвер для ``warm_pool.run_warm_pool_revoke_sweep``: находит уникальные
    ``(node_id, access_username)`` в ``pool_state=revoked`` (и юзер-отвязанные,
    и брошенные ``invalidate_node_warm_pool``) и гоняет ``state=absent`` по
    каждому, батчем ``WARM_POOL_REVOKE_BATCH_PER_TICK``. Без этого тика
    revoked-identity копятся вечно и в конфиге xray (лишние клиенты), и
    revoked-строками в БД (finding #71). Sweep сам делает back-off после
    ``WARM_POOL_REVOKE_MAX_ATTEMPTS`` провалов, чтобы не молотить мёртвую ноду.

    Self-reschedules через ``WARM_POOL_REVOKE_INTERVAL`` (default 300s).
    Disabled при 0 или ``WARM_POOL_ENABLED=0``.
    """
    from .db import SessionLocal
    from .queue import schedule_tick
    from .services import warm_pool

    # Reschedule в начале — см. run_pending_rescue_tick. Sweep ходит по
    # нодам ansible'ом и может зависнуть на SSH, поэтому reschedule ДО работы.
    interval = _env_int("WARM_POOL_REVOKE_INTERVAL", 300)
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_warm_pool_revoke_tick",
                interval,
                tick_id="tick-warm-pool-revoke",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("warm_pool_revoke: failed to re-enqueue tick (at start)")

    session = SessionLocal()
    summary: dict = {}
    try:
        summary = warm_pool.run_warm_pool_revoke_sweep(session)
    except Exception:  # noqa: BLE001
        logger.exception("warm_pool_revoke: sweep failed")
        # Ре-бросаем — см. run_renewal_check: падение тика уходит в failed → /ops.
        raise
    finally:
        session.close()

    return summary


def run_spawn_sweep_tick() -> dict:
    """Подбор спавнов, застрявших в ``registering`` (finding #70).

    Достройка спавна исторически жила в daemon-потоке backend'а — рестарт
    контейнера убивал её молча, и оплаченный сервер навсегда оставался
    невидимым (registering + placeholder-host), продолжая списывать деньги у
    хостера. Тик зовёт ``node_spawner.sweep_stuck_spawns``, который для каждой
    застрявшей ноды/exit'а ставит персистентную RQ-джобу
    ``run_spawn_finalize`` на провижининг-очередь (поток из тика не годится:
    work-horse завершается сразу после return и убил бы достройку).

    Self-reschedules через ``NODE_SPAWN_SWEEP_INTERVAL`` (default 600s).
    Disabled при 0. Порог «застрял» — ``NODE_SPAWN_STUCK_MINUTES`` (30).
    """
    from .db import SessionLocal
    from .queue import enqueue_spawn_finalize, schedule_tick
    from .services import node_spawner

    interval = _env_int("NODE_SPAWN_SWEEP_INTERVAL", 600)
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_spawn_sweep_tick",
                interval,
                tick_id="tick-spawn-sweep",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("spawn_sweep: failed to re-enqueue tick (at start)")

    session = SessionLocal()
    summary: dict = {}
    try:
        summary = node_spawner.sweep_stuck_spawns(
            session, enqueue=enqueue_spawn_finalize
        )
    except Exception:  # noqa: BLE001
        logger.exception("spawn_sweep: sweep failed")
        # Ре-бросаем — см. run_renewal_check: падение тика уходит в failed → /ops.
        raise
    finally:
        session.close()

    return summary


def run_spawn_finalize(kind: str, entity_id: int) -> dict:
    """RQ-джоба достройки одного застрявшего спавна (finding #70).

    Ставится ``queue.enqueue_spawn_finalize`` из spawn-sweep-тика; сама
    достройка (wait_for_ipv4 + host + coalesced bootstrap) — в
    ``node_spawner.resume_stuck_spawn``. Идёт на провижининг-очереди (не
    тиковой): может ждать IP минуты и не должна голодать короткие тики.
    """
    from .services import node_spawner

    return node_spawner.resume_stuck_spawn(kind, entity_id)


def run_retention_tick() -> dict:
    """Периодическая очистка безлимитно растущих таблиц (finding #247).

    Две таблицы пишутся в горячих путях и растут без потолка:
      * ``audit_logs`` — ``subscription_fetch`` на каждый фетч сабы (клиенты
        рефрешат ссылку каждые ~6ч) + ``*:delivered`` маркеры доставки
        уведомлений (дедуп по ним живёт лишь в узком окне продлений/триала);
      * ``node_traffic_samples`` — по строке на ноду каждые 5 мин с тяжёлым
        JSONB (детекторам нужны лишь последние тики/часы).

    Чистим строки старше N дней БАТЧАМИ (``id IN (SELECT id … LIMIT batch)``,
    коммит после каждого батча), чтобы не держать долгий лок на таблице.
    Прочие audit-события (провижининг, действия админов, DLQ и т.п.) НЕ
    трогаем — только высокочастотный шум. Индексы на ``created_at`` /
    ``observed_at`` (миграции 0019/0056) делают выборку батча дешёвой.

    Env:
      RETENTION_INTERVAL               default 86400 (раз в сутки), 0 = off
      AUDIT_LOG_RETENTION_DAYS         default 90, 0 = не чистить audit_logs
      TRAFFIC_SAMPLE_RETENTION_DAYS    default 30, 0 = не чистить сэмплы
      RETENTION_DELETE_BATCH           default 10000 (строк на батч)
      RETENTION_MAX_BATCHES            default 200 (потолок батчей/таблицу/тик)
    Self-reschedules.
    """
    from datetime import timedelta

    from sqlalchemy import or_

    from . import models
    from .db import SessionLocal
    from .queue import schedule_tick
    from .time_utils import utcnow

    # Reschedule в начале — см. run_pending_rescue_tick.
    interval = _env_int("RETENTION_INTERVAL", 86400)
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_retention_tick",
                interval,
                tick_id="tick-retention",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("retention: failed to re-enqueue tick (at start)")

    audit_days = _env_int("AUDIT_LOG_RETENTION_DAYS", 90)
    traffic_days = _env_int("TRAFFIC_SAMPLE_RETENTION_DAYS", 30)
    batch = max(1, _env_int("RETENTION_DELETE_BATCH", 10000))
    max_batches = max(1, _env_int("RETENTION_MAX_BATCHES", 200))
    now = utcnow()
    summary = {"audit_deleted": 0, "traffic_deleted": 0}

    session = SessionLocal()
    try:
        def _bulk_delete_by_ids(model, ids) -> int:
            return (
                session.query(model)
                .filter(model.id.in_(ids))
                .delete(synchronize_session=False)
            )

        # ── audit_logs: subscription_fetch + *:delivered старше N дней ──
        if audit_days > 0:
            cutoff = now - timedelta(days=audit_days)
            cond = or_(
                models.AuditLog.action == "subscription_fetch",
                models.AuditLog.action.like("%:delivered"),
            )
            for _ in range(max_batches):
                ids = [
                    row[0]
                    for row in session.query(models.AuditLog.id)
                    .filter(models.AuditLog.created_at < cutoff)
                    .filter(cond)
                    .limit(batch)
                    .all()
                ]
                if not ids:
                    break
                summary["audit_deleted"] += _bulk_delete_by_ids(models.AuditLog, ids)
                session.commit()
                if len(ids) < batch:
                    break

        # ── node_traffic_samples старше N дней (весь флот) ──
        if traffic_days > 0:
            cutoff = now - timedelta(days=traffic_days)
            for _ in range(max_batches):
                ids = [
                    row[0]
                    for row in session.query(models.NodeTrafficSample.id)
                    .filter(models.NodeTrafficSample.observed_at < cutoff)
                    .limit(batch)
                    .all()
                ]
                if not ids:
                    break
                summary["traffic_deleted"] += _bulk_delete_by_ids(
                    models.NodeTrafficSample, ids
                )
                session.commit()
                if len(ids) < batch:
                    break
    except Exception:  # noqa: BLE001
        logger.exception("retention: tick failed")
        if session.is_active:
            session.rollback()
        # Ре-бросаем — см. run_renewal_check: падение тика уходит в failed → /ops.
        raise
    finally:
        session.close()

    if summary["audit_deleted"] or summary["traffic_deleted"]:
        logger.info(
            "retention: удалено audit_logs=%s node_traffic_samples=%s",
            summary["audit_deleted"], summary["traffic_deleted"],
        )
    return summary


_LOW_BALANCE_THRESHOLD_DAYS = _env_int("LOW_BALANCE_WARN_DAYS", 3)


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
    interval = _env_int("BALANCE_CHARGE_INTERVAL", 3600)
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
        # Берём только id должников БЕЗ удержания локов: commit после каждой
        # подписки внутри цикла всё равно закрывал бы транзакцию и снимал
        # FOR UPDATE со ВСЕХ ещё не обработанных строк батча — оставшиеся 499
        # дальше «продлевались» бы без блокировки, и параллельный продлеватель
        # (renewal_check, второй тиковый воркер, ручной прогон) мог списать
        # деньги дважды. Поэтому лочим каждую подписку индивидуально в своей
        # короткой транзакции и перепроверяем условие уже под локом.
        due_renew_ids = [
            row[0]
            for row in session.query(models.Subscription.id)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.auto_renew.is_(True),
                models.Subscription.expires_at.isnot(None),
                models.Subscription.expires_at <= now,
            )
            .order_by(models.Subscription.expires_at.asc())
            .limit(500)
            .all()
        ]
        for sub_id in due_renew_ids:
            try:
                sub = (
                    session.query(models.Subscription)
                    .filter(models.Subscription.id == sub_id)
                    .with_for_update(skip_locked=True)
                    .first()
                )
                if sub is None:
                    # Строку залочил другой воркер (skip_locked) или её удалили —
                    # чужой тик её обработает, пропускаем.
                    session.rollback()
                    continue
                # Перепроверка под локом: пока строка ждала лока, параллельный
                # продлеватель мог уже продлить/экспайрить её (status/expires_at/
                # тумблер изменились) — тогда повторно списывать нельзя.
                if (
                    sub.status != models.SubscriptionStatus.active
                    or not sub.auto_renew
                    or sub.expires_at is None
                    or sub.expires_at > now
                ):
                    session.rollback()
                    continue
                ok = balance.renew_subscription(session, sub)
            except Exception:
                logger.exception("balance: renew failed for sub %s", sub_id)
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
        # Тот же паттерн, что и в пассе 1: commit-в-цикле после общего
        # with_for_update снимал локи со всего батча, поэтому лочим каждую
        # подписку по отдельности и перепроверяем условие под локом.
        expired_freeze_ids = [
            row[0]
            for row in session.query(models.Subscription.id)
            .filter(
                models.Subscription.status == models.SubscriptionStatus.frozen,
                models.Subscription.frozen_until.isnot(None),
                models.Subscription.frozen_until <= now,
            )
            .order_by(models.Subscription.frozen_until.asc())
            .limit(100)
            .all()
        ]
        for sub_id in expired_freeze_ids:
            try:
                sub = (
                    session.query(models.Subscription)
                    .filter(models.Subscription.id == sub_id)
                    .with_for_update(skip_locked=True)
                    .first()
                )
                if sub is None:
                    session.rollback()
                    continue
                if (
                    sub.status != models.SubscriptionStatus.frozen
                    or sub.frozen_until is None
                    or sub.frozen_until > now
                ):
                    session.rollback()
                    continue
                balance.unfreeze_subscription(session, sub, auto=True)
                stats["unfrozen"] += 1
                session.commit()
            except Exception:
                logger.exception("balance: auto-unfreeze failed for sub %s", sub_id)
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
        # Ре-бросаем — см. run_renewal_check: тик уходит в failed → виден в /ops.
        raise
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
    interval = _env_int("TRAFFIC_STATS_INTERVAL", 300)
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
        # Ре-бросаем — см. run_renewal_check: тик уходит в failed → виден в /ops.
        raise
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

    stale_hs_min = _env_int("AUTO_DIAGNOSE_HANDSHAKE_STALE_MIN", 10)
    observed_fresh_min = _env_int("AUTO_DIAGNOSE_OBSERVED_FRESH_MIN", 8)
    debounce_min = _env_int("AUTO_DIAGNOSE_DEBOUNCE_MIN", 30)
    max_per_tick = _env_int("AUTO_DIAGNOSE_MAX_PER_TICK", 3)

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

    debounce_min = _env_int("AUTO_DIAGNOSE_NODE_DEBOUNCE_MIN", 30)
    max_per_tick = _env_int("AUTO_DIAGNOSE_NODE_MAX_PER_TICK", 2)

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
    interval = _env_int("RELAY_LINK_HEALTH_INTERVAL", 300)
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
        # Ре-бросаем — см. run_renewal_check: тик уходит в failed → виден в /ops.
        raise
    finally:
        session.close()

    return summary


def _incident_auto_close_blocked(target, now) -> bool:
    """True → инцидент НЕЛЬЗЯ автозакрывать по ssh_ok/реконсайлу (finding #98).

    ``close_incident`` обнуляет ``diagnose_acked_at`` и ``diagnose_follow_mode``.
    Для двух случаев это стирает значимое состояние и делает кнопки крауд-пуша
    нефункциональными:

      * оператор явно взял инцидент в работу — ``ack`` (``diagnose_acked_at``
        свежее открытия) или ``follow`` (``diagnose_follow_mode=='exponential'``);
      * крауд-инцидент: нода выведена из пула по жалобам юзеров
        (``cooldown_until`` в будущем). SSH к такой ноде здоров КАЖДЫЙ тик —
        юзеров блокирует DPI/РКН, а не контроллера, — поэтому ssh_ok-ветка без
        этого гварда закрывала бы крауд-инцидент немедленно, стирая нажатый
        оператором ack/follow, и порог заново пушил бы после cooldown, как
        будто оператор ничего не жал.

    Ручной close (кнопка оператора) идёт мимо этого гварда — оператор всегда
    может закрыть инцидент сам. ``cooldown_until`` есть только у ``VPNNode``;
    у ``WGExitNode`` его нет — ``getattr`` вернёт None и ветка не сработает.
    """
    incident_open = getattr(target, "diagnose_incident_open_at", None)
    if incident_open is None:
        return False
    acked = getattr(target, "diagnose_acked_at", None)
    if acked is not None and acked >= incident_open:
        return True
    if getattr(target, "diagnose_follow_mode", None) == "exponential":
        return True
    cooldown_until = getattr(target, "cooldown_until", None)
    return cooldown_until is not None and cooldown_until > now


def _node_vpn_tcp_ports(node) -> list[int]:
    """TCP VPN-порты ноды для liveness-пробы сервиса (finding #1 сетевого аудита).

    SSH-доступность ≠ работающий VPN: xray мог упасть/не слушать порт при живом
    sshd. Берём порты из enabled ``VPNConfig`` ноды, ИСКЛЮЧАЯ hysteria2 (UDP —
    TCP-проба его не проверяет, иначе hysteria-only нода ложно читалась бы как
    degraded). ``NODE_VPN_PROBE_PORTS`` (csv) переопределяет список вручную;
    пустой результат = проверку порта пропускаем (прежнее поведение).
    """
    override = os.getenv("NODE_VPN_PROBE_PORTS", "").strip()
    if override:
        ports: set[int] = set()
        for tok in override.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                ports.add(int(tok))
            except ValueError:
                logger.warning("NODE_VPN_PROBE_PORTS: не число %r — пропускаю", tok)
        return sorted(ports)
    ports = set()
    for cfg in getattr(node, "configs", None) or []:
        if not getattr(cfg, "is_enabled", True):
            continue
        proto = getattr(cfg, "protocol", None)
        proto_val = getattr(proto, "value", proto)
        if proto_val == "hysteria2":  # UDP — TCP-пробой не проверяется
            continue
        p = getattr(cfg, "port", None)
        if p:
            ports.add(int(p))
    return sorted(ports)


def _probe_port_open(probe, port: int) -> bool:
    """True, если проба зафиксировала TCP-порт открытым (или его не проверяли).

    ``extra_tcp_ports`` кладёт в ``probe.checks`` запись с name ``tcp:{port}``
    и status ok/fail. Если записи нет (порт не пробовали) — НЕ считаем закрытым,
    чтобы не поднять ложный degraded.
    """
    name = f"tcp:{port}"
    for c in probe.checks:
        if c.get("name") == name:
            return c.get("status") == "ok"
    return True


def _controller_has_network() -> bool:
    """Есть ли у контроллера (worker) выход в сеть (finding #3 сетевого аудита).

    Перед тем как метить цели недоступными, убеждаемся, что упал не сам
    аплинк/DNS воркера. Пробуем TCP до внешних якорей ``NODE_CONTROLLER_ANCHORS``
    (csv host:port, default cloudflare+google:443). Хоть один ответил → сеть
    есть. ВСЕ молчат → считаем контроллер оффлайн. Пустой список отключает
    проверку (всегда True) — на случай egress-политики без прямого интернета.
    """
    import socket as _socket

    raw = os.getenv("NODE_CONTROLLER_ANCHORS", "1.1.1.1:443,8.8.8.8:443").strip()
    if not raw:
        return True
    anchors: list[tuple[str, int]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        h, sep, p = item.rpartition(":")
        if sep:
            try:
                anchors.append((h, int(p)))
            except ValueError:
                continue
        else:
            anchors.append((item, 443))
    if not anchors:
        return True
    timeout = float(os.getenv("NODE_CONTROLLER_ANCHOR_TIMEOUT", "5"))
    for host, port in anchors:
        try:
            with _socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


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
    from .services.admin_notify import notify_admins, notify_node_diagnosis
    from .services.provisioning import ProvisioningOrchestrator
    from .time_utils import utcnow

    # Reschedule first (SSH can hang) — same pattern as the other ticks.
    interval = _env_int("NODE_REACHABILITY_INTERVAL", 300)
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

    max_diag = _env_int("NODE_REACHABILITY_MAX_DIAGNOSE_PER_TICK", 4)
    # Анти-спам: не диагностируем/алертим, пока недоступность не подтвердилась
    # серией пробов длиной >= confirm_min минут (единичный пропущенный пинг —
    # не повод будить админа). Дефолт 20 мин ≈ «5 мин + 15 мин» из ТЗ; при
    # интервале тика 300с это ~4 проба подряд. Тюнится NODE_ALERT_CONFIRM_MIN;
    # 0 = старое поведение (алерт с первого DOWN).
    confirm_min = float(os.getenv("NODE_ALERT_CONFIRM_MIN", "20"))
    summary: dict = {
        "enabled": True,
        "checked": 0,
        "down": [],
        "recovered": [],
        "reconciled": [],
        "suspect": [],
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

        # Анти-голодание: обходим цели в порядке «дольше всех не пробовалась»
        # (last_probe_at ASC, NULLS FIRST). Раньше порядок был фиксированный
        # (все node, потом все exit): несколько лежащих нод в начале списка
        # съедали весь wall-clock бюджет (~30с таймаутов каждая), и хвост —
        # в первую очередь exit-ноды — не пробовался ВООБЩЕ, пока длится
        # отказ. Сортировка по давности проба сама ротирует порядок между
        # тиками: обрезанный бюджетом хвост становится самым «голодным» и
        # идёт первым в следующем тике.
        from datetime import datetime as _datetime, timedelta as _timedelta
        targets.sort(key=lambda kt: kt[1].last_probe_at or _datetime.min)

        # Бэкстоп-реконсиляция: закрыть инциденты, оставшиеся открытыми на уже
        # здоровых целях (last_probe_status=='ok', серии падений нет). Покрывает
        # пропуски штатного закрытия на ssh_ok — обрезанный бюджетом хвост,
        # крауд-открытые инциденты на SSH-здоровой ноде, подвисший тик. Дешёвый
        # проход без SSH, до бюджетного цикла, поэтому не голодает. Штатное
        # немедленное закрытие на ssh_ok ниже остаётся.
        recon_now = utcnow()
        recon_max_age = float(os.getenv("NODE_INCIDENT_RECONCILE_MAX_AGE_MIN", "30"))
        for kind, target in targets:
            # Крауд-инцидент / взятый оператором в работу не реконсилим:
            # close стёр бы ack/follow (finding #98). Оператор закрывает вручную.
            if _incident_auto_close_blocked(target, recon_now):
                continue
            if diagnostics_state.reconcile_healthy_incident(
                target, now=recon_now, max_age_min=recon_max_age
            ):
                summary["reconciled"].append(f"{kind}:{target.id}")
        if summary["reconciled"]:
            session.commit()

        # Finding #3: self-check связности контроллера ДО пер-нодовых пробов.
        # Если у самого воркера нет выхода в сеть (упал аплинк/DNS/NAT),
        # ping/tcp/ssh упадут для ВСЕХ целей разом и весь флот уйдёт в ложный
        # DOWN. Прежде чем метить цели, убеждаемся, что контроллер вообще
        # видит внешнюю сеть; если нет — один агрегированный алерт и выход.
        if not _controller_has_network():
            summary["controller_offline"] = True
            logger.error(
                "node_reachability: контроллер не видит внешнюю сеть (все якоря "
                "недоступны) — пропускаю пер-нодовые пробы, чтобы не пометить "
                "весь флот ложным DOWN"
            )
            try:
                notify_admins(
                    session,
                    kind="controller_offline",
                    text=(
                        "⚠️ Монитор доступности потерял выход в сеть (внешние "
                        "якоря недоступны). Пер-нодовые проверки пропущены, "
                        "чтобы не поднимать ложные алерты по всему флоту. "
                        "Проверьте сеть/DNS воркера."
                    ),
                    dedup_key={"scope": "reachability"},
                    window_sec=1800,
                    autocommit=True,
                )
            except Exception:  # noqa: BLE001
                logger.exception("node_reachability: controller-offline alert failed")
            return summary

        import time as _time

        # Per-tick wall-clock budget. Serial ping/ssh per DOWN target costs
        # seconds (each blackholed probe waits its timeout) and the RQ job
        # timeout is tick-node-reachability=240s. Stop probing once we near
        # it — the tick already self-rescheduled at the top, so the unprobed
        # tail is picked up next cycle instead of the whole tick getting
        # killed mid-loop by the RQ kill-horse (which would also rotate which
        # targets get starved). Default 200s leaves headroom under 240s.
        budget_s = _env_int("NODE_REACHABILITY_BUDGET_SEC", 200)
        started = _time.monotonic()
        diagnosed = 0
        down_count = 0
        # Finding #7: дыра между пробами > interval*gap_factor означает, что
        # непрерывного наблюдения не было — серию перезапускаем, а не
        # эскалируем по дырявому wall-clock.
        gap_factor = float(os.getenv("NODE_PROBE_GAP_FACTOR", "2"))
        # Finding #3 (mass-down): доля одновременно упавших целей выше порога →
        # подавляем индивидуальные алерты (вероятно, сеть контроллера, а не
        # весь флот сразу). Default 0 = выключено (анкерный self-check выше —
        # основной механизм).
        mass_down_fraction = float(os.getenv("NODE_MASS_DOWN_FRACTION", "0"))
        mass_down_min = _env_int("NODE_MASS_DOWN_MIN", 5)
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
            # Finding #7: запоминаем время ПРЕДЫДУЩЕГО проба ДО перезаписи —
            # нужно, чтобы отличить непрерывную серию DOWN от двух замеров,
            # разнесённых бюджетной дырой (тогда wall-clock врёт).
            prev_probe_at = getattr(target, "last_probe_at", None)
            # Finding #1: при живом SSH дополнительно пробим TCP VPN-порт(ы)
            # ноды — SSH-liveness ≠ VPN-liveness. Порты берём из enabled
            # VPNConfig (hysteria2/UDP исключён). У exit-нод (WG/UDP) — пусто.
            vpn_ports = _node_vpn_tcp_ports(target) if kind == "node" else []
            try:
                probe = diagnostics.run_local_path_probe(
                    host,
                    ssh_port=getattr(target, "ssh_port", 22) or 22,
                    extra_tcp_ports=vpn_ports or None,
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

            # net-audit #97: ssh-стадия НЕ выполнялась (нет paramiko или
            # ssh-ключа на контроллере) — проба недиагностична. Не трогаем
            # last_probe_status/unreachable_since: конфиг-ошибка контроллера
            # не должна класть весь флот в ложный DOWN со штормом пушей.
            # last_probe_at тоже не двигаем — stale_targets-метрика честно
            # покажет, что флот фактически не мониторится.
            if probe.ssh_skipped and not probe.ssh_ok:
                summary.setdefault("ssh_skipped", []).append(ref)
                continue

            if probe.ssh_ok:
                # Finding #1: SSH жив, но проверяем, слушают ли TCP VPN-порты.
                # ЗАКРЫТЫ ВСЕ заявленные порты при живом SSH = сервис (xray)
                # деградировал/упал. Требуем ИМЕННО все закрыты (не любой) —
                # единичный отключённый листенер не повод для алерта.
                degraded = bool(vpn_ports) and all(
                    not _probe_port_open(probe, p) for p in vpn_ports
                )
                if not degraded:
                    target.last_probe_at = now
                    target.last_probe_status = "ok"
                    target.unreachable_since = None  # серия прервалась — сброс
                    # ssh_ok у крауд-заблокированной ноды true КАЖДЫЙ тик (DPI
                    # блокирует юзеров, не контроллера). Без гварда close_incident
                    # стирал бы операторский ack/follow немедленно (finding #98).
                    if not _incident_auto_close_blocked(target, now):
                        if diagnostics_state.close_incident(target):
                            summary["recovered"].append(ref)
                    session.commit()
                    continue
                # ── DEGRADED: SSH ok, но VPN-порт(ы) не слушают ──────────
                target.last_probe_at = now
                target.last_probe_status = "degraded"
                symptom = "vpn_port_down"
                summary.setdefault("degraded", []).append(ref)
            else:
                # ── DOWN ────────────────────────────────────────────────
                target.last_probe_at = now
                target.last_probe_status = "unreachable"
                symptom = "unreachable"
                summary["down"].append(ref)

            down_count += 1

            # Confirm-окно: первый DOWN/degraded только запоминаем (начало
            # серии), не диагностируем и не алертим. Эскалируем (диагностика +
            # пуш) лишь когда недоступность держится >= confirm_min — несколько
            # пробов подряд. Транзиентный 1-2 пропущенных пинга сюда не дотянет
            # → recovery очистит unreachable_since и серия не накопится.
            if getattr(target, "unreachable_since", None) is None:
                target.unreachable_since = now
            elif (
                prev_probe_at is not None
                and interval > 0
                and (now - prev_probe_at).total_seconds() > interval * gap_factor
            ):
                # Finding #7: между прошлым и этим пробом дыра > interval*factor
                # (цель выпала в обрезанный бюджетом хвост / тик подвисал).
                # Непрерывного наблюдения не было — не эскалируем по дырявому
                # wall-clock, серию начинаем заново.
                target.unreachable_since = now
                summary.setdefault("series_reset", []).append(ref)
                session.commit()
                continue
            elapsed_min = (now - target.unreachable_since).total_seconds() / 60.0
            if confirm_min > 0 and elapsed_min < confirm_min:
                summary["suspect"].append(ref)
                session.commit()
                continue

            # Finding #3 (mass-down): доля DOWN/degraded среди уже пробитых
            # целей выше порога → вероятно, сеть у контроллера, а не весь флот
            # разом. Подавляем индивидуальные пуши/диагностику, шлём один
            # агрегированный алерт в конце. Default off (fraction=0).
            if (
                mass_down_fraction > 0
                and summary["checked"] >= mass_down_min
                and down_count / summary["checked"] >= mass_down_fraction
            ):
                summary["mass_down_suppressed"] = (
                    summary.get("mass_down_suppressed", 0) + 1
                )
                session.commit()
                continue

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
                    {"auto_triggered": True, "symptom": symptom},
                )
                session.commit()
                orchestrator.run_task_async(
                    task, node=target if kind == "node" else None
                )
            except Exception:  # noqa: BLE001
                logger.exception("node_reachability: diagnose enqueue failed for %s", ref)
                if session.is_active:
                    session.rollback()

        # Finding #3 (mass-down): если за тик подавили индивидуальные алерты по
        # массовой недоступности — шлём ОДИН агрегированный сигнал вместо лавины.
        if summary.get("mass_down_suppressed"):
            logger.error(
                "node_reachability: массовая недоступность (%s подавлено из %s "
                "пробитых) — вероятно, сеть контроллера/аплинка",
                summary["mass_down_suppressed"], summary["checked"],
            )
            try:
                notify_admins(
                    session,
                    kind="reachability_mass_down",
                    text=(
                        "⚠️ Монитор: массовая недоступность нод за один тик — "
                        "индивидуальные алерты подавлены (вероятно, сетевой сбой "
                        "контроллера/аплинка, а не всех нод сразу). Проверьте "
                        "сеть воркера."
                    ),
                    dedup_key={"scope": "reachability"},
                    window_sec=1800,
                    autocommit=True,
                )
            except Exception:  # noqa: BLE001
                logger.exception("node_reachability: mass-down alert failed")

        # net-audit #97: ssh-пробы скипались по конфиг-ошибке контроллера —
        # доступность флота фактически НЕ проверяется. Один агрегированный
        # алерт с дедупом вместо тихого «всё ок» (статусы не обновляются, а
        # значит и down-детект, и recovery заморожены до починки окружения).
        if summary.get("ssh_skipped"):
            logger.error(
                "node_reachability: ssh-проба пропущена для %s целей (нет "
                "paramiko или ssh-ключа на контроллере) — статусы нод не "
                "обновляются, мониторинг слеп",
                len(summary["ssh_skipped"]),
            )
            try:
                notify_admins(
                    session,
                    kind="reachability_ssh_skipped",
                    text=(
                        "⚠️ Монитор доступности не может выполнить ssh-пробу "
                        "(paramiko или ssh-ключ недоступны на контроллере). "
                        "Статусы нод/exit'ов не обновляются — down-детект и "
                        "recovery заморожены. Проверьте окружение воркера "
                        "(ANSIBLE_PRIVATE_KEY_FILE / paramiko)."
                    ),
                    dedup_key={"scope": "reachability-ssh-skipped"},
                    window_sec=1800,
                    autocommit=True,
                )
            except Exception:  # noqa: BLE001
                logger.exception("node_reachability: ssh-skipped alert failed")

        # Метрика самого голодания: сколько целей не пробовано дольше
        # NODE_REACHABILITY_STALE_MIN минут (default 30). Стабильно >0 —
        # бюджета тика не хватает на весь флот, пора его поднимать или
        # ускорять пробы. Цели без host не считаем — их тик не пробует.
        stale_min = float(os.getenv("NODE_REACHABILITY_STALE_MIN", "30"))
        stale_cutoff = utcnow() - _timedelta(minutes=stale_min)
        stale = sum(
            1 for _k, t in targets
            if getattr(t, "host", None)
            and (t.last_probe_at is None or t.last_probe_at < stale_cutoff)
        )
        summary["stale_targets"] = stale
        REACHABILITY_STALE_TARGETS.set(stale)
    except Exception:  # noqa: BLE001
        logger.exception("node_reachability: tick failed")
        if session.is_active:
            session.rollback()
        # Ре-бросаем — см. run_renewal_check: тик уходит в failed → виден в /ops.
        raise
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

    interval = _env_int("USER_HEALTH_PING_INTERVAL", 1800)
    batch = _env_int("USER_HEALTH_PING_BATCH", 50)
    # Базовая дебаунс-дельта = минимум между ping'ами. Jitter (random
    # forward-offset) добавляется к `health_ping_last_at` при записи,
    # чтобы фактический интервал растянулся в [base, base+jitter] на
    # юзера. Дефолты: 168ч (7 дн.) base + 168ч (7 дн.) jitter → ping
    # каждые 7-14 дней случайно. Раньше было 24ч fixed — юзеры жали
    # «не работает» в игнор из-за фоновой усталости, и реальные
    # жалобы тонули в шуме.
    debounce_hours = _env_int("USER_HEALTH_PING_DEBOUNCE_HOURS", 168)
    jitter_hours = _env_int("USER_HEALTH_PING_DEBOUNCE_JITTER_HOURS", 168)

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
    ping_hour_start = _env_int("HEALTH_PING_HOUR_START", 11)
    ping_hour_end = _env_int("HEALTH_PING_HOUR_END", 14)

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
        # Ре-бросаем — см. run_renewal_check: тик уходит в failed → виден в /ops.
        raise
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

    interval = _env_int("BROADCAST_DISPATCH_INTERVAL", 10)
    batch_size = _env_int("BROADCAST_BATCH_SIZE", 50)

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
        # Ре-бросаем — см. run_renewal_check: тик уходит в failed → виден в /ops.
        raise
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
    """RQ exception handler — вызывается на КАЖДОМ падении джобы.

    ВАЖНО: RQ зовёт exception handlers ДО retry-логики (handle_exception →
    handle_job_failure), поэтому обработчик срабатывает на каждой попытке, а
    не только на финальной. Наивная реализация инкрементила DLQ-счётчик, писала
    provisioning_dlq в аудит и пушила админу «Провижининг упал» уже на первой
    транзиентной ошибке ansible-джобы (у неё Retry(max=3)), хотя через 10с она
    успешно ретраилась.

    Поэтому:
      * пока у джобы остались ретраи (``job.retries_left`` > 0) — это НЕ
        финальное падение: только warning, без DLQ/аудита/пуша;
      * реальный dead-letter — когда ретраи исчерпаны (retries_left == 0) или
        у джобы вовсе нет Retry (retries_left is None → падение сразу
        финальное);
      * аудит/пуш с ``target_type='provisioning_task'`` пишем ТОЛЬКО для
        provisioning-джоб (``run_provisioning_task``), у которых ``args[0]`` —
        это task_id. У ``run_ops_plan_execute`` args[0] — plan_id, у
        ``run_scale_workers`` — число реплик; приписывать их id несуществующей
        provisioning-таске нельзя.
    """
    retries_left = getattr(job, "retries_left", None)
    if retries_left:
        # retries_left > 0 (truthy) — впереди ещё ретрай, падение транзиентное.
        logger.warning(
            "Job %s (%s) упала, но остались ретраи (retries_left=%s): %s",
            job.id, getattr(job, "func_name", "?"), retries_left, exc_value,
        )
        return True  # let RQ continue its normal failure/retry flow

    func_name = getattr(job, "func_name", "") or ""
    if not func_name.endswith("run_provisioning_task"):
        # Не provisioning-джоба — args[0] не task_id, DLQ-аудит неприменим.
        logger.error(
            "Job %s (%s) dead-lettered после ретраев: %s",
            job.id, func_name or "?", exc_value,
        )
        return True  # let RQ continue its normal failure flow

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


def _fail_stuck_ops_plan(plan_id: int, reason: str, *, phase: str = "crash") -> bool:
    """Страховка: условный UPDATE executing→failed для ops-плана.

    Открывает СВЕЖУЮ сессию (рабочая после исключения может быть в
    неопределённом состоянии) и переводит план в failed ТОЛЬКО если он всё
    ещё в executing — терминальные статусы, выставленные ``execute_plan``
    (expired / failed с деталями фазы), не затираются. Без этого любое
    исключение вне OpsExecError оставляло план в executing навсегда:
    эндпоинт армит план ДО постановки джобы и принимает только proposed,
    так что подтверждённый оператором план было не перезапустить.
    Возвращает True, если статус реально флипнули.
    """
    from sqlalchemy import update as sa_update

    from . import models
    from .db import SessionLocal
    from .time_utils import utcnow

    session = SessionLocal()
    try:
        flipped = session.execute(
            sa_update(models.OpsPlan)
            .where(
                models.OpsPlan.id == plan_id,
                models.OpsPlan.status == "executing",
            )
            .values(
                status="failed",
                execution={
                    "phase": phase,
                    "reason": reason[:500],
                    "failed_at": utcnow().isoformat(),
                },
            )
        ).rowcount
        session.commit()
        if flipped:
            logger.warning(
                "ops plan %s: помечен failed (phase=%s) — застрял в executing",
                plan_id, phase,
            )
        return bool(flipped)
    except Exception:  # noqa: BLE001
        logger.exception("ops plan %s: не смог пометить план failed", plan_id)
        return False
    finally:
        session.close()


def run_ops_plan_execute(plan_id: int) -> dict:
    """RQ-джоба: исполнить сохранённый ops-план (Phase 3, за флагом
    OPS_EXECUTE_ENABLED). Эндпоинт ``/api/agent/ops/execute`` армит план
    (status=executing) и энкьюит сюда; вся тяжёлая работа (ре-валидация,
    pre-flight по живым ценам/балансу, заказ нод через spawn_node_async) — тут,
    без HTTP-таймаута. Результат пишется в ``ops_plans.execution``."""
    from . import models
    from .db import SessionLocal
    from .services.agent.ops_execution import OpsExecError, execute_plan

    session = SessionLocal()
    try:
        plan = session.get(models.OpsPlan, plan_id)
        if not plan:
            return {"ok": False, "error": f"ops_plan {plan_id} not found"}
        result = execute_plan(session, plan)
        return {"ok": True, **result}
    except OpsExecError as exc:
        logger.warning("ops plan %s execution rejected/failed: %s", plan_id, exc)
        # execute_plan сам выставляет терминальный статус на путях
        # integrity/validate/preflight/TTL, но ветка «исполнение выключено
        # (OPS_EXECUTE_ENABLED=0)» оставляла план в executing — добиваем.
        # Условный UPDATE не тронет уже выставленные failed/expired.
        _fail_stuck_ops_plan(plan_id, str(exc), phase="rejected")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("ops plan %s execution crashed", plan_id)
        # Иначе план навсегда завис бы в executing (ретрай через эндпоинт
        # закрыт: он принимает только status=proposed).
        _fail_stuck_ops_plan(plan_id, f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": str(exc)}
    finally:
        session.close()


def run_ops_plan_reaper_tick() -> dict:
    """Бэкстоп по возрасту: добить ops-планы, залипшие в executing.

    In-job страховка (``_fail_stuck_ops_plan``) не спасает, когда процесса
    уже нет: воркер перезапустился/упал по OOM или RQ убил джобу по
    job_timeout=1800 — план остаётся в executing навсегда, а эндпоинт
    повторное исполнение запрещает (принимает только proposed).

    Момент арминга берём из AuditLog(action='agent_ops_execute_armed') —
    эндпоинт пишет его одной транзакцией с армом; отдельной колонки
    executing_since нет, и миграцию ради бэкстопа не заводим. Fallback —
    expires_at (арм всегда РАНЬШЕ протухания, значит оценка консервативна),
    затем created_at. Планы, армленные раньше чем job_timeout+grace назад,
    переводятся в failed (phase='crash') условным UPDATE'ом — с ещё живой
    джобой не гоняемся. Self-reschedules; OPS_PLAN_REAPER_INTERVAL=0 — off.
    """
    from datetime import timedelta

    from sqlalchemy import update as sa_update

    from . import models
    from .db import SessionLocal
    from .queue import schedule_tick
    from .time_utils import utcnow

    # Reschedule в начале — см. run_pending_rescue_tick.
    interval = _env_int("OPS_PLAN_REAPER_INTERVAL", 300)
    if interval > 0:
        try:
            schedule_tick(
                "app.worker.run_ops_plan_reaper_tick",
                interval,
                tick_id="tick-ops-plan-reaper",
                replace=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("ops_plan_reaper: failed to re-enqueue tick (at start)")

    # job_timeout джобы run_ops_plan_execute (см. api/agent.py, 1800с) +
    # запас на ожидание в очереди/clock skew.
    job_timeout = _env_int("OPS_EXECUTE_JOB_TIMEOUT", 1800)
    grace = _env_int("OPS_PLAN_REAPER_GRACE", 120)
    summary: dict = {"checked": 0, "reaped": []}
    session = SessionLocal()
    try:
        cutoff = utcnow() - timedelta(seconds=job_timeout + grace)
        stuck = (
            session.query(models.OpsPlan)
            .filter(models.OpsPlan.status == "executing")
            .all()
        )
        summary["checked"] = len(stuck)
        for plan in stuck:
            armed_log = (
                session.query(models.AuditLog)
                .filter(
                    models.AuditLog.action == "agent_ops_execute_armed",
                    models.AuditLog.target_type == "ops_plan",
                    models.AuditLog.target_id == plan.id,
                )
                .order_by(models.AuditLog.created_at.desc())
                .first()
            )
            armed_at = (
                armed_log.created_at if armed_log is not None
                else (plan.expires_at or plan.created_at)
            )
            if armed_at is None or armed_at > cutoff:
                continue  # ещё может легитимно исполняться — ждём
            reason = (
                f"executing дольше {job_timeout + grace}с с момента арма "
                f"({armed_at.isoformat()}) — воркер умер или джоба убита "
                f"по job_timeout; что успело заказаться — проверь по нодам "
                f"с notes='ops-agent plan #{plan.id}'"
            )
            flipped = session.execute(
                sa_update(models.OpsPlan)
                .where(
                    models.OpsPlan.id == plan.id,
                    models.OpsPlan.status == "executing",
                )
                .values(
                    status="failed",
                    execution={
                        "phase": "crash",
                        "reason": reason,
                        "failed_at": utcnow().isoformat(),
                    },
                )
            ).rowcount
            if not flipped:
                continue
            session.add(
                models.AuditLog(
                    actor="ops-plan-reaper",
                    actor_type=models.AuditActor.system,
                    action="agent_ops_execute_reaped",
                    target_type="ops_plan",
                    target_id=plan.id,
                    extra={"armed_at": armed_at.isoformat(), "reason": reason},
                )
            )
            summary["reaped"].append(plan.id)
            logger.warning("ops_plan_reaper: план %s → failed (%s)", plan.id, reason)
        session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("ops_plan_reaper: tick failed")
        if session.is_active:
            session.rollback()
        # Ре-бросаем — см. run_renewal_check: тик уходит в failed → виден в /ops.
        raise
    finally:
        session.close()

    return summary


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
    pending_rescue_interval = _env_int("PENDING_RESCUE_INTERVAL", 60)
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

    # Schedule lava.top reconcile (webhook-independent credit of card
    # payments). lava's webhook delivery is best-effort and was observed
    # missing in prod; this tick polls GET /api/v2/invoices and credits any
    # pending invoice whose lava sale is COMPLETED. No-op без LAVA_TOP_API_KEY.
    lava_reconcile_interval = _env_int("LAVA_TOP_RECONCILE_INTERVAL", 60)
    if do_bootstrap and lava_reconcile_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_lava_reconcile_tick",
                min(lava_reconcile_interval, 30),
                tick_id="tick-lava-reconcile",
                replace=True,
            )
            logger.info(
                "Lava reconcile tick bootstrapped: first run in 30s (interval=%ss)",
                lava_reconcile_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule lava reconcile tick")

    # Schedule operator-report watcher (Phase 1 operator-aware routing) —
    # resolves «VPN не работает» reports by observed reconnect. Default 5 min.
    operator_watch_interval = _env_int("OPERATOR_REPORT_WATCH_INTERVAL", 300)
    if do_bootstrap and operator_watch_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_operator_report_watch_tick",
                min(operator_watch_interval, 60),
                tick_id="tick-operator-report-watch",
                replace=True,
            )
            logger.info(
                "Operator-report watcher bootstrapped: first run in 60s "
                "(interval=%ss)", operator_watch_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule operator-report watcher tick")

    # Phase 3 reconcile tick — сходит ноды по desired-state generations.
    # No-op пока RECONCILER_ENABLED выключен (сам тик short-circuit'ит).
    # Дефолт 3s; debounce RECONCILE_DEBOUNCE_S коллапсит burst правок.
    reconcile_interval = _env_int("RECONCILE_INTERVAL", 3)
    if do_bootstrap and reconcile_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_reconcile_tick",
                reconcile_interval,
                tick_id="tick-reconcile",
                replace=True,
            )
            logger.info(
                "Reconcile tick bootstrapped: interval=%ss (RECONCILER_ENABLED=%r)",
                reconcile_interval, os.getenv("RECONCILER_ENABLED", ""),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule reconcile tick")

    # Schedule autoscale tick
    autoscale_interval = _env_int("AUTOSCALE_INTERVAL", 0)
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
    renewal_interval = _env_int("RENEWAL_CHECK_INTERVAL", 3600)
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

    # Schedule cloud-billing guard (default: every hour). Pulls provider
    # balance (gauge + low-balance alert) + fleet monthly-cost gauge. No-op
    # без cloud-провайдеров. CLOUD_BILLING_INTERVAL=0 → выключить.
    cloud_billing_interval = _env_int("CLOUD_BILLING_INTERVAL", 3600)
    if do_bootstrap and cloud_billing_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_cloud_billing_tick",
                min(cloud_billing_interval, 120),
                tick_id="tick-cloud-billing",
                replace=True,
            )
            logger.info(
                "Cloud-billing guard bootstrapped: first run in 120s (interval=%ss)",
                cloud_billing_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule cloud-billing tick")

    # Schedule warm-pool check (default: every 2 min). Stage 2.5 of the
    # WebApp roadmap — keeps each active node's pool topped up so user
    # purchases hit a warm bundle instead of paying the Ansible cost.
    warm_interval = _env_int("WARM_POOL_CHECK_INTERVAL", 120)
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

    # Warm-pool revoke sweep (stage 2) — физически снимает revoked-бандлы с
    # нод, иначе брошенные identity копятся в xray-конфиге и revoked-строками
    # в БД (finding #71). Интервал WARM_POOL_REVOKE_INTERVAL (default 300s).
    warm_revoke_interval = _env_int("WARM_POOL_REVOKE_INTERVAL", 300)
    if do_bootstrap and warm_revoke_interval > 0 and warm_enabled:
        try:
            schedule_tick(
                "app.worker.run_warm_pool_revoke_tick",
                min(warm_revoke_interval, 60),
                tick_id="tick-warm-pool-revoke",
                replace=True,
            )
            logger.info(
                "Warm pool revoke sweep bootstrapped: first run in 60s (interval=%ss)",
                warm_revoke_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule warm pool revoke sweep")

    # Spawn-sweep — подбор спавнов, застрявших в registering после рестарта
    # backend'а (daemon-поток финализации умер). Достройку ставит персистентной
    # RQ-джобой на провижининг-очередь (finding #70). Default 600s, 0=off.
    spawn_sweep_interval = _env_int("NODE_SPAWN_SWEEP_INTERVAL", 600)
    if do_bootstrap and spawn_sweep_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_spawn_sweep_tick",
                min(spawn_sweep_interval, 120),
                tick_id="tick-spawn-sweep",
                replace=True,
            )
            logger.info(
                "Spawn sweep bootstrapped: first run in 120s (interval=%ss)",
                spawn_sweep_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule spawn sweep")

    # Schedule balance charge tick (default: hourly). Stage 4 — drives
    # daily-billing ticks for balance subscriptions and auto-unfreezes
    # paused ones whose frozen_until has lapsed.
    balance_interval = _env_int("BALANCE_CHARGE_INTERVAL", 3600)
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
    traffic_stats_interval = _env_int("TRAFFIC_STATS_INTERVAL", 300)
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
    relay_link_health_interval = _env_int("RELAY_LINK_HEALTH_INTERVAL", 300)
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
    node_reach_interval = _env_int("NODE_REACHABILITY_INTERVAL", 300)
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
    health_ping_interval = _env_int("USER_HEALTH_PING_INTERVAL", 1800)
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
    broadcast_interval = _env_int("BROADCAST_DISPATCH_INTERVAL", 10)
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

    # Бэкстоп залипших ops-планов: executing старше job_timeout+grace →
    # failed (воркер умер / джоба убита по таймауту). См. run_ops_plan_reaper_tick.
    ops_reaper_interval = _env_int("OPS_PLAN_REAPER_INTERVAL", 300)
    if do_bootstrap and ops_reaper_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_ops_plan_reaper_tick",
                min(ops_reaper_interval, 120),
                tick_id="tick-ops-plan-reaper",
                replace=True,
            )
            logger.info(
                "Ops-plan reaper tick bootstrapped: first run in %ss (interval=%ss)",
                min(ops_reaper_interval, 120), ops_reaper_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule ops-plan reaper tick")

    # Retention — очистка безлимитно растущих audit_logs / node_traffic_samples
    # (finding #247). Раз в сутки по умолчанию; RETENTION_INTERVAL=0 → off.
    retention_interval = _env_int("RETENTION_INTERVAL", 86400)
    if do_bootstrap and retention_interval > 0:
        try:
            schedule_tick(
                "app.worker.run_retention_tick",
                min(retention_interval, 300),
                tick_id="tick-retention",
                replace=True,
            )
            logger.info(
                "Retention tick bootstrapped: first run in %ss (interval=%ss)",
                min(retention_interval, 300), retention_interval,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule retention tick")

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
    if with_scheduler:
        # ── Fix: "RQ-scheduler залипает после деплоя" ──
        # RQScheduler.acquire_locks() (rq/scheduler.py) делает
        #   SET rq:scheduler-lock:<queue> <pid> NX EX (interval+60)
        # и форкает scheduler ТОЛЬКО если захватил хоть один лок
        # (auto_start: `if self._acquired_locks and auto_start: self.start()`).
        # При деплое `docker compose up -d` пересоздаёт контейнер — старый
        # worker-scheduler получает SIGKILL после stop_grace и НЕ доходит до
        # graceful stop()→release_locks(), поэтому его лок остаётся висеть
        # (TTL ~interval+60). Свежий контейнер на старте делает acquire_locks
        # ОДИН раз → NX фейлится (лок мёртвого инстанса жив) → scheduler не
        # форкается, а reacquire-петля (`work()`: should_reacquire_locks)
        # крутится только ВНУТРИ форкнутого процесса, которого нет → ретрая
        # нет. Итог: ВСЕ тики стоят, пока следующий рестарт случайно не
        # попадёт в окно после истечения TTL (наблюдали ~9 мин простоя).
        # Очередь наша эксклюзивно (worker-scheduler replicas=1, recreate
        # последовательный: стоп старого → старт нового), легитимного
        # держателя в этот момент нет — чистим stale-лок до work().
        from rq.scheduler import RQScheduler

        for q in queues_to_listen:
            lock_key = RQScheduler.get_locking_key(q.name)
            if connection.delete(lock_key):
                logger.warning(
                    "Cleared stale RQ scheduler lock %s (предыдущий "
                    "worker-scheduler умер без release_locks) — иначе тики "
                    "залипли бы до истечения TTL",
                    lock_key,
                )
    worker.work(with_scheduler=with_scheduler)


if __name__ == "__main__":
    main()
