"""Additional API endpoints for MVP features.

This module adds endpoints for:
  - Dynamic subscription links (GET /sub/{token})
  - Referral system (POST /api/referral/code, GET /api/referral/stats)
  - Notification delivery (GET /api/notifications/pending, POST /api/notifications/{id}/ack)
  - Auto-renew toggle (POST /api/subscriptions/{id}/auto_renew)
  - User registration with referral (POST /api/users/register)
  - Self-service config regeneration (POST /api/users/by_telegram/{id}/regenerate)
  - Rate-limited invoice creation (applied via decorator)
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import timedelta

from .time_utils import utcnow
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import models, schemas
from .auth import AuthPrincipal, require_scope, SCOPE_PROBE_WRITE
from .config import get_settings
from .db import SessionLocal
from .security import decrypt as _decrypt

logger = logging.getLogger(__name__)
settings = get_settings()

ext_router = APIRouter(prefix="/api")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_admin(x_admin_token: str | None = Header(default=None)) -> str:
    import hmac
    if not settings.admin_api_token:
        raise HTTPException(status_code=503, detail="admin token missing")
    if not x_admin_token or not hmac.compare_digest(
        x_admin_token.encode(), settings.admin_api_token.encode(),
    ):
        raise HTTPException(status_code=401, detail="invalid admin token")
    return x_admin_token


def optional_admin(x_admin_token: str | None = Header(default=None)) -> str | None:
    if x_admin_token:
        return require_admin(x_admin_token)
    return None


# ── Dynamic subscription link ──

class SubLinkConfig(BaseModel):
    protocol: str
    uri: str


class SubLinkResponse(BaseModel):
    subscription_id: int
    status: str
    expires_at: str
    configs: list[SubLinkConfig]


@ext_router.get("/sub/{token}")
def dynamic_sub_link(token: str, db: Session = Depends(get_db)):
    """Dynamic subscription link. Returns current configs as JSON.

    Clients (Hiddify, v2rayNG) poll this URL and auto-update when the
    server changes due to migration. The token is stable across migrations.
    """
    sub = db.query(models.Subscription).filter_by(sub_token=token).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(status_code=403, detail="Subscription is not active")

    configs = []
    for cred in sub.credentials:
        if not cred.is_active:
            continue
        decrypted = _decrypt(cred.config_text)
        if decrypted:
            configs.append(SubLinkConfig(protocol=cred.proto, uri=decrypted))

    # Also return as plain text for Hiddify/v2rayNG subscription import
    # format: one URI per line
    import base64
    uris = "\n".join(c.uri for c in configs)
    encoded = base64.b64encode(uris.encode()).decode()

    # Return based on Accept header
    return PlainTextResponse(
        content=encoded,
        media_type="text/plain",
        headers={
            "subscription-userinfo": f"expire={int(sub.expires_at.timestamp())}",
            "profile-update-interval": "6",
        },
    )


# ── Auto-renew toggle ──

class AutoRenewRequest(BaseModel):
    auto_renew: bool


@ext_router.post("/subscriptions/{subscription_id}/auto_renew")
def toggle_auto_renew(
    subscription_id: int,
    body: AutoRenewRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    sub.auto_renew = body.auto_renew
    db.add(sub)
    db.commit()
    return {"ok": True, "auto_renew": sub.auto_renew}


# ── Referral system ──

class ReferralCodeRequest(BaseModel):
    telegram_id: str


class ReferralCodeOut(BaseModel):
    code: str
    uses: int
    bonus_days: int
    reward_days: int


@ext_router.post("/referral/code", response_model=ReferralCodeOut)
def get_or_create_referral(
    body: ReferralCodeRequest,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin),
):
    """Get existing referral code or create a new one for the user."""
    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    existing = (
        db.query(models.ReferralCode)
        .filter_by(owner_id=user.id, is_active=True)
        .first()
    )
    if existing:
        return ReferralCodeOut(
            code=existing.code,
            uses=existing.uses,
            bonus_days=existing.bonus_days,
            reward_days=existing.reward_days,
        )

    code = secrets.token_urlsafe(8)
    ref = models.ReferralCode(owner_id=user.id, code=code)
    db.add(ref)
    db.commit()
    db.refresh(ref)
    return ReferralCodeOut(
        code=ref.code, uses=ref.uses,
        bonus_days=ref.bonus_days, reward_days=ref.reward_days,
    )


# ── User registration with referral ──

class UserRegisterRequest(BaseModel):
    telegram_id: str
    referral_code: str | None = None


@ext_router.post("/users/register")
def register_user(
    body: UserRegisterRequest,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin),
):
    """Register or get user, optionally applying a referral code."""
    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    created = False
    if not user:
        user = models.User(telegram_id=body.telegram_id)
        db.add(user)
        db.flush()
        created = True

    if body.referral_code and not user.referred_by_id and created:
        ref = (
            db.query(models.ReferralCode)
            .filter_by(code=body.referral_code, is_active=True)
            .first()
        )
        if ref and ref.owner_id != user.id:
            if ref.max_uses is None or ref.uses < ref.max_uses:
                user.referred_by_id = ref.owner_id
                ref.uses += 1
                db.add(ref)

    db.commit()
    return {"id": user.id, "telegram_id": user.telegram_id, "created": created}


# ── Self-service: regenerate config ──

@ext_router.post("/users/by_telegram/{telegram_id}/regenerate")
def regenerate_user_config(
    telegram_id: str,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Revoke current devices and re-provision on a different node."""
    from .services.provisioning import ProvisioningOrchestrator

    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    active_sub = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.user_id == user.id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .order_by(models.Subscription.created_at.desc())
        .first()
    )
    if not active_sub:
        raise HTTPException(status_code=404, detail="No active subscription")

    orchestrator = ProvisioningOrchestrator(db)

    # Revoke old devices
    for device in active_sub.devices:
        try:
            orchestrator.revoke_device(device, reason="self-service regeneration")
        except Exception:
            logger.exception("Failed to revoke device %s", device.id)

    # Re-provision (potentially on a different node)
    new_sub, task = orchestrator.provision_subscription(
        user, active_sub.plan,
        expires_at_override=active_sub.expires_at,
    )

    # Mark old subscription
    active_sub.status = models.SubscriptionStatus.blocked
    active_sub.notes = "replaced by self-service regeneration"
    db.add(active_sub)
    db.commit()

    return {
        "ok": True,
        "new_subscription_id": new_sub.id,
        "task_id": task.id,
    }


# ── Notification delivery system ──

class NotificationOut(BaseModel):
    id: int
    telegram_id: str
    text: str
    type: str = "info"


@ext_router.get("/notifications/pending", response_model=list[NotificationOut])
def get_pending_notifications(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    limit: int = 20,
):
    """Return pending bot notifications (config-ready, migration, renewals).

    The bot polls this endpoint and delivers messages to users. Notifications
    are generated by the worker (provisioning callbacks, renewal checks,
    health-triggered migrations) and stored in AuditLog with special actions.
    """
    notif_actions = [
        "renewal_reminder", "expiry_reminder", "config_ready", "migration_notice",
    ]
    logs = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.action.in_(notif_actions),
            models.AuditLog.actor_type == models.AuditActor.system,
        )
        .order_by(models.AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )

    results = []
    for log in logs:
        extra = log.extra or {}
        telegram_id = extra.get("telegram_id")
        if not telegram_id:
            continue

        if log.action == "renewal_reminder":
            text = (
                "⏰ Подписка истекает скоро!\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Используй /renew для продления."
            )
        elif log.action == "expiry_reminder":
            text = (
                "⚠️ Твоя подписка скоро истекает.\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Используй /renew или /plans."
            )
        elif log.action == "config_ready":
            text = (
                "✅ Конфиг VPN готов!\n"
                "Используй /config чтобы получить ссылку для подключения."
            )
        elif log.action == "migration_notice":
            text = (
                "🔄 Твой VPN-сервер был перемещён.\n"
                "Новый конфиг доступен по /config.\n"
                "Если у тебя ссылка подписки — она обновилась автоматически."
            )
        else:
            continue

        results.append(NotificationOut(
            id=log.id,
            telegram_id=telegram_id,
            text=text,
            type=log.action,
        ))

    return results


@ext_router.post("/notifications/{notif_id}/ack")
def ack_notification(
    notif_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Acknowledge a notification so it won't be returned again.

    We simply update the action name to mark it delivered.
    """
    log = db.get(models.AuditLog, notif_id)
    if not log:
        raise HTTPException(status_code=404, detail="Notification not found")
    log.action = f"{log.action}:delivered"
    db.add(log)
    db.commit()
    return {"ok": True}
