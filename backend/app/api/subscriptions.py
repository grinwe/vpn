"""Subscription and device admin endpoints.

Covers ``/api/subscriptions/*``, ``/api/devices/*`` and the admin
disable/enable/add-device flows. The webapp-facing subscription routes
live in ``api_webapp.py`` instead — this module is admin-token gated,
those are webapp-JWT gated.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
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
    """
    from ..services import balance as balance_svc

    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status == models.SubscriptionStatus.active:
        raise HTTPException(status_code=400, detail="Subscription is already active")

    if sub.status == models.SubscriptionStatus.frozen:
        try:
            balance_svc.unfreeze_subscription(db, sub, auto=False)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        sub.status = models.SubscriptionStatus.active
        sub.notes = None
        sub.next_charge_at = utcnow()
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
        # Append each email on a separate line
        email_lines = "\\n".join(sorted(emails))
        cmd = f'printf "{email_lines}\\n" >> /var/log/xray/enforcer_unblock.txt'
        stdin, stdout, stderr = client.exec_command(cmd, timeout=10)
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
