"""Node health aggregation and auto-migration.

Two sides:

* **Ingestion** — probers (active checks from RU / KZ / EU rigs, or passive
  reports from clients) POST probe results to
  ``POST /api/nodes/{id}/probes``. The route delegates to
  :func:`record_probe`, which stores a :class:`HealthProbe` row and refreshes
  aggregated health on the node.

* **Reaction** — :func:`recompute_node_health` recalculates ``health_score``
  and ``blocked_regions`` from the most recent probes. Авто-миграция по
  healthcheck отключена с 2026-04-15 (флаппинг переселял активных юзеров);
  вместо неё на probe-блок региона / probe-смерть уходит админ-пуш через
  :func:`_alert_probe_degradation`, а решение о ручной миграции принимает
  оператор. Ручной маршрут по-прежнему зовёт :func:`migrate_subscriptions_off`.

The "healthy node" selector reuses :func:`services.provisioning.choose_node`
so capacity, cooldown and pool constraints are respected identically to the
purchase flow.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

from ..time_utils import utcnow

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models

logger = logging.getLogger(__name__)

# How far back we look when computing aggregate health.
LOOKBACK = timedelta(minutes=15)
# Region is "blocked" if success rate from that region drops below this.
BLOCK_THRESHOLD = 0.3
# Node is unhealthy overall if average success rate across all regions is
# below this — triggers migration even if blocks are not region-specific.
DEAD_THRESHOLD = 0.2
# Cooldown when a node is auto-disabled after a block.
DEFAULT_COOLDOWN = timedelta(days=3)


def record_probe(
    db: Session,
    *,
    node: models.VPNNode,
    source_region: str,
    result: models.ProbeResult,
    latency_ms: int | None = None,
    source_kind: str | None = None,
    details: dict | None = None,
) -> models.HealthProbe:
    probe = models.HealthProbe(
        node_id=node.id,
        source_region=source_region,
        source_kind=source_kind,
        result=result,
        latency_ms=latency_ms,
        details=details,
    )
    db.add(probe)
    db.flush()
    return probe


# Minimum probe count required before we trust an aggregate result.
# With only a handful of samples a single failed probe blows the success
# rate under DEAD_THRESHOLD and triggers a bogus migration. The gate is
# applied both globally and per-region.
MIN_SAMPLES = 5


def _aggregate(
    db: Session, node_id: int, since: datetime
) -> tuple[float, dict[str, float], int, dict[str, int]]:
    """Return (overall_success_rate, per_region_success_rate, total_samples, per_region_samples)."""
    rows = (
        db.query(
            models.HealthProbe.source_region,
            models.HealthProbe.result,
            func.count(models.HealthProbe.id),
        )
        .filter(
            models.HealthProbe.node_id == node_id,
            models.HealthProbe.observed_at >= since,
        )
        .group_by(models.HealthProbe.source_region, models.HealthProbe.result)
        .all()
    )

    per_region_total: dict[str, int] = defaultdict(int)
    per_region_ok: dict[str, int] = defaultdict(int)
    total = 0
    ok_total = 0
    for region, result, count in rows:
        per_region_total[region] += count
        total += count
        if result == models.ProbeResult.ok:
            per_region_ok[region] += count
            ok_total += count

    per_region = {
        region: (per_region_ok[region] / per_region_total[region])
        for region in per_region_total
        if per_region_total[region] > 0
    }
    # #64 — zero probes → None, not 1.0. Prevents fresh/unprobed nodes
    # from appearing as 100% healthy in UI and autoscale selector.
    overall = (ok_total / total) if total else None
    return overall, per_region, total, dict(per_region_total)


def recompute_node_health(
    db: Session,
    node: models.VPNNode,
    *,
    auto_migrate: bool = True,
) -> dict:
    """Refresh ``health_score``/``blocked_regions`` and optionally migrate.

    Returns a small dict with the computed numbers — useful for API output.
    """
    since = utcnow() - LOOKBACK
    overall, per_region, total_samples, per_region_samples = _aggregate(
        db, node.id, since
    )

    # Only consider regions with enough samples for a block decision —
    # a single failed probe should not flag an entire region as blocked.
    blocked = sorted(
        region
        for region, rate in per_region.items()
        if rate < BLOCK_THRESHOLD and per_region_samples.get(region, 0) >= MIN_SAMPLES
    )
    node.blocked_regions = blocked or None
    node.health_score = int(round(overall * 100)) if overall is not None else None
    node.last_health_check_at = utcnow()

    migrated: list[int] = []
    # Признак «probe-смерти» ноды: агрегатный success-rate ниже порога при
    # достаточной выборке. Единственный сигнал DPI-блока/деградации, который
    # reachability-тик по SSH НЕ видит (SSH до ноды жив, а клиентский трафик
    # режется). Считаем всегда — используется и для алерта ниже.
    global_death = (
        overall is not None
        and overall < DEAD_THRESHOLD
        and total_samples >= MIN_SAMPLES
    )

    # DISABLED 2026-04-15: автомиграция по healthcheck отключена — на
    # транзиентных сбоях probes (сетевой дребезг, единичные таймауты)
    # мы флипали ноду в error и переселяли активных пользователей, что
    # проявлялось как отвал подключения каждые 5-10 минут. До пересмотра
    # порогов / anti-flap логики оставляем функцию считать score и
    # blocked_regions, но решения о миграции теперь принимает админ
    # через ручной маршрут. Чтобы вернуть поведение — расхешировать блок.
    # if auto_migrate and (global_death or blocked):
    #     logger.warning(
    #         "Node %s degraded: health=%.2f blocked_regions=%s — considering migration",
    #         node.name,
    #         overall,
    #         blocked,
    #     )
    #     if global_death:
    #         node.status = models.VPNNodeStatus.error
    #         node.is_active = False
    #         node.cooldown_until = utcnow() + DEFAULT_COOLDOWN
    #         migrated = migrate_subscriptions_off(db, node, reason="node unreachable")[
    #             "subscription_ids"
    #         ]

    # #7 — маршрутизация сигнала probe-блокировки/probe-смерти. Даже при
    # отключённой авто-миграции оператор ДОЛЖЕН получить сигнал: probe-риги
    # (RU/KZ/EU) — единственный детектор DPI-блока, который SSH-тик не ловит.
    # Раньше блок молча копился в node.blocked_regions без единого алерта.
    # Шлём админ-пуш вместо молчания; только на автоматическом (probe-driven)
    # пути — ручной пересчёт из админки (auto_migrate=False) не алертит, чтобы
    # клик по «пересчитать» не порождал пуши.
    if auto_migrate and (blocked or global_death):
        _alert_probe_degradation(
            db,
            node,
            blocked=blocked,
            overall=overall,
            total_samples=total_samples,
            global_death=global_death,
        )

    db.add(node)
    db.commit()
    return {
        "node_id": node.id,
        "health_score": node.health_score,
        "blocked_regions": blocked,
        "overall_success_rate": overall,
        "per_region": per_region,
        "migrated_subscriptions": migrated,
    }


def _alert_probe_degradation(
    db: Session,
    node: models.VPNNode,
    *,
    blocked: list[str],
    overall: float | None,
    total_samples: int,
    global_death: bool,
) -> None:
    """Админ-пуш о probe-блокировке региона / probe-смерти ноды.

    Замена молчания после отключения авто-миграции: probe-риги детектят
    DPI-блок, который reachability-тик по SSH не видит, — оператор должен
    узнать и решить о ручной миграции. Без изменения самой ноды (только
    сигнал).

    Анти-спам (по плану находки #7):
      * ``diagnostics_state.is_alerts_muted`` — если оператор заглушил
        алерты по этой ноде, молчим (тот же mute, что и для SSH-инцидентов);
      * dedup-окно ``notify_admins`` по (node_id + отпечаток deg-состояния):
        флаппинг даёт один пуш на окно ``ADMIN_ALERT_BLOCKED_WINDOW_SEC``
        (default 1800с), а не поток. Отпечаток включает список blocked-
        регионов и флаг probe-смерти, чтобы НОВЫЙ регион/переход в death
        пробивал дедуп свежим алертом.

    ``notify_admins`` вызывается с ``autocommit=False`` — строки уедут в БД
    вместе с финальным ``db.commit()`` в ``recompute_node_health``.
    """
    from . import diagnostics_state
    from .admin_notify import notify_admins

    # Уважаем ручной mute алертов по ноде (оператор уже в курсе / работает).
    if diagnostics_state.is_alerts_muted(node):
        return

    name = getattr(node, "name", None) or str(getattr(node, "id", "?"))
    host = getattr(node, "host", "") or ""
    host_suffix = f" ({host})" if host else ""
    pct = f"{round(overall * 100)}%" if overall is not None else "n/a"

    if global_death:
        kind = "node_probe_death"
        text = (
            f"🔴 Нода <b>{name}</b>{host_suffix} — probe-смерть: успех {pct} "
            f"по {total_samples} пробам за {int(LOOKBACK.total_seconds() // 60)} мин. "
            f"SSH может быть жив, но клиентский трафик не проходит. "
            f"Авто-миграция отключена — решите о ручной миграции (/admin/nodes)."
        )
    else:
        kind = "node_region_blocked"
        regions = ", ".join(blocked) if blocked else "?"
        text = (
            f"🔴 Нода <b>{name}</b>{host_suffix} заблокирована в регионе(ах): "
            f"<b>{regions}</b>. Probe-риги видят DPI-блок (SSH до ноды жив). "
            f"Авто-миграция отключена — решите о ручной миграции (/admin/nodes)."
        )

    window = int(os.getenv("ADMIN_ALERT_BLOCKED_WINDOW_SEC", "1800"))
    try:
        notify_admins(
            db,
            kind=kind,
            text=text,
            dedup_key={
                "node_id": node.id,
                "regions": blocked,
                "probe_death": bool(global_death),
            },
            extra={
                "node_id": node.id,
                "target_kind": "node",
                "target_id": node.id,
                "blocked_regions": blocked,
                "health_score": node.health_score,
            },
            window_sec=window,
            autocommit=False,
        )
    except Exception:  # noqa: BLE001
        # Алерт — best-effort: сбой пуша НЕ должен рушить пересчёт health.
        logger.exception(
            "health: не удалось поставить админ-алерт о деградации ноды %s", node.id
        )


def migrate_subscriptions_off(
    db: Session,
    node: models.VPNNode,
    *,
    reason: str,
    exclude_same_region: bool = False,
) -> dict:
    """Move all active subscriptions off ``node`` to a healthy alternative.

    Returns a dict with the migrated subscription IDs, the per-target
    resync task IDs, and the per-device provisioning task IDs. Callers
    that only care about the migrated count (legacy health-monitor
    path) can read ``["subscription_ids"]``; the admin migrate route
    surfaces ``task_ids`` so the UI can render a grouped progress
    banner for the batch.

    ``considered_count`` is the number of active subs we tried to move;
    ``no_target_count`` is how many of those ``choose_node`` couldn't
    find a destination for (every other node in cooldown / unhealthy /
    out of the plan's pool). The admin UI reads these to distinguish
    "no active subs on this node" from "had subs but nowhere to send
    them" — previously both produced an empty ``subscription_ids`` and
    the UI showed the same misleading "нечего мигрировать" toast.
    """
    from .provisioning import (
        ProvisioningOrchestrator,
        _device_vless_uuid,
        _node_has_vless_family,
        choose_node,
    )

    subs: list[models.Subscription] = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.node_id == node.id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .all()
    )
    considered_count = len(subs)
    no_target_count = 0
    migrated_ids: list[int] = []
    orchestrator = ProvisioningOrchestrator(db)
    # Unique target nodes this batch migrated to — we run a single
    # resync per target at the end. Per-sub resync would be O(subs²)
    # and redundant: manage_vless_user.sh is idempotent and the resync
    # reads the full active-subs list from the DB, not the per-sub
    # delta, so one call after the whole batch is exactly enough.
    # Also tracks the provisioning task IDs we create so the admin UI
    # can show a progress banner grouped by this migration run.
    resync_targets: dict[int, models.VPNNode] = {}
    device_task_ids: list[int] = []
    revoke_task_ids: list[int] = []

    for sub in subs:
        try:
            target = choose_node(
                db,
                sub.plan,
                exclude_node_ids=[node.id],
                exclude_regions=[node.region] if exclude_same_region else None,
            )
        except RuntimeError as exc:
            logger.error("No healthy node available for sub %s: %s", sub.id, exc)
            no_target_count += 1
            continue

        # Snapshot every live device's identity BEFORE revoke so the
        # reprovision loop on the target can preserve per-user URIs
        # byte-for-byte. Without this two regressions come back:
        #   - N-device subs collapse to a single "primary" on the new
        #     node because we only called reprovision once per sub.
        #   - Every device's vless:// URI rerolls (fresh UUID + fresh
        #     sub_token) and the clients users have installed start
        #     refetching their subscription file on every migrate.
        # See ``migrate_subscription_to_new_node`` for the full rationale
        # on the snapshot/NULL/reuse dance — this block mirrors it.
        live_devices_snapshot = [
            d
            for d in list(sub.devices)
            if d.status
            not in (models.DeviceStatus.disabled, models.DeviceStatus.revoked)
        ]
        live_names: list[str] = []
        reuse_map: dict[str, tuple[str | None, str | None, str | None]] = {}
        for d in live_devices_snapshot:
            name_key = d.name or "primary"
            live_names.append(name_key)
            if name_key not in reuse_map:
                reuse_map[name_key] = (
                    d.sub_token,
                    d.connection_uri,
                    _device_vless_uuid(d),
                )

        # id96: отзыв старых девайсов выполняется ПОСЛЕ успешного репровижена
        # на целевой ноде (см. success-ветку ниже), а не до. Иначе при сбое
        # reprovision девайсы уже отозваны, и повторный запуск миграции
        # получает пустой live_devices_snapshot — мульти-девайсная сабка
        # схлопнулась бы в один свежий primary без reuse токенов. Список
        # фиксируем сейчас: после reprovision в sub.devices появятся НОВЫЕ
        # девайсы целевой ноды, их отзывать нельзя.
        old_devices = list(sub.devices)

        # Free the unique(sub_token) slot on every just-revoked device
        # whose token we intend to reuse on the target node — without
        # NULL-ing here the reprovision INSERT trips the unique
        # constraint (two rows can't share the same non-null sub_token).
        # client_id_hmac ПРОИЗВОДНЫЙ от sub_token (compute_client_id_hmac) и
        # тоже UNIQUE — сбрасываем ВМЕСТЕ, иначе reprovision INSERT нового
        # Device с тем же reuse_sub_token упрётся в ix_devices_client_id_hmac
        # (IntegrityError). Раньше его не чистили → IntegrityError ловился
        # ниже без rollback → сессия «отравлена» → следующий commit кидал
        # PendingRollbackError → весь /migrate падал в 500 (см.
        # migrate_device_to_node, где это уже сделано правильно).
        #
        # Весь flip обёрнут в try: одиночный сбой на flush/commit НЕ должен
        # ронять всю пачку — откатываем сессию, логируем, пропускаем sub
        # (остаётся на старой ноде с живыми девайсами — отзыв перенесён
        # ниже, — так что ретрай миграции реально возможен), вместо
        # отравленной сессии.
        try:
            reused_tokens = {t for t, _, _ in reuse_map.values() if t}
            # id96: бэкап оригинальных токенов по id девайса. NULL-инг ниже
            # коммитится вместе с флипом node_id ДО репровижена, и если
            # reprovision дальше упадёт, db.rollback() уже закоммиченное не
            # откатит — токен не носила бы ни одна строка Device и
            # /api/sub/{token} умер бы навсегда (ломая инвариант «sub_token
            # никогда не мутируется»). По этому бэкапу компенсирующий блок в
            # except репровижена возвращает не перенесённые токены на
            # исходные строки.
            token_backup: dict[int, tuple[str, str | None]] = {}
            if reused_tokens:
                for d in live_devices_snapshot:
                    if d.sub_token and d.sub_token in reused_tokens:
                        token_backup[d.id] = (d.sub_token, d.client_id_hmac)
                        d.sub_token = None
                        d.client_id_hmac = None
                db.flush()

            # In-place migration: flip node_id on the existing Subscription
            # row and reprovision. This preserves sub_token (dynamic sub-link
            # keeps working) and avoids the "two cards in webapp" UX bug where
            # the old blocked row and new active row both showed up.
            sub.node_id = target.id
            sub.notes = f"migrated: {reason}"
            db.add(sub)
            db.commit()
            db.refresh(sub)
        except Exception:  # noqa: BLE001
            logger.exception("migrate: flip node_id failed for sub %s", sub.id)
            db.rollback()
            no_target_count += 1
            continue

        if not live_names:
            # Sub had zero live devices — still create one so /sub/{token}
            # aliasing on old revoked rows has a sibling to resolve.
            live_names = ["primary"]
        sub_succeeded = False
        # id96: токены, которые реально переехали на новые девайсы целевой
        # ноды, — их восстанавливать на старых строках нельзя (носитель уже
        # есть, unique(sub_token) не пустит вторую строку).
        consumed_tokens: set[str] = set()
        try:
            for name in live_names:
                reuse_token, reuse_uri, reuse_uuid = reuse_map.get(
                    name, (None, None, None)
                )
                _device, _task = orchestrator.reprovision_subscription(
                    sub,
                    device_name=name,
                    reuse_sub_token=reuse_token,
                    reuse_connection_uri=reuse_uri,
                    reuse_uuid=reuse_uuid,
                )
                if _task is not None:
                    device_task_ids.append(_task.id)
                if reuse_token:
                    consumed_tokens.add(reuse_token)
                sub_succeeded = True
            if sub_succeeded:
                # Revoke the old devices on the (possibly already-dead) node.
                # Best effort — if the node is unreachable Ansible will fail,
                # that's okay. id96: отзываем только после того, как новые
                # девайсы с reuse-токенами созданы на целевой ноде.
                for device in old_devices:
                    try:
                        revoke_task = orchestrator.revoke_device(
                            device, reason=reason, background=True
                        )
                        revoke_task_ids.append(revoke_task.id)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to revoke device %s during migration", device.id
                        )
                migrated_ids.append(sub.id)
                resync_targets[target.id] = target
                # Notify the user. The bot polls /notifications/pending for
                # audit_log rows with action="migration_notice" and delivers
                # them as Telegram messages (see api_extensions.py). Without
                # telegram_id in extra the poller silently drops the row, so
                # skip the write for non-Telegram users (e.g. email-only).
                if sub.user and sub.user.telegram_id and sub.user.notify_migrations:
                    db.add(
                        models.AuditLog(
                            actor="health_monitor",
                            actor_type=models.AuditActor.system,
                            action="migration_notice",
                            target_type="subscription",
                            target_id=sub.id,
                            extra={
                                "telegram_id": sub.user.telegram_id,
                                "old_node": node.name,
                                "new_node": target.name,
                                "reason": reason,
                            },
                        )
                    )
                    db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to re-provision sub %s on node %s", sub.id, target.id)
            # Un-poison the session so the next sub's commit (and the
            # end-of-batch resync) don't inherit a rolled-back transaction.
            db.rollback()
            # id96 компенсация: NULL-инг sub_token/client_id_hmac уже был
            # закоммичен вместе с флипом node_id, rollback его не вернул.
            # Возвращаем токены, которые НЕ переехали на новый девайс, на их
            # исходные строки — /api/sub/{token} продолжает резолвиться
            # (напрямую или через sibling-alias), инвариант «sub_token
            # никогда не мутируется» сохраняется, а старые девайсы не
            # отозваны (см. выше), так что ретрай миграции возможен.
            try:
                for dev_id, (tok, tok_hmac) in token_backup.items():
                    if tok in consumed_tokens:
                        continue
                    carrier = (
                        db.query(models.Device.id)
                        .filter(models.Device.sub_token == tok)
                        .first()
                    )
                    if carrier is not None:
                        # reprovision мог закоммитить новый девайс и упасть
                        # уже после (refresh/attach) — токен носится, не трогаем.
                        continue
                    db.query(models.Device).filter(
                        models.Device.id == dev_id
                    ).update(
                        {"sub_token": tok, "client_id_hmac": tok_hmac},
                        synchronize_session=False,
                    )
                db.commit()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "migrate: token restore failed for sub %s", sub.id
                )
                db.rollback()

    # Resync every target node the batch touched. This covers the
    # "user migrated but still gets `invalid request user id`" case:
    # the per-device apply task *does* add the user, but if the target
    # node's config.json is in a degraded state (partial wipe, stale
    # cache, race with another provisioning task on the same node) the
    # resync re-pushes the full authoritative client list via
    # manage_vless_*_user.sh. Covers every vless-family protocol
    # (reality / xhttp / ws_cdn) in one shot. Idempotent, so re-running
    # after the individual applies is safe and costs one extra ansible
    # run per target node — cheap vs. hunting down flakiness.
    resync_task_ids: list[int] = []
    for target_node in resync_targets.values():
        try:
            if not _node_has_vless_family(target_node):
                continue
            resync_task = orchestrator.resync_node_clients(target_node)
            if resync_task is not None:
                resync_task_ids.append(resync_task.id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Post-migration resync failed for target node %s", target_node.id
            )

    return {
        "subscription_ids": migrated_ids,
        "revoke_task_ids": revoke_task_ids,
        "device_task_ids": device_task_ids,
        "resync_task_ids": resync_task_ids,
        "considered_count": considered_count,
        "no_target_count": no_target_count,
    }
