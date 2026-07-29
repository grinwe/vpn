"""Subscription and device admin endpoints.

Covers ``/api/subscriptions/*``, ``/api/devices/*`` and the admin
disable/enable/add-device flows. The webapp-facing subscription routes
live in ``api_webapp.py`` instead — this module is admin-token gated,
those are webapp-JWT gated.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import optional_admin as optional_admin_token
from ..auth import require_admin
from ..security import decrypt as _decrypt
from ..services.provisioning import ProvisioningOrchestrator
from ..time_utils import utcnow
from ._common import (
    ADMIN_ACTOR_HEADER,
    _audit,
    _create_subscription_for_user,
    _get_or_create_user,
    _resolve_admin_actor,
    get_db,
    logger,
)

router = APIRouter()


def _subscription_provision_response(
    subscription: models.Subscription, task: models.ProvisioningTask
) -> schemas.SubscriptionProvisionResponse:
    device = subscription.devices[0] if subscription.devices else None
    if not device:
        raise HTTPException(status_code=500, detail="Subscription has no device")

    return schemas.SubscriptionProvisionResponse(
        subscription_id=subscription.id,
        status=subscription.status.value,
        expires_at=subscription.expires_at,
        node_id=subscription.node_id,
        plan_id=subscription.plan_id,
        provisioning_task_id=task.id,
        device=schemas.DeviceStatusOut(
            id=device.id,
            name=device.name,
            status=device.status.value,
            config_id=device.config_id,
            access_username=device.access_username,
            connection_uri=_decrypt(device.connection_uri),
            credentials=[schemas.CredentialOut.from_orm(c) for c in device.credentials],
            provisioning_task_id=task.id,
        ),
    )


# ── Mass actions over a set of users ─────────────────────────────────
#
# Both bulk endpoints fire ansible per device (regenerate: 1 apply per
# device; migrate: revoke+apply per device) and drain through the single
# serial RQ worker. The 2026-04/05 incidents were exactly an ansible
# backlog thrashing one xray node, so the per-request user cap is kept
# deliberately LOW (batch_ban's 500 is fine for a pure-DB loop, NOT for
# this). The admin UI chunks larger selections; bump worker replicas
# (scripts/workers.sh) before big runs.
_BULK_USERS_MAX = 25


class BulkUserIdsRequest(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=_BULK_USERS_MAX)
    # Honored ONLY by bulk-regenerate-sublink: when False, the "grab your
    # new link" Telegram nudge is suppressed — for operator-driven mass
    # runs where the old link auto-heals and users shouldn't be spammed.
    # The other bulk ops (migrate-auto, rebuild-config) never notify and
    # ignore this field.
    notify: bool = True


def _notify_sublink_rotated(db: Session, user: models.User) -> bool:
    """Queue a "grab your new link from the ЛК" Telegram nudge.

    The bot polls ``/api/notifications/pending`` for system AuditLog rows
    with action ``sublink_rotated``; the row is silently dropped without
    ``extra.telegram_id`` — so email-only users get nothing (reported as
    "not notified" in the bulk summary). Honors the per-user
    ``notify_migrations`` opt-out, same as health-driven migration notices.
    """
    if not (user.telegram_id and user.notify_migrations):
        return False
    db.add(
        models.AuditLog(
            actor="admin_regen",
            actor_type=models.AuditActor.system,
            action="sublink_rotated",
            target_type="user",
            target_id=user.id,
            extra={"telegram_id": user.telegram_id},
        )
    )
    # Commit the nudge immediately so a LATER user's failure (which rolls
    # the session back) can never strand an already-completed user's
    # notification while their new link is durably committed.
    db.commit()
    return True


@router.post("/subscriptions/bulk-regenerate-sublink")
def bulk_regenerate_sublink(
    body: BulkUserIdsRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Mass «перегенерировать sub-link» over a set of users.

    For every ACTIVE subscription of each user, regenerate the sub-link
    (fresh token+UUID+creds on the SAME node, old link kept alive — see
    ``ProvisioningOrchestrator.regenerate_subscription_sublink``) and send
    the user a Telegram nudge to grab the new link from their ЛК.

    This is NOT a server move — for that use ``/bulk-migrate-auto``. The
    ``sub_token`` CHANGES (a new URL appears in the ЛК), but the monthly
    cost is unchanged: ``extra_device_slots`` is never touched and the
    live device count is preserved 1:1. Old devices stay on the node so
    the user keeps connecting until they pick up the new link.
    """
    actor, actor_type = _resolve_admin_actor(admin_actor)
    orchestrator = ProvisioningOrchestrator(db)

    users = (
        db.query(models.User).filter(models.User.id.in_(body.user_ids)).all()
    )
    found = {u.id for u in users}
    not_found = [uid for uid in body.user_ids if uid not in found]

    done: list[int] = []
    skipped: list[int] = []
    notified: list[int] = []
    failed: list[dict] = []
    subs_done = 0
    devices_created = 0

    for user in users:
        active_subs = (
            db.query(models.Subscription)
            .filter(
                models.Subscription.user_id == user.id,
                models.Subscription.status == models.SubscriptionStatus.active,
            )
            .all()
        )
        if not active_subs:
            skipped.append(user.id)
            continue
        any_ok = False
        for sub in active_subs:
            try:
                created = orchestrator.regenerate_subscription_sublink(sub)
            except Exception as exc:  # noqa: BLE001
                # Batch resilience: one bad sub must never abort the whole
                # run. Roll back its partial state so the session is clean
                # for the next sub, and surface it for the operator.
                db.rollback()
                # str(exc) в ответе эфемерен — трейсбек в лог, иначе разбор
                # «почему не перегенерировалось» упрётся в невоспроизводимость.
                logger.exception(
                    "bulk-regenerate-sublink: user %s sub %s failed",
                    user.id,
                    sub.id,
                )
                failed.append(
                    {"user_id": user.id, "subscription_id": sub.id, "error": str(exc)}
                )
                continue
            any_ok = True
            subs_done += 1
            devices_created += len(created)
            _audit(
                db,
                actor,
                "sublink_regenerated",
                "subscription",
                sub.id,
                actor_type=actor_type,
                metadata={"devices_created": len(created)},
            )
        if any_ok:
            done.append(user.id)
            if body.notify and _notify_sublink_rotated(db, user):
                notified.append(user.id)

    db.commit()
    return {
        "done": done,
        "skipped": skipped,
        "not_found": not_found,
        "failed": failed,
        "notified": notified,
        "subscriptions_regenerated": subs_done,
        "devices_created": devices_created,
    }


@router.post("/subscriptions/{subscription_id}/rebuild-config")
def rebuild_subscription_config(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Пересобрать config_text подписки из ТЕКУЩЕГО VPNConfig — тихо.

    Без ротации sub_token, без нового устройства, без ansible, без пуша.
    Лечит расхождение «исправили VPNConfig в БД (напр. xhttp sni/port
    после DR), а сабка отдаёт старый config_text»: URI пересобирается из
    актуального cfg тем же UUID, старая ссылка юзера сама подтянет
    исправленный конфиг на следующем рефреше.
    """
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    actor, actor_type = _resolve_admin_actor(admin_actor)
    orchestrator = ProvisioningOrchestrator(db)
    try:
        rebuilt = orchestrator.rebuild_subscription_config_text(sub)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        db,
        actor,
        "sublink_config_rebuilt",
        "subscription",
        sub.id,
        actor_type=actor_type,
        metadata={"credentials_rebuilt": rebuilt},
    )
    db.commit()
    return {"subscription_id": sub.id, "credentials_rebuilt": rebuilt}


@router.post("/subscriptions/bulk-rebuild-config")
def bulk_rebuild_config(
    body: BulkUserIdsRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Массовый ТИХИЙ rebuild config_text по набору юзеров.

    Для каждой активной подписки пересобирает config_text из текущего
    VPNConfig (см. ``rebuild_subscription_config_text``). Никаких пушей,
    ротаций токена, новых устройств, ansible — чистая починка вшитых URI
    после правки конфигов. ``notify`` в боди игнорируется.
    """
    actor, actor_type = _resolve_admin_actor(admin_actor)
    orchestrator = ProvisioningOrchestrator(db)

    users = (
        db.query(models.User).filter(models.User.id.in_(body.user_ids)).all()
    )
    found = {u.id for u in users}
    not_found = [uid for uid in body.user_ids if uid not in found]

    done: list[int] = []
    skipped: list[int] = []
    failed: list[dict] = []
    creds_rebuilt = 0

    for user in users:
        active_subs = (
            db.query(models.Subscription)
            .filter(
                models.Subscription.user_id == user.id,
                models.Subscription.status == models.SubscriptionStatus.active,
            )
            .all()
        )
        if not active_subs:
            skipped.append(user.id)
            continue
        any_ok = False
        for sub in active_subs:
            try:
                n = orchestrator.rebuild_subscription_config_text(sub)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                # Трейсбек в лог — str(exc) в ответе не переживёт закрытия админки.
                logger.exception(
                    "bulk-rebuild-config: user %s sub %s failed",
                    user.id,
                    sub.id,
                )
                failed.append(
                    {"user_id": user.id, "subscription_id": sub.id, "error": str(exc)}
                )
                continue
            any_ok = True
            creds_rebuilt += n
            _audit(
                db,
                actor,
                "sublink_config_rebuilt",
                "subscription",
                sub.id,
                actor_type=actor_type,
                metadata={"credentials_rebuilt": n},
            )
        if any_ok:
            done.append(user.id)

    db.commit()
    return {
        "done": done,
        "skipped": skipped,
        "not_found": not_found,
        "failed": failed,
        "credentials_rebuilt": creds_rebuilt,
    }


@router.post("/subscriptions/bulk-migrate-auto")
def bulk_migrate_auto(
    body: BulkUserIdsRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Mass «переехать на другой сервер» over a set of users.

    For every ACTIVE subscription of each user, auto-pick a free healthy
    node (excluding the current one + the user's node ban-list), migrate,
    and auto-ban the old node — the bulk version of the per-card
    ``/migrate-auto``. The ``sub_token`` is PRESERVED (sub-link invariant):
    the client's URL keeps working, only the server changes, so NO user
    notification is sent (the profile auto-updates via the sibling-alias).
    """
    actor, actor_type = _resolve_admin_actor(admin_actor)
    orchestrator = ProvisioningOrchestrator(db)

    users = (
        db.query(models.User).filter(models.User.id.in_(body.user_ids)).all()
    )
    found = {u.id for u in users}
    not_found = [uid for uid in body.user_ids if uid not in found]

    done: list[int] = []
    skipped: list[int] = []
    failed: list[dict] = []
    subs_migrated = 0

    for user in users:
        active_subs = (
            db.query(models.Subscription)
            .filter(
                models.Subscription.user_id == user.id,
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Subscription.node_id.isnot(None),
            )
            .all()
        )
        if not active_subs:
            skipped.append(user.id)
            continue
        any_ok = False
        for sub in active_subs:
            old_node = sub.node
            try:
                new_node, _dev, task, banned_old = (
                    orchestrator.migrate_subscription_to_free_node(sub, banned_by=actor)
                )
            except Exception as exc:  # noqa: BLE001
                # RuntimeError = no free node (pool empty / all unhealthy /
                # all banned). Broadened to Exception for batch resilience:
                # a DB/transient error on one sub must not abort the run.
                # Roll back so the session is clean for the next sub.
                db.rollback()
                # Трейсбек в лог: str(exc) без него часто не даёт понять место
                # падения (напр. SQLAlchemy-ошибка), а ответ API эфемерен.
                logger.exception(
                    "bulk-migrate-auto: user %s sub %s failed",
                    user.id,
                    sub.id,
                )
                failed.append(
                    {"user_id": user.id, "subscription_id": sub.id, "error": str(exc)}
                )
                continue
            any_ok = True
            subs_migrated += 1
            _audit(
                db,
                actor,
                "subscription_migrated",
                "subscription",
                sub.id,
                actor_type=actor_type,
                metadata={
                    "old_node_id": old_node.id if old_node else None,
                    "new_node_id": new_node.id,
                    "banned_old_node": banned_old,
                    "reason": "bulk auto free-server",
                },
            )
        if any_ok:
            done.append(user.id)

    db.commit()
    return {
        "done": done,
        "skipped": skipped,
        "not_found": not_found,
        "failed": failed,
        "subscriptions_migrated": subs_migrated,
    }


@router.post("/subscriptions", response_model=schemas.SubscriptionProvisionResponse)
def create_subscription(
    payload: schemas.SubscriptionCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    plan = db.get(models.Plan, payload.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    user = _get_or_create_user(db, payload.telegram_id, payload.email)
    sub, task = _create_subscription_for_user(
        db, user, plan, node_id=payload.node_id, device_name=payload.device_name
    )
    return _subscription_provision_response(sub, task)


@router.get("/devices/{device_id}", response_model=schemas.DeviceStatusOut)
def get_device(
    device_id: int,
    telegram_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin_token),
):
    device = db.get(models.Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Ownership check: must be admin or provide matching telegram_id
    if not admin_token:
        if not telegram_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        owner = device.subscription.user if device.subscription else None
        if not owner or owner.telegram_id != telegram_id:
            raise HTTPException(status_code=403, detail="Forbidden")

    task = (
        db.query(models.ProvisioningTask)
        .filter(
            models.ProvisioningTask.target_type == "device",
            models.ProvisioningTask.target_id == device_id,
        )
        .order_by(models.ProvisioningTask.created_at.desc())
        .first()
    )
    return schemas.DeviceStatusOut(
        id=device.id,
        name=device.name,
        status=device.status.value,
        config_id=device.config_id,
        access_username=device.access_username,
        connection_uri=_decrypt(device.connection_uri),
        credentials=[schemas.CredentialOut.from_orm(c) for c in device.credentials],
        provisioning_task_id=task.id if task else None,
    )


@router.post("/devices/{device_id}/revoke", response_model=schemas.ProvisioningTaskOut)
def revoke_device(
    device_id: int,
    body: schemas.DisableRequest | None = None,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    device = db.get(models.Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.revoke_device(device, reason=body.reason if body else None)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "device_revoked",
        "device",
        device_id,
        metadata={"reason": body.reason if body else None},
        actor_type=actor_type,
    )
    db.refresh(task)
    return schemas.ProvisioningTaskOut.from_orm(task)


@router.post("/subscriptions/{subscription_id}/disable")
def disable_subscription(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(status_code=400, detail="Subscription is not active")

    orchestrator = ProvisioningOrchestrator(db)
    tasks = orchestrator.revoke_subscription_devices(sub, reason="disabled by admin")
    # NB: we do NOT refund any charged plan price on disable — the
    # sub's ``expires_at`` stays intact so an /enable call resumes from
    # where it left off. A separate /cancel action (refund + terminate)
    # can be added later; this keeps disable ↔ enable symmetric.
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "subscription_disabled", "subscription", subscription_id, actor_type=actor_type)
    return {
        "subscription_id": sub.id,
        "status": sub.status.value,
        "revocation_tasks": [task.id for task in tasks],
    }


@router.post(
    "/subscriptions/{subscription_id}/devices",
    response_model=schemas.ProvisioningTaskOut,
)
def admin_add_device(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Admin counterpart to webapp add-device — bypasses the paid-slot flow.

    The webapp endpoint charges a pro-rated
    ``EXTRA_DEVICE_MONTHLY_KOPECKS`` from the wallet when the new
    device exceeds ``plan.max_devices + sub.extra_device_slots`` and
    persists the bought slot on ``sub.extra_device_slots`` so every
    subsequent renewal bills for it (see
    [WEBAPP_REFERENCE.md](../../docs/WEBAPP_REFERENCE.md#add-device)).

    Admin skips **both**: no wallet debit, and ``extra_device_slots``
    is NOT bumped. That means the admin-added device is effectively
    a permanent freebie — renewals will only bill for slots the user
    bought themselves via the webapp. Useful for re-binding a device
    revoked by mistake, or giving a friend a slot on top of a plan
    without touching billing. If you want the user to be billed for
    an admin-added extra, also bump ``sub.extra_device_slots`` by
    hand (or top up the user's wallet to match).
    """
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(
            status_code=400,
            detail=f"Subscription is {sub.status.value}, must be active",
        )

    current = (
        db.query(models.Device)
        .filter(
            models.Device.subscription_id == sub.id,
            models.Device.status.notin_(
                [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
            ),
        )
        .count()
    )
    new_count = current + 1
    orchestrator = ProvisioningOrchestrator(db)
    try:
        _device, task = orchestrator.reprovision_subscription(
            sub, device_name=f"device-{new_count}"
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "admin_device_added",
        "subscription",
        sub.id,
        actor_type=actor_type,
        metadata={"new_device_count": new_count},
    )
    db.refresh(task)
    return schemas.ProvisioningTaskOut.from_orm(task)


@router.post("/subscriptions/{subscription_id}/enable")
def enable_subscription(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Resume a non-active subscription.

    * ``frozen`` → routes through ``balance.unfreeze_subscription`` so
      the year-budget accounting runs and the original sub_token is
      preserved.
    * ``blocked`` / ``expired`` → flipped back to ``active`` and
      reprovisioned. Caller is responsible for having topped up the
      balance first — we don't gate on it, just resume.

    For a LAPSED term (``expires_at`` in the past — i.e. ``expired``, or a
    ``blocked`` sub that also outlived its term) we bump ``expires_at`` by the
    plan's ``duration_days``. Without it the expiry tick would re-flag the sub
    ``expired`` on its next run and the resume would silently bounce back.
    """
    from ..services import balance as balance_svc

    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status == models.SubscriptionStatus.active:
        raise HTTPException(status_code=400, detail="Subscription is already active")

    reprovision_ok = True
    if sub.status == models.SubscriptionStatus.frozen:
        try:
            balance_svc.unfreeze_subscription(db, sub, auto=False)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        now = utcnow()
        # Истёкший срок продлеваем на срок плана — иначе expiry-тик worker'а
        # снова пометит sub `expired` и резюм отвалится. blocked с ещё живым
        # сроком не трогаем (не дарим лишних дней).
        if sub.expires_at is None or sub.expires_at < now:
            days = sub.plan.duration_days if sub.plan else 30
            sub.expires_at = now + timedelta(days=days)
        sub.status = models.SubscriptionStatus.active
        sub.notes = None
        sub.next_charge_at = now
        db.add(sub)
        db.flush()
        orchestrator = ProvisioningOrchestrator(db)
        try:
            orchestrator.reprovision_subscription(sub)
        except Exception:
            logger.exception(
                "enable_subscription: reprovision failed sub=%s — left active w/o device",
                sub.id,
            )
            # Репровижининг упал: подписка active, но рабочего конфига нет —
            # прокидываем факт сбоя в ответ, чтобы админка показала оператору,
            # а не молча отрапортовала успех (иначе sub-link отдаст 503).
            reprovision_ok = False

    db.commit()
    db.refresh(sub)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "subscription_enabled",
        "subscription",
        subscription_id,
        actor_type=actor_type,
    )
    return {
        "subscription_id": sub.id,
        "status": sub.status.value,
        "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
        "reprovision_failed": not reprovision_ok,
    }


@router.post(
    "/subscriptions/{subscription_id}/migrate",
    response_model=schemas.SubscriptionMigrateOut,
)
def migrate_subscription(
    subscription_id: int,
    payload: schemas.SubscriptionMigrateIn,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Admin override: move one subscription to a specific target node.

    Per-sub counterpart to ``POST /nodes/{id}/migrate`` (which drains a
    whole node). Bypasses pool/health/capacity/cooldown filters — only
    ``is_active=True`` is enforced on the target. Intended for surgical
    admin fixes ("move user X off node Y right now") after e.g. a
    partial outage or a user-specific complaint.

    ``sub_token`` is preserved so the client's sub-link URL keeps
    working. Old devices are revoked in background, the new device is
    provisioned via the normal reprovision path.
    """
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(
            status_code=400,
            detail=f"Subscription is {sub.status.value}, must be active",
        )
    old_node = sub.node
    if old_node is None:
        raise HTTPException(status_code=400, detail="Subscription has no node")
    if payload.target_node_id == sub.node_id:
        raise HTTPException(
            status_code=400, detail="target_node_id matches current node"
        )

    orchestrator = ProvisioningOrchestrator(db)
    try:
        new_node, _device, task = orchestrator.migrate_subscription_to_new_node(
            sub, target_node_id=payload.target_node_id
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "subscription_migrated",
        "subscription",
        sub.id,
        actor_type=actor_type,
        metadata={
            "old_node_id": old_node.id,
            "old_node_name": old_node.name,
            "new_node_id": new_node.id,
            "new_node_name": new_node.name,
            "reason": "admin override",
        },
    )
    db.commit()
    return schemas.SubscriptionMigrateOut(
        subscription_id=sub.id,
        old_node_id=old_node.id,
        old_node_name=old_node.name,
        new_node_id=new_node.id,
        new_node_name=new_node.name,
        provisioning_task_id=task.id if task else None,
    )


@router.post(
    "/subscriptions/{subscription_id}/migrate-auto",
    response_model=schemas.SubscriptionMigrateOut,
)
def migrate_subscription_auto(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """«Обновить подписку»: авто-выбор свободного сервера + миграция.

    В отличие от ``/migrate`` (админ задаёт target вручную), здесь
    ``choose_node`` сам берёт наименее загруженную здоровую ноду пула,
    исключая текущую И ноды из бан-листа юзера (``NodeUserBan``). Старая
    нода авто-банится для этого юзера, чтобы повторное «обновление» не
    вернуло его обратно. ``sub_token`` сохраняется (инвариант sub-link).

    Бесплатно, без подтверждения target — кнопка «дай другой сервер».
    Тот же путь в будущем дёргает ЛК юзера (``api_webapp``).
    """
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(
            status_code=400,
            detail=f"Subscription is {sub.status.value}, must be active",
        )
    old_node = sub.node
    if old_node is None:
        raise HTTPException(status_code=400, detail="Subscription has no node")

    actor, actor_type = _resolve_admin_actor(admin_actor)
    orchestrator = ProvisioningOrchestrator(db)
    try:
        new_node, _device, task, banned_old = (
            orchestrator.migrate_subscription_to_free_node(sub, banned_by=actor)
        )
    except RuntimeError as exc:
        # Свободной ноды нет: пул пуст / все нездоровы / все в бан-листе.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _audit(
        db,
        actor,
        "subscription_migrated",
        "subscription",
        sub.id,
        actor_type=actor_type,
        metadata={
            "old_node_id": old_node.id,
            "old_node_name": old_node.name,
            "new_node_id": new_node.id,
            "new_node_name": new_node.name,
            "banned_old_node": banned_old,
            "reason": "auto free-server (обновление подписки)",
        },
    )
    db.commit()
    return schemas.SubscriptionMigrateOut(
        subscription_id=sub.id,
        old_node_id=old_node.id,
        old_node_name=old_node.name,
        new_node_id=new_node.id,
        new_node_name=new_node.name,
        provisioning_task_id=task.id if task else None,
        banned_old_node=banned_old,
    )


@router.post(
    "/subscriptions/{subscription_id}/switch-exit",
    response_model=schemas.SubscriptionSwitchExitOut,
)
def switch_subscription_exit(
    subscription_id: int,
    payload: schemas.SubscriptionSwitchExitIn,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Re-pin a subscription's creds to a different exit on the same relay.

    Cheaper than a node migration — the user stays on the same VLESS
    UUID + sub_token; only the relay's xray routing rule changes to
    point their email at a different ``direct-wgN`` outbound. Works
    only on multi-link relays where the target exit is already
    attached (via ``RelayExitLink``); attaching a new exit first is a
    separate admin action.
    """
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status not in (
        models.SubscriptionStatus.active,
        models.SubscriptionStatus.frozen,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Subscription is {sub.status.value}, must be active or frozen",
        )
    node = sub.node
    if node is None:
        raise HTTPException(status_code=400, detail="Subscription has no node")

    old_exit_id: int | None = None
    first_cred = (
        db.query(models.Credential)
        .filter(
            models.Credential.subscription_id == sub.id,
            models.Credential.is_active.is_(True),
        )
        .first()
    )
    if first_cred is not None:
        old_exit_id = first_cred.exit_id
    if old_exit_id == payload.exit_id:
        raise HTTPException(
            status_code=400,
            detail="Subscription is already routed through this exit",
        )

    orchestrator = ProvisioningOrchestrator(db)
    try:
        tasks = orchestrator.switch_subscription_exit(sub, payload.exit_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    link = (
        db.query(models.RelayExitLink)
        .filter(
            models.RelayExitLink.relay_node_id == node.id,
            models.RelayExitLink.exit_id == payload.exit_id,
        )
        .first()
    )
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "subscription_exit_switched",
        "subscription",
        sub.id,
        actor_type=actor_type,
        metadata={
            "relay_node_id": node.id,
            "old_exit_id": old_exit_id,
            "new_exit_id": payload.exit_id,
            "new_interface": link.wg_interface_name if link else None,
            "task_count": len(tasks),
        },
    )
    db.commit()
    return schemas.SubscriptionSwitchExitOut(
        subscription_id=sub.id,
        old_exit_id=old_exit_id,
        new_exit_id=payload.exit_id,
        new_interface=link.wg_interface_name if link else "",
        task_ids=[t.id for t in tasks],
    )


@router.post(
    "/devices/{device_id}/migrate",
    response_model=schemas.DeviceMigrateOut,
)
def migrate_device(
    device_id: int,
    payload: schemas.DeviceMigrateIn,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Admin override: relocate ONE device to a target node.

    Per-device counterpart to ``POST /subscriptions/{id}/migrate``.
    Leaves ``subscription.node_id`` on the old node — the sub becomes
    "split" across nodes (future add_device defaults back to sub.node).
    Use for surgical fixes ("user says only their phone is slow").
    """
    device = db.get(models.Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    old_node = device.config.node if device.config else (
        device.subscription.node if device.subscription else None
    )
    if old_node is None:
        raise HTTPException(status_code=400, detail="Device has no node")
    if payload.target_node_id == old_node.id:
        raise HTTPException(
            status_code=400, detail="target_node_id matches device's current node"
        )

    orchestrator = ProvisioningOrchestrator(db)
    try:
        new_node, new_device, task = orchestrator.migrate_device_to_node(
            device, target_node_id=payload.target_node_id
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "device_migrated",
        "device",
        new_device.id,
        actor_type=actor_type,
        metadata={
            "old_device_id": device.id,
            "old_node_id": old_node.id,
            "old_node_name": old_node.name,
            "new_node_id": new_node.id,
            "new_node_name": new_node.name,
            "subscription_id": device.subscription_id,
        },
    )
    db.commit()
    return schemas.DeviceMigrateOut(
        old_device_id=device.id,
        device_id=new_device.id,
        old_node_id=old_node.id,
        old_node_name=old_node.name,
        new_node_id=new_node.id,
        new_node_name=new_node.name,
        provisioning_task_id=task.id if task else None,
    )


def _device_node_set(db: Session, device: models.Device) -> list[dict]:
    """Набор нод, на которых у device есть АКТИВНЫЕ creds (диверсная подписка):
    [{node_id, name, region, status, protocols:[...]}]. Для админки — «на каких
    нодах сидит юзер»."""
    by_node: dict[int, set[str]] = {}
    for c in device.credentials:
        if c.is_active and c.node_id:
            by_node.setdefault(c.node_id, set()).add(c.proto)
    if not by_node:
        return []
    rows = (
        db.query(
            models.VPNNode.id, models.VPNNode.name,
            models.VPNNode.region, models.VPNNode.status,
        )
        .filter(models.VPNNode.id.in_(list(by_node)))
        .all()
    )
    info = {r[0]: r for r in rows}
    out = []
    for nid, protos in by_node.items():
        r = info.get(nid)
        out.append({
            "node_id": nid,
            "name": r[1] if r else None,
            "region": r[2] if r else None,
            "status": (r[3].value if r and hasattr(r[3], "value") else None),
            "protocols": sorted(protos),
        })
    return sorted(out, key=lambda x: x["node_id"])


@router.get("/devices/{device_id}/nodes")
def get_device_nodes(
    device_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Набор нод диверсной подписки device (на каких нодах сидит юзер)."""
    device = db.get(models.Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"device_id": device_id, "nodes": _device_node_set(db, device)}


@router.post("/devices/{device_id}/nodes/{node_id}/swap")
def swap_device_node(
    device_id: int,
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Diverse-rotation: убрать ноду ``node_id`` из набора device и добрать свежую
    диверсную взамен (sub_token не меняется). Это «миграция» для диверс-подписок —
    меняет одну ноду, не схлопывая набор. См. ProvisioningOrchestrator.swap_node_out."""
    device = db.get(models.Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    orchestrator = ProvisioningOrchestrator(db)
    try:
        added = orchestrator.swap_node_out(device, node_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "device_node_swapped", "device", device_id,
        actor_type=actor_type,
        metadata={"removed_node_id": node_id, "added_nodes": added},
    )
    db.commit()
    db.refresh(device)
    return {
        "device_id": device_id,
        "removed_node_id": node_id,
        "added_nodes": added,
        "nodes": _device_node_set(db, device),
    }


class DiverseBackfillIn(BaseModel):
    # сколько девайсов ТРОНУТЬ за прогон (пейсинг, чтоб не осушить warm-пул).
    limit: int = Field(default=20, ge=1, le=500)
    # dry_run=True (дефолт!) — только отчёт охвата, без мутаций.
    dry_run: bool = True
    # таргетированный добор: только подписки этого user_id (None = вся база).
    user_id: int | None = None


@router.post("/subscriptions/diverse-backfill")
def diverse_backfill(
    body: DiverseBackfillIn,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Phase A.2 — добрать СУЩЕСТВУЮЩИЕ подписки до диверс-набора, пейсимо.

    Идемпотентно + best-effort (см. ProvisioningOrchestrator.backfill_diverse_
    subscriptions). dry_run=true (дефолт) — посмотреть охват без мутаций; затем
    гонять малыми порциями (limit) и проверять в админке node-set девайса.
    Ответ различает topped_up/no_op — когда topped_up=0 при ненулевом
    eligible_total, остаток упёрся в дефицит тёплых нод (пора заказывать).
    ⚠️ Гонять ПО ОДНОМУ — параллельные вызовы могут перебрать набор > N."""
    orchestrator = ProvisioningOrchestrator(db)
    result = orchestrator.backfill_diverse_subscriptions(
        limit=body.limit, dry_run=body.dry_run, user_id=body.user_id
    )
    if not body.dry_run and result.get("processed"):
        actor, actor_type = _resolve_admin_actor(admin_actor)
        _audit(
            db, actor, "diverse_backfill", "subscription", body.user_id,
            actor_type=actor_type,
            metadata={
                "processed": result["processed"],
                "nodes_added": result["nodes_added"],
                "legs_relaid": result.get("legs_relaid"),
                "legs_incomplete": result.get("legs_incomplete"),
                "eligible_total": result["eligible_total"],
                "limit": body.limit,
                "user_id": body.user_id,
            },
        )
    return result


@router.post(
    "/devices/{device_id}/switch-exit",
    response_model=schemas.DeviceSwitchExitOut,
)
def switch_device_exit(
    device_id: int,
    payload: schemas.DeviceSwitchExitIn,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Re-pin a single device's creds to a different exit on its relay.

    Per-device counterpart to ``POST /subscriptions/{id}/switch-exit``.
    Siblings on the same sub stay on their current exits — useful when
    admin wants to test exit-Y performance on one device before moving
    the whole sub.
    """
    device = db.get(models.Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.status in (
        models.DeviceStatus.disabled,
        models.DeviceStatus.revoked,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"device is {device.status.value}, must be active/pending",
        )

    orchestrator = ProvisioningOrchestrator(db)
    try:
        old_exit_id, new_interface, tasks = orchestrator.switch_device_exit(
            device, payload.exit_id
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "device_exit_switched",
        "device",
        device.id,
        actor_type=actor_type,
        metadata={
            "old_exit_id": old_exit_id,
            "new_exit_id": payload.exit_id,
            "new_interface": new_interface,
            "subscription_id": device.subscription_id,
            "task_count": len(tasks),
        },
    )
    db.commit()
    return schemas.DeviceSwitchExitOut(
        device_id=device.id,
        old_exit_id=old_exit_id,
        new_exit_id=payload.exit_id,
        new_interface=new_interface,
        task_ids=[t.id for t in tasks],
    )


@router.post("/subscriptions/{subscription_id}/unblock-sharing")
def unblock_sharing(
    subscription_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Unblock a user whose sharing enforcer blocked them on the node.

    Writes the device access_username(s) into the node's
    ``enforcer_unblock.txt`` file via SSH.  The enforcer daemon picks
    it up on the next tick (≤10s), removes the block from its state
    and re-adds the user to xray.
    """
    import os
    import time

    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    node = sub.node
    if not node:
        raise HTTPException(status_code=400, detail="Subscription has no node")

    # Collect access_username(s) for all live devices on this sub
    devices = (
        db.query(models.Device)
        .filter(
            models.Device.subscription_id == sub.id,
            models.Device.status.notin_(
                [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
            ),
        )
        .all()
    )
    emails = {d.access_username for d in devices if d.access_username}
    if not emails:
        raise HTTPException(status_code=400, detail="No active devices with access_username")

    # SSH to node and append emails to unblock file
    try:
        import paramiko
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="paramiko not available") from exc

    key_path = (
        os.getenv("ANSIBLE_PRIVATE_KEY_FILE")
        or os.getenv("PROVISIONING_SSH_KEY")
        or "/run/secrets/provisioning_key"
    )
    if not os.path.exists(key_path):
        raise HTTPException(status_code=500, detail="SSH key not found")

    pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=node.host,
            port=node.ssh_port or 22,
            username="root",
            pkey=pkey,
            timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )
        # TCP keepalive: рвём half-open соединение, а не висим на нём вечно
        # (нездоровая нода / тихо оборванный TCP).
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(5)
        # Append each email on a separate line
        email_lines = "\\n".join(sorted(emails))
        cmd = f'printf "{email_lines}\\n" >> /var/log/xray/enforcer_unblock.txt'
        stdin, stdout, stderr = client.exec_command(cmd, timeout=10)
        # recv_exit_status() ждёт status_event БЕЗ таймаута (paramiko #448):
        # channel timeout на него не распространяется. Поллим готовность с
        # дедлайном, иначе поток threadpool'а зависнет навсегда на больной ноде.
        deadline = time.monotonic() + 15
        while not stdout.channel.exit_status_ready():
            if time.monotonic() > deadline:
                raise HTTPException(
                    status_code=504,
                    detail="SSH command timed out waiting for exit status",
                )
            time.sleep(0.2)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            err = stderr.read().decode("utf-8", errors="replace")
            raise HTTPException(status_code=500, detail=f"SSH command failed: {err}")
    except paramiko.SSHException as exc:
        raise HTTPException(status_code=502, detail=f"SSH error: {exc}") from exc
    finally:
        client.close()

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "sharing_unblock",
        "subscription",
        subscription_id,
        actor_type=actor_type,
        metadata={"emails": sorted(emails), "node": node.name},
    )
    return {
        "subscription_id": sub.id,
        "emails_unblocked": sorted(emails),
        "node": node.name,
    }


@router.get(
    "/subscriptions/{subscription_id}/status",
    response_model=schemas.SubscriptionStatusOut,
)
def get_subscription_status(subscription_id: int, db: Session = Depends(get_db)):
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    is_active = sub.status == models.SubscriptionStatus.active and sub.expires_at > utcnow()
    return schemas.SubscriptionStatusOut(
        plan_name=sub.plan.name,
        # node may be NULL if the sub was terminated and its node later deleted.
        server_name=sub.node.name if sub.node else "(удалена)",
        expires_at=sub.expires_at,
        is_active=is_active,
        proto_configs=[schemas.CredentialOut.from_orm(c) for c in sub.credentials],
    )
