"""Admin endpoint for claiming orphan subscriptions to real users.

Background: after the 2026-05-19 disaster recovery (POSTMORTEM_2026-05-19.md)
~15 warm-pool credentials lost their owner mapping and ended up parked
on a placeholder user (id=999999, telegram_id=``__recovery_orphans__``).
When the real owner contacts support and shares the ``vless://...`` URL
their Hiddify is currently using, this endpoint TRANSFERs the existing
Subscription / Device / Credential bundle to that user.

Nothing is recreated — ``sub_token`` stays the same (preserving the
sub-link invariant), no ansible run fires, the UUID in ``xray.clients[]``
on the node is unchanged. The user's existing VPN session keeps working
across the claim. Full spec + flow in
``docs/operations/admin_claim_orphans.md``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session

from .. import models
from ..auth import require_admin
from ..services.vless import extract_uuid_from_vless_url
from ..time_utils import utcnow
from ._common import (
    ADMIN_ACTOR_HEADER,
    _resolve_admin_actor,
    get_db,
)

router = APIRouter()

# Placeholder user that owns orphan subscriptions/credentials produced
# by the 2026-05-19 disaster recovery. See POSTMORTEM_2026-05-19.md §3.3.3.
ORPHAN_OWNER_ID = 999999


class ClaimOrphanRequest(BaseModel):
    user_id: int | None = None
    telegram_id: str | None = None
    # Either a bare UUID or a full ``vless://UUID@host:port?...`` URI —
    # operators paste whatever the user sends from Hiddify, the backend
    # extracts the UUID itself.
    uuid: str
    plan_id: int | None = None
    expires_at: datetime | None = None
    device_name: str | None = None

    @model_validator(mode="after")
    def _require_identifier(self) -> "ClaimOrphanRequest":
        if self.user_id is None and not self.telegram_id:
            raise ValueError("user_id or telegram_id is required")
        return self


class ClaimedCredentialOut(BaseModel):
    id: int
    proto: str


class ClaimOrphanResponse(BaseModel):
    subscription_id: int
    device_id: int
    old_user_id: int
    new_user_id: int
    new_expires_at: datetime
    claimed_credentials: list[ClaimedCredentialOut]


def _resolve_target_user(
    db: Session, user_id: int | None, telegram_id: str | None
) -> models.User:
    """Strict lookup — never auto-creates.

    The recovery flow always targets a *known* user (we picked them off
    the cached admin tab in 3.1). Auto-create here would silently spawn
    a left user on operator typo.
    """
    if user_id is not None:
        user = db.get(models.User, user_id)
        if user is None:
            raise HTTPException(404, f"User #{user_id} not found")
        return user
    user = (
        db.query(models.User)
        .filter(models.User.telegram_id == telegram_id)
        .first()
    )
    if user is None:
        raise HTTPException(404, f"User with telegram_id={telegram_id} not found")
    return user


@router.post("/admin/claim-orphan", response_model=ClaimOrphanResponse)
def claim_orphan(
    body: ClaimOrphanRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    actor_header: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    uuid = extract_uuid_from_vless_url(body.uuid)
    if not uuid:
        raise HTTPException(
            400,
            "Could not parse UUID — pass either a bare UUID or a vless:// URL.",
        )

    target_user = _resolve_target_user(db, body.user_id, body.telegram_id)
    if target_user.id == ORPHAN_OWNER_ID:
        raise HTTPException(
            400, "Cannot claim orphan to the placeholder user itself."
        )

    # Find credential(s) carrying this UUID. We join Subscription so the
    # subsequent owner check happens against the live ``sub.user_id``,
    # not a stale denormalization.
    creds = (
        db.query(models.Credential)
        .join(
            models.Subscription,
            models.Credential.subscription_id == models.Subscription.id,
        )
        .filter(models.Credential.config_text.ilike(f"%{uuid}%"))
        .all()
    )
    if not creds:
        raise HTTPException(
            404, f"No credential found whose config_text contains UUID {uuid}"
        )

    sub_ids = {c.subscription_id for c in creds if c.subscription_id is not None}
    if len(sub_ids) > 1:
        raise HTTPException(
            409,
            f"UUID matches credentials in multiple subscriptions: {sorted(sub_ids)}",
        )
    sub_id = next(iter(sub_ids), None)
    if sub_id is None:
        raise HTTPException(409, "Matched credential is not bound to any subscription")

    sub = db.get(models.Subscription, sub_id)
    if sub is None:
        raise HTTPException(404, f"Subscription #{sub_id} not found")

    if sub.user_id == target_user.id:
        raise HTTPException(
            409, f"Subscription #{sub.id} is already owned by user_id={target_user.id}"
        )
    if sub.user_id != ORPHAN_OWNER_ID:
        raise HTTPException(
            409,
            f"Subscription #{sub.id} is not an orphan (current owner=user_id={sub.user_id})",
        )

    # Pull every sibling under this subscription so transfer is atomic.
    all_devices = (
        db.query(models.Device)
        .filter(models.Device.subscription_id == sub.id)
        .all()
    )
    all_creds = (
        db.query(models.Credential)
        .filter(models.Credential.subscription_id == sub.id)
        .all()
    )
    if not all_devices:
        raise HTTPException(
            409, f"Subscription #{sub.id} has no devices to transfer"
        )

    plan_id = body.plan_id or sub.plan_id
    plan = db.get(models.Plan, plan_id) if plan_id is not None else None
    if plan is None:
        raise HTTPException(404, f"Plan #{plan_id} not found")

    now = utcnow()
    new_expires = body.expires_at
    if new_expires is None:
        new_expires = now + timedelta(days=plan.duration_days)
    elif new_expires.tzinfo is not None:
        # Columns are naive UTC; strip tz cleanly.
        new_expires = new_expires.astimezone(timezone.utc).replace(tzinfo=None)

    actor, actor_type = _resolve_admin_actor(actor_header)
    old_user_id = sub.user_id
    note_line = (
        f"orphan_claimed by {actor} @ {now.isoformat(timespec='seconds')} "
        f"(uuid={uuid}, from user_id={old_user_id})"
    )
    sub.notes = f"{sub.notes}\n{note_line}" if sub.notes else note_line
    sub.user_id = target_user.id
    sub.plan_id = plan.id
    sub.expires_at = new_expires
    sub.updated_at = now
    for d in all_devices:
        d.user_id = target_user.id
        if body.device_name:
            d.name = body.device_name
        d.updated_at = now

    # Pick a stable "primary" device id for the response — lowest id is
    # a stable handle even when the subscription has multiple devices.
    primary_device = min(all_devices, key=lambda d: d.id)

    db.add(
        models.AuditLog(
            actor=actor,
            actor_type=actor_type,
            action="orphan_claimed",
            target_type="subscription",
            target_id=sub.id,
            extra={
                "uuid": uuid,
                "old_user_id": old_user_id,
                "new_user_id": target_user.id,
                "new_expires_at": new_expires.isoformat(),
                "plan_id": plan.id,
                "credential_ids": [c.id for c in all_creds],
                "device_ids": [d.id for d in all_devices],
            },
        )
    )
    db.commit()
    db.refresh(sub)

    return ClaimOrphanResponse(
        subscription_id=sub.id,
        device_id=primary_device.id,
        old_user_id=old_user_id,
        new_user_id=target_user.id,
        new_expires_at=new_expires,
        claimed_credentials=[
            ClaimedCredentialOut(id=c.id, proto=c.proto) for c in all_creds
        ],
    )
