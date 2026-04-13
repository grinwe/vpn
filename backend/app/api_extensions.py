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

import logging
import secrets


from fastapi import APIRouter, Depends, HTTPException

from .auth import optional_admin, require_admin  # noqa: F401 — re-exported for legacy imports
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .db import SessionLocal
from .security import decrypt as _decrypt
from .time_utils import utcnow

logger = logging.getLogger(__name__)
settings = get_settings()

ext_router = APIRouter(prefix="/api")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
    """Dynamic subscription link — per-device or legacy per-subscription.

    Lookup order:
    1. Device.sub_token → returns only that device's credentials (secure).
    2. Subscription.sub_token → backward compat for clients installed
       before per-device tokens. Returns all active credentials.

    Clients (Hiddify, v2rayNG) poll this URL and auto-update when the
    server changes due to migration. The token is stable across migrations.
    """
    import base64

    # ── Per-device lookup (preferred) ──────────────────────────────────
    device = db.query(models.Device).filter_by(sub_token=token).first()
    if device:
        sub = device.subscription
        if sub.status != models.SubscriptionStatus.active:
            raise HTTPException(status_code=403, detail="Subscription is not active")
        if sub.expires_at and sub.expires_at < utcnow():
            raise HTTPException(status_code=403, detail="Subscription expired")

        configs = []
        for cred in device.credentials:
            if not cred.is_active:
                continue
            decrypted = _decrypt(cred.config_text)
            if decrypted:
                configs.append(SubLinkConfig(protocol=cred.proto, uri=decrypted))

        db.add(
            models.AuditLog(
                actor=str(sub.user_id),
                actor_type=models.AuditActor.user,
                action="subscription_fetch",
                target_type="device",
                target_id=device.id,
                extra={"protocols": [c.protocol for c in configs], "device_token": True},
            )
        )
        db.commit()

        uris = "\n".join(c.uri for c in configs)
        encoded = base64.b64encode(uris.encode()).decode()
        title = "V8-VPN"
        return PlainTextResponse(
            content=encoded,
            media_type="text/plain",
            headers={
                "subscription-userinfo": f"expire={int(sub.expires_at.timestamp())}",
                "profile-update-interval": "6",
                "profile-title": title,
                "content-disposition": f'attachment; filename="{title}"',
            },
        )

    # ── Legacy per-subscription fallback ───────────────────────────────
    sub = db.query(models.Subscription).filter_by(sub_token=token).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(status_code=403, detail="Subscription is not active")

    if sub.expires_at and sub.expires_at < utcnow():
        raise HTTPException(status_code=403, detail="Subscription expired")

    configs = []
    for cred in sub.credentials:
        if not cred.is_active:
            continue
        decrypted = _decrypt(cred.config_text)
        if decrypted:
            configs.append(SubLinkConfig(protocol=cred.proto, uri=decrypted))
        else:
            logger.warning(
                "sub-link: decrypt returned empty for credential %s (proto=%s, sub=%s)",
                cred.id, cred.proto, sub.id,
            )

    uris = "\n".join(c.uri for c in configs)
    encoded = base64.b64encode(uris.encode()).decode()

    db.add(
        models.AuditLog(
            actor=str(sub.user_id),
            actor_type=models.AuditActor.user,
            action="subscription_fetch",
            target_type="subscription",
            target_id=sub.id,
            extra={"protocols": [c.protocol for c in configs], "legacy_token": True},
        )
    )
    db.commit()

    title = "V8-VPN"
    return PlainTextResponse(
        content=encoded,
        media_type="text/plain",
        headers={
            "subscription-userinfo": f"expire={int(sub.expires_at.timestamp())}",
            "profile-update-interval": "6",
            "profile-title": title,
            "content-disposition": f'attachment; filename="{title}"',
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

    # Trial-flow rework: /register no longer credits bonuses. It only
    # attaches the referral when the field is still NULL — this means a
    # user who accidentally /start'ed before ever getting a ref link can
    # still be attributed the next time they click one. The actual
    # referee-side bonus is handed out on /trial/activate, and the
    # referrer-side bonus lands on the first confirmed kind=topup (see
    # _mark_invoice_paid_core in api.py).
    if body.referral_code and not user.referred_by_id:
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
                db.flush()

    db.commit()
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "created": created,
        # Always false under the trial-flow model — retained for
        # backward compat with older bot builds that still read it.
        "referral_bonus_credited": False,
        # Bot reads this to decide whether to include the "first month
        # on us" line in the welcome copy. True iff the user hasn't
        # activated their trial yet — works retroactively for users
        # who registered before this column existed.
        "trial_available": user.trial_activated_at is None,
    }


# ── Free trial activation ──

class TrialActivateRequest(BaseModel):
    telegram_id: str


class TrialActivateResponse(BaseModel):
    trial_amount_kopecks: int
    referral_bonus_kopecks: int
    balance_kopecks: int
    trial_expires_at: str  # ISO-8601, serialized from datetime


@ext_router.post("/trial/activate", response_model=TrialActivateResponse)
def activate_trial(
    body: TrialActivateRequest,
    db: Session = Depends(get_db),
    admin_token: str | None = Depends(optional_admin),
):
    """Grant the one-time trial bonus to a user identified by telegram_id.

    Gated on ``User.trial_activated_at IS NULL``. Returns 409 on a
    repeat call so the client knows to hide the banner, 404 if the
    user row doesn't exist yet (the bot should /register first), and
    503 if no visible 30-day plan is configured (nothing to size the
    trial against). Bonus amounts read from the Plan table at call
    time — no hardcoded rubles.
    """
    from .services import trial as trial_svc

    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        result = trial_svc.activate_trial(db, user.id)
    except trial_svc.TrialAlreadyActivated:
        raise HTTPException(status_code=409, detail="Trial already activated")
    except trial_svc.NoTrialPlan:
        raise HTTPException(status_code=503, detail="No trial plan configured")
    db.commit()
    return TrialActivateResponse(
        trial_amount_kopecks=result.trial_amount_kopecks,
        referral_bonus_kopecks=result.referral_bonus_kopecks,
        balance_kopecks=result.balance_kopecks,
        trial_expires_at=result.trial_expires_at.isoformat(),
    )


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
    # Optional context the bot needs to build inline keyboards. Currently
    # used by Phase C health-ping prompts so the callback data can carry
    # the subscription ID — leave NULL for notification types that don't
    # need it (renewal_reminder, expiry_reminder, ...).
    subscription_id: int | None = None


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
        "renewal_reminder", "expiry_reminder",
        "renewal_reminder_1d", "expiry_reminder_1d",
        "config_ready", "migration_notice",
        "low_balance_warning", "trial_expiry_warning", "health_ping_request",
        "sharing_warning", "sharing_kick", "sharing_block",
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
                "⏰ Подписка истекает через 3 дня!\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Пополни баланс, чтобы автопродление сработало."
            )
        elif log.action == "renewal_reminder_1d":
            text = (
                "🚨 Подписка истекает завтра!\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Пополни баланс сейчас — иначе подписка отключится."
            )
        elif log.action == "expiry_reminder":
            text = (
                "⚠️ Твоя подписка истекает через 3 дня.\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Используй /renew или /plans для продления."
            )
        elif log.action == "expiry_reminder_1d":
            text = (
                "🚨 Подписка истекает завтра!\n"
                f"Дата: {extra.get('expires_at', '?')[:10]}\n"
                "Продли сейчас через /renew, иначе VPN отключится."
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
        elif log.action == "low_balance_warning":
            days = extra.get("days_remaining", "?")
            balance_rub = extra.get("balance_rub", "?")
            text = (
                f"⚠️ Низкий баланс: {balance_rub} ₽ — хватит на {days} дн.\n"
                "Пополни через /balance, иначе подписка отключится."
            )
        elif log.action == "trial_expiry_warning":
            text = (
                "⏳ Твой пробный месяц кончается через 3 дня.\n"
                "Пополни баланс, чтобы подписка не отключилась — "
                "реферальные 50 ₽ (если есть) остаются при тебе."
            )
        elif log.action == "health_ping_request":
            text = (
                "🛟 Помогите нам улучшить сервис!\n\n"
                "Подскажите, как сейчас работает VPN на вашем "
                "устройстве? Это займёт одну секунду и поможет нам "
                "быстрее находить и устранять проблемы.\n\n"
                "Спасибо, что вы с нами! 💛"
            )
        elif log.action == "sharing_warning":
            text = (
                "Привет! 👋 Мы заметили, что к твоему аккаунту "
                "подключаются с нескольких устройств одновременно. "
                "Напоминаем, что передача конфигурации другим людям "
                "запрещена правилами сервиса — это влияет на качество "
                "и скорость для всех пользователей. Если это ошибка — "
                "просто проигнорируй это сообщение."
            )
        elif log.action == "sharing_kick":
            text = (
                "Привет! Нам очень жаль, но мы снова зафиксировали "
                "одновременное подключение к твоему аккаунту с нескольких "
                "устройств. Соединение было временно разорвано. Передача "
                "конфигурации снижает качество сервиса для всех, поэтому "
                "мы вынуждены реагировать. Пожалуйста, убедись, что "
                "конфигурацию используешь только ты."
            )
        elif log.action == "sharing_block":
            text = (
                "Привет 😔 Нам очень жаль, но мы вынуждены временно "
                "заблокировать доступ к VPN — мы зафиксировали "
                "систематическое использование аккаунта с нескольких "
                "устройств. Это негативно влияет на качество услуги "
                "для других пользователей, поэтому мы не можем это "
                "игнорировать. Напиши в поддержку — мы разберёмся "
                "и поможем восстановить доступ."
            )
        else:
            continue

        sub_id_extra: int | None = None
        # health_ping_request always carries subscription_id in extra.
        # Other notification types might too (e.g. config_ready) but we
        # only forward it for the ones the bot needs it for, to keep
        # the keyboard-routing logic on the bot side simple.
        if log.action == "health_ping_request":
            raw = extra.get("subscription_id")
            if isinstance(raw, int):
                sub_id_extra = raw

        results.append(NotificationOut(
            id=log.id,
            telegram_id=telegram_id,
            text=text,
            type=log.action,
            subscription_id=sub_id_extra,
        ))

    return results


# ── Phase C: bot health-ping responses + opt-out ──

class HealthPingResponseRequest(BaseModel):
    telegram_id: str
    subscription_id: int | None = None
    answer: str  # "ok" | "bad"


@ext_router.post("/users/health-ping-response")
def submit_health_ping_response(
    body: HealthPingResponseRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Record a user's answer to the health-ping prompt.

    Writes an AuditLog row that the (eventual) detector reads as a
    time-series of per-node user reports. We don't update health_score
    here — the detector lives in a follow-up — but the row carries
    enough context (subscription_id, node lookup at write time) for
    that future detector to backfill from.
    """
    if body.answer not in ("ok", "bad"):
        raise HTTPException(status_code=400, detail="answer must be 'ok' or 'bad'")

    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    node_id: int | None = None
    if body.subscription_id is not None:
        sub = db.get(models.Subscription, body.subscription_id)
        # Cross-check ownership so a leaked sub_id from one user can't
        # be used to forge a report from another.
        if sub and sub.user_id == user.id:
            node_id = sub.node_id

    db.add(
        models.AuditLog(
            actor=str(user.id),
            actor_type=models.AuditActor.user,
            action="health_ping_response",
            target_type="subscription",
            target_id=body.subscription_id,
            extra={
                "telegram_id": body.telegram_id,
                "answer": body.answer,
                "node_id": node_id,
            },
        )
    )
    db.commit()
    return {"ok": True}


class HealthPingOptOutRequest(BaseModel):
    telegram_id: str


@ext_router.post("/users/health-ping-opt-out")
def opt_out_health_ping(
    body: HealthPingOptOutRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Set ``User.health_ping_opt_out`` so the worker stops queueing
    pings for this user. Idempotent — calling twice is fine.
    """
    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.health_ping_opt_out = True
    db.add(user)
    db.add(
        models.AuditLog(
            actor=str(user.id),
            actor_type=models.AuditActor.user,
            action="health_ping_opt_out",
            target_type="user",
            target_id=user.id,
            extra={"telegram_id": body.telegram_id},
        )
    )
    db.commit()
    return {"ok": True, "opted_out": True}


# ── Per-user notification preferences ──

class NotificationPrefsRequest(BaseModel):
    telegram_id: str
    notify_renewals: bool | None = None
    notify_migrations: bool | None = None
    health_ping_opt_out: bool | None = None


class NotificationPrefsOut(BaseModel):
    notify_renewals: bool
    notify_migrations: bool
    health_ping_opt_out: bool


@ext_router.get("/users/notification-prefs")
def get_notification_prefs(
    telegram_id: str,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Return current notification preferences for a user."""
    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return NotificationPrefsOut(
        notify_renewals=user.notify_renewals,
        notify_migrations=user.notify_migrations,
        health_ping_opt_out=user.health_ping_opt_out,
    )


@ext_router.post("/users/notification-prefs")
def update_notification_prefs(
    body: NotificationPrefsRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Update notification preferences. Only provided fields are changed."""
    user = db.query(models.User).filter_by(telegram_id=body.telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    changed: dict[str, bool] = {}
    if body.notify_renewals is not None:
        user.notify_renewals = body.notify_renewals
        changed["notify_renewals"] = body.notify_renewals
    if body.notify_migrations is not None:
        user.notify_migrations = body.notify_migrations
        changed["notify_migrations"] = body.notify_migrations
    if body.health_ping_opt_out is not None:
        user.health_ping_opt_out = body.health_ping_opt_out
        changed["health_ping_opt_out"] = body.health_ping_opt_out

    if changed:
        db.add(user)
        db.add(
            models.AuditLog(
                actor=str(user.id),
                actor_type=models.AuditActor.user,
                action="notification_prefs_updated",
                target_type="user",
                target_id=user.id,
                extra={"telegram_id": body.telegram_id, **changed},
            )
        )
        db.commit()

    return NotificationPrefsOut(
        notify_renewals=user.notify_renewals,
        notify_migrations=user.notify_migrations,
        health_ping_opt_out=user.health_ping_opt_out,
    )


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
