"""User admin endpoints: ``/api/users/*``.

Includes the ``_subscriptions_for_user`` helper that is re-exported at
the package level for ``api_webapp`` to use (it composes the same
response shape for the user-facing webapp history view).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
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


def _subscriptions_for_user(user_id: int, db: Session) -> list[schemas.SubscriptionOut]:
    subs = (
        db.query(models.Subscription)
        .filter(models.Subscription.user_id == user_id)
        .all()
    )
    if not subs:
        raise HTTPException(status_code=404, detail="Subscriptions not found")
    result = []
    for sub in subs:
        item = schemas.SubscriptionOut(
            id=sub.id,
            plan_name=sub.plan.name,
            plan_id=sub.plan_id,
            node=sub.node.name,
            region=sub.node.region,
            expires_at=sub.expires_at,
            status=sub.status.value,
            auto_renew=sub.auto_renew or False,
            sub_token=sub.sub_token,
            credentials=[schemas.CredentialOut.from_orm(c) for c in sub.credentials],
            devices=[schemas.DeviceOut.from_orm(d) for d in sub.devices],
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


@router.get("/users", response_model=list[schemas.UserOut])
def list_users(
    limit: int = Query(default=50, le=200, ge=1),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    q = db.query(models.User)
    if search:
        like = f"%{search}%"
        q = q.filter(
            (models.User.telegram_id.ilike(like)) | (models.User.email.ilike(like))
        )
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
        )
        for u in users
    ]


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
        days_left = None
        if sub.expires_at:
            delta = (sub.expires_at - now).total_seconds()
            days_left = max(int(delta // 86400), 0)

        sub_summaries.append({
            "id": sub.id,
            "plan_name": plan.name if plan else "",
            "status": sub.status.value,
            "plan_price_kopecks": price,
            "plan_duration_days": plan.duration_days if plan else 30,
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "auto_renew": bool(sub.auto_renew),
            "frozen_until": sub.frozen_until.isoformat() if sub.frozen_until else None,
        })
        if days_left is not None and sub.status == models.SubscriptionStatus.active:
            min_days = days_left if min_days is None else min(min_days, days_left)

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
