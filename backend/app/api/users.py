"""User admin endpoints: ``/api/users/*``.

Includes the ``_subscriptions_for_user`` helper that is re-exported at
the package level for ``api_webapp`` to use (it composes the same
response shape for the user-facing webapp history view).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..services.provisioning import ProvisioningOrchestrator
from ..time_utils import utcnow
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


class AdminTopupRequest(BaseModel):
    # Kopecks (integer), positive only. We route through balance_svc.topup
    # which enforces >0 — no negatives here. For debits/clawbacks there
    # should be a separate explicit endpoint, it's a much rarer operation
    # and we want the intent to be obvious in audit logs.
    amount_kopecks: int = Field(gt=0, le=10_000_000)
    note: str | None = Field(default=None, max_length=200)


class AdminTopupResponse(BaseModel):
    user_id: int
    telegram_id: str | None
    balance_kopecks: int
    tx_id: int


def _sub_sharing_blocked(db: Session, sub: models.Subscription) -> bool:
    """Decide whether this subscription currently has a sharing-enforcer
    block in effect on the node.

    Source of truth is the AuditLog stream: `traffic_stats.collect_and_persist`
    writes a `sharing_block` row per (email, tick) when the on-node enforcer
    reports a block, and `/subscriptions/{id}/unblock-sharing` writes a
    `sharing_unblock` row. For each live device email we check whether the
    latest block is more recent than the latest unblock mentioning that email;
    if yes for any one email — the sub is blocked.

    Хелпер сидит не только в админке, но и на горячем пути вебаппа
    (``_subscriptions_for_user`` ← ``api_webapp``), а audit_logs —
    append-only и без индексов под эти фильтры, так что каждый запрос —
    seq scan всей таблицы. Поэтому вместо 2 запросов на каждый email
    делаем ОДИН агрегирующий запрос на все email подписки сразу; в
    типичном случае (блоков не было) на этом и заканчиваем. Запрос по
    unblock-событиям выполняется только для тех email, у которых
    реально есть block — это редкий случай.
    """
    emails = [
        d.access_username for d in sub.devices
        if d.access_username and d.status not in (
            models.DeviceStatus.revoked, models.DeviceStatus.disabled
        )
    ]
    if not emails:
        return False
    # Последний sharing_block по каждому email одним запросом. Блок-аудит
    # хранит email в extra->>'email' (одна строка = один email, пишет
    # traffic_stats при срабатывании энфорсера).
    block_email = models.AuditLog.extra["email"].astext
    block_rows = (
        db.query(block_email, func.max(models.AuditLog.created_at))
        .filter(
            models.AuditLog.action == "sharing_block",
            block_email.in_(emails),
        )
        .group_by(block_email)
        .all()
    )
    for email, last_block_at in block_rows:
        # Latest sharing_unblock event that included this email. Unblocks
        # are batched per-subscription and store the emails list under
        # extra->'emails' (JSONB array). The `?` JSONB op asks
        # "does this array contain this string as a top-level element".
        last_unblock = (
            db.query(models.AuditLog.created_at)
            .filter(
                models.AuditLog.action == "sharing_unblock",
                models.AuditLog.extra["emails"].op("?")(email),
            )
            .order_by(models.AuditLog.created_at.desc())
            .first()
        )
        if last_unblock is None or last_unblock[0] < last_block_at:
            return True
    return False


def _subscriptions_for_user(user_id: int, db: Session) -> list[schemas.SubscriptionOut]:
    subs = (
        db.query(models.Subscription)
        .filter(models.Subscription.user_id == user_id)
        .all()
    )
    if not subs:
        raise HTTPException(status_code=404, detail="Subscriptions not found")
    result = []
    # Cache exit rows across subs/devices to avoid N+1 on the admin
    # "show user" page when a user has many devices across several subs.
    exit_name_cache: dict[int, str | None] = {}

    def _exit_name(exit_id: int | None) -> str | None:
        if exit_id is None:
            return None
        if exit_id in exit_name_cache:
            return exit_name_cache[exit_id]
        exit_row = db.get(models.WGExitNode, exit_id)
        exit_name_cache[exit_id] = exit_row.name if exit_row is not None else None
        return exit_name_cache[exit_id]

    for sub in subs:
        # Sub-level representative cred → used for SubscriptionOut
        # aggregate fields (current_exit_*). Per-device routing is
        # resolved below inside the device loop — after per-device
        # migrate a sub can be split across nodes/exits.
        sub_exit_id: int | None = None
        for cred in sub.credentials:
            if cred.is_active and cred.exit_id is not None:
                sub_exit_id = cred.exit_id
                break
        sub_exit_name = _exit_name(sub_exit_id)

        device_outs: list[schemas.DeviceOut] = []
        for d in sub.devices:
            # Resolve the device's actual home: device.config.node is
            # authoritative post-migrate; fall back to sub.node for
            # terminal devices whose config_id was nulled out by
            # config-delete cleanup.
            d_node = d.config.node if d.config else sub.node
            d_node_id = d_node.id if d_node else None
            d_node_name = d_node.name if d_node else None
            d_node_region = d_node.region if d_node else None
            d_is_relay = bool(d_node.has_relay_config) if d_node else False
            d_exit_id: int | None = None
            for cred in d.credentials:
                if cred.is_active and cred.exit_id is not None:
                    d_exit_id = cred.exit_id
                    break
            device_outs.append(
                schemas.DeviceOut.from_orm(
                    d,
                    node_id=d_node_id,
                    node_name=d_node_name,
                    node_region=d_node_region,
                    is_relay=d_is_relay,
                    exit_id=d_exit_id,
                    exit_name=_exit_name(d_exit_id),
                )
            )

        item = schemas.SubscriptionOut(
            id=sub.id,
            plan_name=sub.plan.name,
            plan_id=sub.plan_id,
            # node may be NULL for terminated subs whose node was deleted.
            node=sub.node.name if sub.node else "(удалена)",
            node_id=sub.node_id,
            region=sub.node.region if sub.node else "",
            expires_at=sub.expires_at,
            status=sub.status.value,
            auto_renew=sub.auto_renew or False,
            sub_token=sub.sub_token,
            credentials=[schemas.CredentialOut.from_orm(c) for c in sub.credentials],
            devices=device_outs,
            sharing_blocked=_sub_sharing_blocked(db, sub),
            current_exit_id=sub_exit_id,
            current_exit_name=sub_exit_name,
        )
        result.append(item)
    return result


@router.post("/users/{user_id}/disable")
def disable_user(
    user_id: int,
    body: schemas.DisableRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    subs = db.query(models.Subscription).filter(models.Subscription.user_id == user_id).all()
    if not subs:
        raise HTTPException(status_code=404, detail="User or subscriptions not found")
    orchestrator = ProvisioningOrchestrator(db)
    total_tasks = 0
    for sub in subs:
        sub.status = models.SubscriptionStatus.blocked
        sub.notes = body.reason
        tasks = orchestrator.revoke_subscription_devices(sub, reason=body.reason)
        total_tasks += len(tasks)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "user_disabled",
        "user",
        user_id,
        metadata={"reason": body.reason},
        actor_type=actor_type,
    )
    return {"disabled": len(subs), "revocation_tasks": total_tasks}


def _apply_banned_filter(q, banned: str | None):
    """Narrow a users query by ban state.  ``banned`` values:
    - ``active`` — only rows with ``banned_at IS NULL``
    - ``banned`` — only rows with ``banned_at IS NOT NULL``
    - ``all`` / ``None`` — no filter (default)
    Raises HTTPException(400) for anything else so the admin SPA catches
    typos early instead of silently getting the full list.
    """
    if banned in (None, "all"):
        return q
    if banned == "active":
        return q.filter(models.User.banned_at.is_(None))
    if banned == "banned":
        return q.filter(models.User.banned_at.isnot(None))
    raise HTTPException(
        status_code=400,
        detail="banned must be one of: all, active, banned",
    )


@router.get("/users", response_model=list[schemas.UserOut])
def list_users(
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    banned: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    q = db.query(models.User)
    if search:
        like = f"%{search}%"
        q = q.filter(
            (models.User.telegram_id.ilike(like)) | (models.User.email.ilike(like))
        )
    q = _apply_banned_filter(q, banned)
    users = q.order_by(models.User.id.desc()).offset(offset).limit(limit).all()
    if not users:
        return []
    user_ids = [u.id for u in users]
    counts_rows = (
        db.query(models.Subscription.user_id, models.Subscription.id)
        .filter(models.Subscription.user_id.in_(user_ids))
        .all()
    )
    counts: dict[int, int] = {}
    for uid, _sid in counts_rows:
        counts[uid] = counts.get(uid, 0) + 1
    return [
        schemas.UserOut(
            id=u.id,
            telegram_id=u.telegram_id,
            email=u.email,
            created_at=u.created_at,
            subscription_count=counts.get(u.id, 0),
            balance_kopecks=u.balance_kopecks or 0,
            banned_at=u.banned_at,
        )
        for u in users
    ]


# Hard cap on the id-list response. 5000 is enough to cover the usual
# spam-wave (single-shot ban of 250+ bots, with headroom) while keeping
# the batch_ban's max_length=500 as a separate server-side hedge —
# admin UI slices this list into chunks of that size.
_USERS_IDS_MAX = 5000


@router.get("/users/ids", response_model=list[int])
def list_user_ids(
    search: str | None = Query(default=None),
    banned: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Return *all* user ids matching the current filter, capped at
    :data:`_USERS_IDS_MAX`.  Feeds the admin "select all filtered"
    action when the operator wants to apply batch_ban to a whole tab
    (typical: "ban all 250 bots at once" — impossible with paginated
    /users because the client doesn't have the ids on later pages).

    Intentionally does NOT join subscriptions or compute any derived
    fields — this endpoint is only an id producer, the UI already has
    the rich rows for the currently-visible page.
    """
    q = db.query(models.User.id)
    if search:
        like = f"%{search}%"
        q = q.filter(
            (models.User.telegram_id.ilike(like)) | (models.User.email.ilike(like))
        )
    q = _apply_banned_filter(q, banned)
    rows = q.order_by(models.User.id.desc()).limit(_USERS_IDS_MAX).all()
    return [uid for (uid,) in rows]


@router.get("/users/banned-telegram-ids", response_model=list[str])
def list_banned_telegram_ids(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Return the list of Telegram IDs for currently-banned users.

    The bot pulls this periodically (short TTL cache) so its outer
    middleware can drop incoming updates from banned accounts WITHOUT
    making a backend call per update — the motivating scenario is a
    DDoS where hundreds of bot accounts spam ``/start``. One admin API
    call every N seconds beats N calls per second.

    Only rows where ``banned_at IS NOT NULL AND telegram_id IS NOT NULL``.
    Legacy users without a ``telegram_id`` can't send TG updates anyway,
    so there's nothing to gate.
    """
    rows = (
        db.query(models.User.telegram_id)
        .filter(
            models.User.banned_at.isnot(None),
            models.User.telegram_id.isnot(None),
        )
        .all()
    )
    return [tg for (tg,) in rows]


@router.post("/users/{user_id}/ban")
def ban_user(
    user_id: int,
    body: schemas.BanRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Mark a user as banned.

    Bot middleware drops every incoming update from banned users
    silently (no reply — DDoS bots don't get feedback). Orthogonal to
    subscriptions: this endpoint does NOT revoke/block subs, and
    ``/users/{id}/disable`` does NOT set banned_at. Idempotent — banning
    an already-banned user returns ``already_banned`` and skips the
    audit write.
    """
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.banned_at is not None:
        return {"status": "already_banned", "banned_at": user.banned_at}
    user.banned_at = utcnow()
    db.commit()
    db.refresh(user)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "user_banned",
        "user",
        user_id,
        metadata={"reason": body.reason},
        actor_type=actor_type,
    )
    return {"status": "banned", "banned_at": user.banned_at}


@router.post("/users/{user_id}/unban")
def unban_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Clear a user's ban. Idempotent for already-unbanned users."""
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.banned_at is None:
        return {"status": "not_banned"}
    user.banned_at = None
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "user_unbanned",
        "user",
        user_id,
        actor_type=actor_type,
    )
    return {"status": "unbanned"}


# ── Per-node user bans ───────────────────────────────────────────────
# Список нод, на которые авто-выбор (choose_node через exclude_node_ids)
# НЕ должен селить данного юзера. Ортогонально глобальному banned_at.
# Заполняется авто-миграцией («обновить подписку» авто-банит старую
# ноду) и этими ручными эндпоинтами.


@router.get(
    "/users/{user_id}/node-bans",
    response_model=list[schemas.NodeUserBanOut],
)
def list_user_node_bans(
    user_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Per-node баны юзера (с именами нод для админки)."""
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    rows = (
        db.query(models.NodeUserBan, models.VPNNode.name)
        .outerjoin(
            models.VPNNode, models.VPNNode.id == models.NodeUserBan.node_id
        )
        .filter(models.NodeUserBan.user_id == user_id)
        .order_by(models.NodeUserBan.created_at.desc())
        .all()
    )
    return [
        schemas.NodeUserBanOut(
            id=ban.id,
            user_id=ban.user_id,
            node_id=ban.node_id,
            node_name=node_name,
            reason=ban.reason,
            created_by=ban.created_by,
            created_at=ban.created_at,
        )
        for ban, node_name in rows
    ]


@router.post(
    "/users/{user_id}/node-bans",
    response_model=schemas.NodeUserBanOut,
)
def add_user_node_ban(
    user_id: int,
    body: schemas.NodeUserBanCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Вручную забанить юзера на ноде — авто-выбор её пропустит.

    Идемпотентно по (user_id, node_id): повторный бан возвращает
    существующую запись. Не мигрирует юзера — только помечает ноду как
    нежелательную для будущих авто-выборов.
    """
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    node = db.get(models.VPNNode, body.node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    actor, actor_type = _resolve_admin_actor(admin_actor)
    ban = (
        db.query(models.NodeUserBan)
        .filter(
            models.NodeUserBan.user_id == user_id,
            models.NodeUserBan.node_id == body.node_id,
        )
        .first()
    )
    if ban is None:
        try:
            ban = models.NodeUserBan(
                user_id=user_id,
                node_id=body.node_id,
                reason=body.reason,
                created_by=actor,
            )
            db.add(ban)
            db.commit()
            db.refresh(ban)
            _audit(
                db,
                actor,
                "node_user_banned",
                "user",
                user_id,
                metadata={"node_id": body.node_id, "reason": body.reason},
                actor_type=actor_type,
            )
        except IntegrityError:
            # Гонка: параллельный запрос уже создал бан (user_id, node_id).
            # uq_node_user_ban → idempotent: откатываемся и перечитываем.
            db.rollback()
            ban = (
                db.query(models.NodeUserBan)
                .filter(
                    models.NodeUserBan.user_id == user_id,
                    models.NodeUserBan.node_id == body.node_id,
                )
                .first()
            )
            if ban is None:
                raise HTTPException(
                    status_code=409,
                    detail="Конфликт при создании бана — повторите запрос",
                )
    return schemas.NodeUserBanOut(
        id=ban.id,
        user_id=ban.user_id,
        node_id=ban.node_id,
        node_name=node.name,
        reason=ban.reason,
        created_by=ban.created_by,
        created_at=ban.created_at,
    )


@router.delete("/users/{user_id}/node-bans/{node_id}")
def remove_user_node_ban(
    user_id: int,
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Снять per-node бан (разбан) — авто-выбор снова сможет селить юзера
    на эту ноду. Идемпотентно."""
    ban = (
        db.query(models.NodeUserBan)
        .filter(
            models.NodeUserBan.user_id == user_id,
            models.NodeUserBan.node_id == node_id,
        )
        .first()
    )
    if ban is None:
        return {"status": "not_banned"}
    db.delete(ban)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_user_unbanned",
        "user",
        user_id,
        metadata={"node_id": node_id},
        actor_type=actor_type,
    )
    return {"status": "unbanned"}


class BatchBanRequest(BaseModel):
    # `ban` sets banned_at=now on the given user_ids, `unban` clears it.
    # Audit rows are per-user so individual bans remain attributable even
    # when this arrived as a batch. The Invoices bulk endpoint returns the
    # same `{done, skipped, not_found}` shape — we mirror it so the admin
    # UI can reuse the same result-summary toast.
    user_ids: list[int] = Field(min_length=1, max_length=500)
    action: str  # "ban" or "unban"
    reason: str | None = Field(default=None, max_length=500)


@router.post("/users/batch_ban")
def batch_ban(
    body: BatchBanRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    if body.action not in ("ban", "unban"):
        raise HTTPException(
            status_code=400, detail="action must be 'ban' or 'unban'"
        )
    users = (
        db.query(models.User).filter(models.User.id.in_(body.user_ids)).all()
    )
    found_ids = {u.id for u in users}
    not_found = [uid for uid in body.user_ids if uid not in found_ids]
    actor, actor_type = _resolve_admin_actor(admin_actor)

    done: list[int] = []
    skipped: list[int] = []
    now = utcnow()
    for user in users:
        if body.action == "ban":
            if user.banned_at is not None:
                skipped.append(user.id)
                continue
            user.banned_at = now
            db.add(
                models.AuditLog(
                    actor=actor,
                    actor_type=actor_type,
                    action="user_banned",
                    target_type="user",
                    target_id=user.id,
                    extra={"reason": body.reason, "batch": True},
                )
            )
            done.append(user.id)
        else:
            if user.banned_at is None:
                skipped.append(user.id)
                continue
            user.banned_at = None
            db.add(
                models.AuditLog(
                    actor=actor,
                    actor_type=actor_type,
                    action="user_unbanned",
                    target_type="user",
                    target_id=user.id,
                    extra={"batch": True},
                )
            )
            done.append(user.id)
    db.commit()
    return {
        "action": body.action,
        "done": done,
        "skipped": skipped,
        "not_found": not_found,
    }


@router.get(
    "/users/by_telegram/{telegram_id}",
    response_model=list[schemas.SubscriptionOut],
)
def get_user_by_telegram(
    telegram_id: str,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _subscriptions_for_user(user.id, db)


@router.get("/users/by_telegram/{telegram_id}/balance")
def get_balance_by_telegram(
    telegram_id: str,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Bot-side balance lookup (stage 4).

    Returns the user's current balance + per-active-sub days_remaining
    so the bot can render ``/balance`` without re-implementing the
    daily-cost math. Admin-token gated because the bot speaks
    server-to-server with the API token, not WebApp JWT.
    """
    from ..services import balance as balance_svc

    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    subs = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.user_id == user.id,
            models.Subscription.status.in_(
                [
                    models.SubscriptionStatus.active,
                    models.SubscriptionStatus.frozen,
                ]
            ),
        )
        .all()
    )

    now = utcnow()

    sub_summaries: list[dict] = []
    min_days: int | None = None
    for sub in subs:
        plan = sub.plan
        price = balance_svc.plan_price_kopecks(plan) if plan else 0
        duration = plan.duration_days if plan else 30
        days_left = None
        if sub.expires_at:
            delta = (sub.expires_at - now).total_seconds()
            days_left = max(int(delta // 86400), 0)

        sub_summaries.append({
            "id": sub.id,
            "plan_name": plan.name if plan else "",
            "status": sub.status.value,
            "plan_price_kopecks": price,
            "plan_duration_days": duration,
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "auto_renew": bool(sub.auto_renew),
            "frozen_until": sub.frozen_until.isoformat() if sub.frozen_until else None,
        })
        if days_left is not None and sub.status == models.SubscriptionStatus.active:
            # Mirror the webapp runway: days_left + future renewals the
            # wallet can cover. Must use total_renewal_cost_kopecks so
            # device-slot surcharge is counted (bare plan_price would
            # overstate runway for users with paid slots).
            runway = days_left
            if sub.auto_renew and plan:
                renewal_cost = balance_svc.total_renewal_cost_kopecks(sub)
                if renewal_cost > 0:
                    balance_k = user.balance_kopecks or 0
                    runway += (balance_k // renewal_cost) * duration
            min_days = runway if min_days is None else min(min_days, runway)

    return {
        "user_id": user.id,
        "balance_kopecks": user.balance_kopecks or 0,
        "balance_rub": round((user.balance_kopecks or 0) / 100, 2),
        "min_days_remaining": min_days,
        "subscriptions": sub_summaries,
    }


@router.post(
    "/users/by_telegram/{telegram_id}/topup",
    response_model=AdminTopupResponse,
)
def admin_topup_by_telegram(
    telegram_id: str,
    body: AdminTopupRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    actor_header: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Credit a user's balance from the admin panel.

    Intended for test accounts, comped friend accounts, compensating a
    failed payment we confirmed out-of-band, etc. Writes a
    ``kind=adjust`` ledger row with a unique ``admin_topup:<user>:<ts>``
    reference (so repeated clicks create distinct transactions — this is
    not an idempotent upsert, each click is a real new credit) and an
    audit log entry attributing the action to the admin token that made
    the call.
    """
    from ..services import balance as balance_svc

    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ts = int(utcnow().timestamp())
    reference = f"admin_topup:{user.id}:{ts}"
    try:
        tx = balance_svc.topup(
            db,
            user.id,
            body.amount_kopecks,
            reference=reference,
            kind=models.BalanceTxKind.adjust,
            note=body.note or "admin topup",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor, actor_type = _resolve_admin_actor(actor_header)
    db.add(
        models.AuditLog(
            actor=actor,
            actor_type=actor_type,
            action="admin_topup",
            target_type="user",
            target_id=user.id,
            extra={
                "telegram_id": user.telegram_id,
                "amount_kopecks": body.amount_kopecks,
                "reference": reference,
                "note": body.note,
            },
        )
    )
    db.commit()
    db.refresh(user)
    return AdminTopupResponse(
        user_id=user.id,
        telegram_id=user.telegram_id,
        balance_kopecks=user.balance_kopecks or 0,
        tx_id=tx.id,
    )


@router.get("/users/{user_id}", response_model=list[schemas.SubscriptionOut])
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    return _subscriptions_for_user(user_id, db)
