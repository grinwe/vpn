"""Telegram WebApp endpoints.

WebApp = HTML5 page rendered inside Telegram. Telegram embeds a signed
``initData`` blob into the page; the frontend POSTs it here, we verify the
HMAC against ``BOT_TOKEN`` (standard Telegram scheme), look up / create
the User by ``telegram_id`` and hand back a short-lived signed token used
for subsequent ``/api/webapp/*`` calls. No password, no OAuth — Telegram
is the IdP.

Token format is a hand-rolled compact JWT-ish: ``base64(payload).hmac``.
We avoid PyJWT to keep the dependency surface small; the only thing we
need is "trust this user_id for N minutes".
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import models, schemas
from .api import _subscriptions_for_user
from .config import get_settings
from .db import SessionLocal
from .rate_limit import limiter
from .services.payments.base import ProviderError, get_provider

logger = logging.getLogger(__name__)

webapp_router = APIRouter(prefix="/api/webapp", tags=["webapp"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Telegram initData verification ----------

def _verify_init_data(init_data: str, bot_token: str, ttl_seconds: int) -> dict:
    """Validate Telegram WebApp initData and return parsed fields.

    Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Algorithm: split query-string, pull out ``hash``, sort the rest as
    ``key=value`` lines joined by ``\n``, HMAC-SHA256 it with a secret key
    derived as ``HMAC-SHA256("WebAppData", bot_token)``.

    ``ttl_seconds`` bounds how stale the ``auth_date`` can be. This is
    a handshake, not a session — the caller exchanges the parsed result
    for a session JWT within seconds. Pre-fix (#53, 2026-04) the TTL
    was hardcoded at 24h *and* the check was guarded by ``if auth_date
    and …``, which silently skipped validation when ``auth_date`` was
    missing or zero. Both gaps are closed here: ``auth_date`` must be
    present, a positive integer, not in the future beyond a small
    clock-skew window, and not older than ``ttl_seconds``.
    """
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="initData missing hash")

    data_check_string = "\n".join(
        f"{k}={parsed[k]}" for k in sorted(parsed.keys())
    )
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(status_code=401, detail="initData hash mismatch")

    raw_auth_date = parsed.get("auth_date")
    if raw_auth_date is None or raw_auth_date == "":
        raise HTTPException(status_code=401, detail="initData missing auth_date")
    try:
        auth_date = int(raw_auth_date)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="initData has invalid auth_date")
    if auth_date <= 0:
        raise HTTPException(status_code=401, detail="initData has invalid auth_date")
    now = int(time.time())
    # Tolerate ~60s of clock skew between the Telegram edge and this
    # backend — anything more is suspicious.
    if auth_date - now > 60:
        raise HTTPException(status_code=401, detail="initData auth_date in future")
    if (now - auth_date) > ttl_seconds:
        raise HTTPException(status_code=401, detail="initData expired")

    return parsed


# ---------- Token (signed bearer) ----------

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(user_id: int, secret: str, ttl_seconds: int) -> str:
    payload = {"uid": user_id, "exp": int(time.time()) + ttl_seconds}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def verify_token(token: str, secret: str) -> int:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="malformed token")
    expected_sig = _b64url_encode(
        hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected_sig, sig):
        raise HTTPException(status_code=401, detail="bad token signature")
    try:
        payload = json.loads(_b64url_decode(body))
    except Exception:
        raise HTTPException(status_code=401, detail="bad token payload")
    if int(payload.get("exp", 0)) < time.time():
        raise HTTPException(status_code=401, detail="token expired")
    return int(payload["uid"])


def require_webapp_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> models.User:
    settings = get_settings()
    if not settings.webapp_jwt_secret:
        raise HTTPException(status_code=500, detail="WEBAPP_JWT_SECRET not set")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    user_id = verify_token(token, settings.webapp_jwt_secret)
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="user not found")
    return user


# ---------- Endpoints ----------

class AuthRequest(BaseModel):
    init_data: str


class AuthResponse(BaseModel):
    token: str
    expires_in: int
    user_id: int


@webapp_router.post("/auth", response_model=AuthResponse)
@limiter.limit("10/minute")
def webapp_auth(request: Request, body: AuthRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not set")
    if not settings.webapp_jwt_secret:
        raise HTTPException(status_code=500, detail="WEBAPP_JWT_SECRET not set")

    parsed = _verify_init_data(
        body.init_data, settings.bot_token, settings.webapp_init_data_ttl_seconds
    )
    user_blob = parsed.get("user")
    if not user_blob:
        raise HTTPException(status_code=401, detail="initData missing user")
    try:
        tg_user = json.loads(user_blob)
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="bad user payload")

    telegram_id = str(tg_user.get("id"))
    if not telegram_id or telegram_id == "None":
        raise HTTPException(status_code=401, detail="initData has no user.id")

    user = (
        db.query(models.User)
        .filter(models.User.telegram_id == telegram_id)
        .one_or_none()
    )
    if user is None:
        user = models.User(telegram_id=telegram_id)
        db.add(user)
        db.commit()
        db.refresh(user)

    token = issue_token(user.id, settings.webapp_jwt_secret, settings.webapp_jwt_ttl_seconds)
    return AuthResponse(
        token=token, expires_in=settings.webapp_jwt_ttl_seconds, user_id=user.id
    )


class BalanceInfo(BaseModel):
    """Stage 4 balance summary embedded into ``/me``.

    ``min_days_remaining`` is the *worst-case* runway across all of the
    user's active subs — that's what the WebApp header shows so the
    user immediately sees when their cheapest sub will run out, not the
    optimistic average.
    """
    balance_kopecks: int
    balance_rub: float
    min_days_remaining: int | None
    has_active_balance_sub: bool
    # Free-trial offer. ``trial_available`` is True iff the user hasn't
    # claimed their one-time trial yet (``User.trial_activated_at IS
    # NULL``) — WebApp shows the activation banner. The amount mirrors
    # the cheapest visible 30-day plan, read at request time so a price
    # change in the Plan table auto-propagates without a deploy.
    trial_available: bool
    trial_amount_kopecks: int


class DeviceSummary(BaseModel):
    id: int
    name: str
    status: str
    sub_token: str | None
    created_at: datetime | None


class SubscriptionWebAppExtra(BaseModel):
    """Per-subscription balance/UI fields."""
    subscription_id: int
    plan_name: str | None
    plan_price_kopecks: int
    plan_duration_days: int
    expires_at: datetime | None
    auto_renew: bool
    frozen_until: datetime | None
    can_freeze: bool
    device_count: int
    bundled_devices: int
    # Paid extra-device slots on top of ``bundled_devices``. See
    # ``webapp_add_device``. 0 for every sub that never bought a slot.
    extra_device_slots: int
    # Monthly fee per extra slot; lets the UI show "+₽100/мес за
    # устройство" without hardcoding the env constant on the client.
    extra_device_monthly_kopecks: int
    # Pro-rated fee the user would be charged *right now* for buying
    # one more slot (for the remainder of the current period). Used by
    # the Home.tsx add-device confirm dialog. May be 0 if the new
    # device still fits in the already-paid envelope (e.g. user had
    # bought a slot then removed a device).
    next_extra_fee_kopecks: int
    # Renewal period derived from ``plan.duration_days`` (>=365 → year,
    # else month). Frontend uses it to pick the "₽/год" vs "₽/мес"
    # label and the correct amount from ``total_per_period_kopecks``.
    period: Literal["month", "year"]
    # What the user will be charged on the next renewal. For monthly
    # plans: ``price + slots * EXTRA_DEVICE_MONTHLY_KOPECKS``. For
    # annual: ``price + slots * EXTRA_DEVICE_MONTHLY_KOPECKS * 12``
    # (device-slots are prepaid for the whole year on activation). This
    # is the honest "N ₽/period" number the UI should show.
    total_per_period_kopecks: int
    # Deprecated alias of ``total_per_period_kopecks``, kept for ~2
    # releases because Telegram caches the WebApp bundle aggressively
    # and old clients still read this field. New code must use
    # ``total_per_period_kopecks``.
    total_monthly_kopecks: int
    devices: list[DeviceSummary]


class MeResponse(BaseModel):
    user: schemas.UserOut
    subscriptions: list[schemas.SubscriptionOut]
    balance: BalanceInfo
    subscription_extras: list[SubscriptionWebAppExtra]
    # Stage 8 — boring-domain proxy URL the WebApp should use when
    # rendering the sub link to copy / show as QR. Empty string means
    # "fall back to the relative /api/sub/<token> on the same origin".
    sub_link_base_url: str
    bot_username: str


def _build_subscription_extras(
    db: Session, user: models.User
) -> tuple[list[SubscriptionWebAppExtra], int | None]:
    """Compute per-sub metadata for the WebApp.

    Returns ``(extras, min_days_remaining)``.
    """
    from .services import balance as balance_svc

    now = utcnow_aware()
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

    extras: list[SubscriptionWebAppExtra] = []
    min_days: int | None = None
    for sub in subs:
        plan = sub.plan
        live_device_rows = (
            db.query(models.Device)
            .filter(
                models.Device.subscription_id == sub.id,
                models.Device.status.notin_(
                    [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
                ),
            )
            .order_by(models.Device.id.asc())
            .all()
        )
        live_devices = len(live_device_rows)
        price = balance_svc.plan_price_kopecks(plan) if plan else 0
        duration = plan.duration_days if plan else 30

        # Days until expires_at.
        days_left = None
        if sub.expires_at:
            delta = (sub.expires_at - now).total_seconds()
            days_left = max(int(delta // 86400), 0)

        # Can freeze: active + auto_renew on + hasn't frozen this year.
        current_year = now.year
        already_froze = sub.has_frozen_this_year or False
        if sub.frozen_year != current_year:
            already_froze = False
        can_freeze = (
            sub.status == models.SubscriptionStatus.active
            and sub.auto_renew
            and not already_froze
        )

        bundled = (plan.max_devices if plan else 1) or 1
        slots = sub.extra_device_slots or 0
        # If the user already has a paid envelope big enough for the
        # next device (bundled + slots > live), adding another one is
        # free — show 0. Otherwise quote the pro-rated remainder.
        capacity = bundled + slots
        next_extra_fee = (
            balance_svc.prorated_extra_device_fee(sub)
            if plan and live_devices + 1 > capacity
            else 0
        )
        period: Literal["month", "year"] = "year" if duration >= 365 else "month"
        total_per_period = (
            balance_svc.total_renewal_cost_kopecks(sub) if plan else price
        )
        extras.append(
            SubscriptionWebAppExtra(
                subscription_id=sub.id,
                plan_name=plan.name if plan else None,
                plan_price_kopecks=price,
                plan_duration_days=duration,
                expires_at=sub.expires_at,
                auto_renew=bool(sub.auto_renew),
                frozen_until=sub.frozen_until,
                can_freeze=can_freeze,
                device_count=live_devices,
                bundled_devices=bundled,
                extra_device_slots=slots,
                extra_device_monthly_kopecks=balance_svc.EXTRA_DEVICE_MONTHLY_KOPECKS,
                next_extra_fee_kopecks=next_extra_fee,
                period=period,
                total_per_period_kopecks=total_per_period,
                total_monthly_kopecks=total_per_period,
                devices=[
                    DeviceSummary(
                        id=d.id,
                        name=d.name or f"device-{d.id}",
                        status=d.status.value,
                        sub_token=d.sub_token,
                        created_at=getattr(d, "created_at", None),
                    )
                    for d in live_device_rows
                ],
            )
        )

        if days_left is not None and sub.status == models.SubscriptionStatus.active:
            # Total runway = current period remaining + future renewals
            # the balance can cover (only if auto_renew is on).
            # Renewal cost includes the device-slot surcharge — see
            # balance.total_renewal_cost_kopecks. Pre-fix this used bare
            # plan price, so users with paid slots saw an inflated
            # runway (e.g. 532 дн вместо ~120 дн при 2 слотах).
            runway = days_left
            if sub.auto_renew and plan:
                renewal_cost = balance_svc.total_renewal_cost_kopecks(sub)
                if renewal_cost > 0:
                    balance = user.balance_kopecks or 0
                    future_renewals = balance // renewal_cost
                    runway += future_renewals * duration
            min_days = runway if min_days is None else min(min_days, runway)

    return extras, min_days


def utcnow_aware():
    """Local re-export so the helper above doesn't import from time_utils
    repeatedly. Kept inside this module to avoid an ordering wart with
    the existing imports."""
    from .time_utils import utcnow

    return utcnow()


@webapp_router.get("/me", response_model=MeResponse)
def webapp_me(
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    try:
        subs = _subscriptions_for_user(user.id, db)
    except HTTPException as exc:
        # _subscriptions_for_user raises 404 when there are none —
        # for the WebApp "no subs yet" is a normal first-launch state.
        if exc.status_code == 404:
            subs = []
        else:
            raise

    # Hide terminal subscriptions from the webapp cabinet. `blocked`/`expired`
    # rows are kept in the DB for audit/ledger purposes but showing them as
    # cards confuses users (especially after migration, where the legacy
    # flow left a blocked row behind alongside the new active one).
    subs = [s for s in subs if s.status not in ("blocked", "expired")]

    extras, min_days = _build_subscription_extras(db, user)
    balance_kopecks = user.balance_kopecks or 0

    from .services import trial as trial_svc
    trial_available = user.trial_activated_at is None
    trial_amount = trial_svc.trial_amount_kopecks(db) if trial_available else 0

    balance = BalanceInfo(
        balance_kopecks=balance_kopecks,
        balance_rub=round(balance_kopecks / 100, 2),
        min_days_remaining=min_days,
        has_active_balance_sub=any(e.plan_price_kopecks > 0 for e in extras),
        trial_available=trial_available,
        trial_amount_kopecks=trial_amount,
    )
    user_out = schemas.UserOut(
        id=user.id,
        telegram_id=user.telegram_id,
        email=user.email,
        created_at=user.created_at,
        subscription_count=len(subs),
    )
    return MeResponse(
        user=user_out,
        subscriptions=subs,
        balance=balance,
        subscription_extras=extras,
        sub_link_base_url=os.getenv("SUB_LINK_BASE_URL", "").rstrip("/"),
        bot_username=os.getenv("BOT_USERNAME", ""),
    )


# ---------- Plans (WebApp-shaped) ----------

# Conversion from the plan price (stored in RUB) to Telegram Stars.
# Stars are integer, no fractions. The default 1 ⭐ = 1.5 ₽ is roughly
# what TG charges end users for star packs in RU at time of writing —
# tweak via env when the rate moves. This is read every request so a
# config bump doesn't need a backend restart.
def _stars_per_rub() -> float:
    try:
        return float(os.getenv("WEBAPP_STARS_PER_RUB", "0.67"))
    except (TypeError, ValueError):
        return 0.67


def _rub_to_stars(price_rub: float) -> int:
    stars = price_rub * _stars_per_rub()
    # Round up so we never undercharge on a fractional conversion;
    # also clamp to a minimum of 1 ⭐ since Bot API rejects 0.
    return max(1, int(stars + 0.999))


def _classify_plan(plan: models.Plan) -> tuple[str, str, str | None]:
    """Return (tier, period, badge) for a plan.

    Tier is derived from ``max_devices`` (1 → Solo, 3 → Family, ≥5 → Pro);
    period from ``duration_days``. Badge ``popular`` is hardcoded onto
    Family — same as the bot's plans list, kept in sync deliberately so
    the WebApp doesn't lie about which plan we're pushing.
    """
    md = plan.max_devices or 1
    if md <= 1:
        tier = "Solo"
    elif md <= 3:
        tier = "Family"
    else:
        tier = "Pro"
    period = "year" if (plan.duration_days or 0) >= 365 else "month"
    badge = "popular" if tier == "Family" else None
    return tier, period, badge


class WebAppPlan(BaseModel):
    id: int
    name: str
    tier: str
    period: str
    duration_days: int
    max_devices: int
    price_rub: float
    price_stars: int
    badge: str | None = None


@webapp_router.get("/plans", response_model=list[WebAppPlan])
def webapp_plans(db: Session = Depends(get_db)):
    plans = (
        db.query(models.Plan)
        .filter(models.Plan.is_visible.is_(True))
        .order_by(models.Plan.max_devices.asc(), models.Plan.duration_days.asc())
        .all()
    )
    out: list[WebAppPlan] = []
    for p in plans:
        tier, period, badge = _classify_plan(p)
        price_rub = float(p.price) if p.price is not None else 0.0
        out.append(
            WebAppPlan(
                id=p.id,
                name=p.name,
                tier=tier,
                period=period,
                duration_days=p.duration_days,
                max_devices=p.max_devices or 1,
                price_rub=price_rub,
                price_stars=_rub_to_stars(price_rub),
                badge=badge,
            )
        )
    return out


# ---------- Checkout ----------

class CheckoutRequest(BaseModel):
    plan_id: int
    provider: str = "telegram_stars"
    # When set, this checkout extends an existing subscription instead of
    # creating a new one. Used by the WebApp's "продлить" button on Home.
    subscription_id: int | None = None


class CheckoutResponse(BaseModel):
    invoice_id: int
    provider: str
    pay_url: str
    amount: float
    currency: str


@webapp_router.post("/checkout", response_model=CheckoutResponse)
def webapp_checkout(
    body: CheckoutRequest,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Create an Invoice for the authed WebApp user and hand back a pay URL.

    Reuses the same provider abstraction as the bot's flow, so the
    successful_payment handler picks up the result with zero changes.
    Enforces ``plan.max_devices`` so a user can't checkout a 5th seat on
    a Solo plan — the same gate that ``provision_subscription`` would hit
    later, but moved up to fail loudly before the user pays.
    """
    plan = db.get(models.Plan, body.plan_id)
    if not plan or not plan.is_visible:
        raise HTTPException(status_code=404, detail="Plan not found")

    is_renewal = body.subscription_id is not None
    target_subscription: models.Subscription | None = None
    if is_renewal:
        target_subscription = db.get(models.Subscription, body.subscription_id)
        if not target_subscription or target_subscription.user_id != user.id:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if target_subscription.plan_id != plan.id:
            raise HTTPException(
                status_code=400,
                detail="Renewal plan must match the existing subscription's plan",
            )
    else:
        # New-purchase device cap: how many active subs does this user
        # already have on this plan? If they're already at max_devices,
        # block before money changes hands.
        active_count = (
            db.query(models.Subscription)
            .filter(
                models.Subscription.user_id == user.id,
                models.Subscription.plan_id == plan.id,
                models.Subscription.status == models.SubscriptionStatus.active,
            )
            .count()
        )
        if active_count >= (plan.max_devices or 1):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"You already have {active_count} active subscriptions on "
                    f"this plan (max {plan.max_devices}). Renew an existing one "
                    "or pick a higher tier."
                ),
            )

    # Pricing: send what the bot will actually charge the user. For
    # Stars, we convert RUB → ⭐ here so the Invoice.amount matches the
    # number Telegram will display. For other providers we keep RUB.
    if body.provider == "telegram_stars":
        amount = float(_rub_to_stars(float(plan.price)))
        currency = "XTR"
    else:
        amount = float(plan.price)
        currency = "RUB"

    invoice = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        subscription_id=target_subscription.id if target_subscription else None,
        amount=amount,
        currency=currency,
        action=models.InvoiceAction.renewal if is_renewal else models.InvoiceAction.new_subscription,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    try:
        provider = get_provider(body.provider)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        provider_invoice = provider.create_invoice(
            invoice_id=invoice.id,
            amount=amount,
            currency=currency,
            # Stage 9d: neutral description — bank compliance scanners flag
            # the literal "VPN" in payment metadata. Plan name is intentionally
            # omitted so acquirers see only an opaque order id.
            description=f"Order #{invoice.id}",
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"payment provider error: {exc}")

    payment = models.Payment(
        invoice_id=invoice.id,
        amount=invoice.amount,
        currency=invoice.currency,
        status=models.PaymentStatus.pending,
        provider=provider.name,
        external_id=provider_invoice.external_id,
    )
    db.add(payment)
    db.commit()

    return CheckoutResponse(
        invoice_id=invoice.id,
        provider=provider.name,
        pay_url=provider_invoice.pay_url,
        amount=provider_invoice.amount,
        currency=provider_invoice.currency,
    )


# ---------- Invoice status (for post-checkout polling) ----------

class InvoiceStatusResponse(BaseModel):
    invoice_id: int
    status: str
    subscription_id: int | None
    subscription_active: bool
    has_credentials: bool


@webapp_router.get("/invoices/{invoice_id}", response_model=InvoiceStatusResponse)
def webapp_invoice_status(
    invoice_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Polled by the WebApp after ``tg.openInvoice`` returns ``paid``.

    Walks invoice → subscription → credentials so the frontend can show a
    three-step progress: paid ✓ → provisioned ✓ → credentials ready ✓.
    The credentials check is what gates the "open my config" CTA — until
    Ansible finishes, ``has_credentials`` stays false.
    """
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.user_id != user.id:
        # Don't leak existence — same shape as 404.
        raise HTTPException(status_code=404, detail="Invoice not found")

    sub = None
    if invoice.subscription_id:
        sub = db.get(models.Subscription, invoice.subscription_id)
    if sub is None:
        # mark_paid creates the subscription and back-links it onto the
        # invoice; if it's still missing, provisioning hasn't reached that
        # step yet.
        sub = (
            db.query(models.Subscription)
            .filter(models.Subscription.user_id == user.id)
            .filter(models.Subscription.plan_id == invoice.plan_id)
            .order_by(models.Subscription.id.desc())
            .first()
        )

    has_credentials = False
    sub_active = False
    sub_id: int | None = None
    if sub is not None:
        sub_id = sub.id
        sub_active = sub.status == models.SubscriptionStatus.active
        has_credentials = bool(sub.credentials)

    return InvoiceStatusResponse(
        invoice_id=invoice.id,
        status=invoice.status.value if hasattr(invoice.status, "value") else str(invoice.status),
        subscription_id=sub_id,
        subscription_active=sub_active,
        has_credentials=has_credentials,
    )


# ---------- Stage 4: Balance billing endpoints ----------

class TopupRequest(BaseModel):
    amount_kopecks: int
    provider: str = "telegram_stars"


class TopupResponse(BaseModel):
    invoice_id: int
    provider: str
    pay_url: str
    amount: float
    currency: str


@webapp_router.post("/topup", response_model=TopupResponse)
@limiter.limit("10/minute")
def webapp_topup(
    request: Request,
    body: TopupRequest,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Create a balance topup invoice.

    The invoice has ``kind='topup'`` and no ``plan_id`` — when it gets
    paid, ``_mark_invoice_paid_core`` credits the user's balance instead
    of provisioning a subscription. Same payment provider plumbing as
    the legacy ``/checkout`` flow, so Stars and CryptoBot work without
    changes.
    """
    from .services import balance as balance_svc

    if body.amount_kopecks < balance_svc.MIN_TOPUP_KOPECKS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Minimum topup is {balance_svc.MIN_TOPUP_KOPECKS // 100} ₽"
            ),
        )

    amount_rub = body.amount_kopecks / 100
    if body.provider == "telegram_stars":
        amount = float(_rub_to_stars(amount_rub))
        currency = "XTR"
    else:
        amount = amount_rub
        currency = "RUB"

    invoice = models.Invoice(
        user_id=user.id,
        plan_id=None,
        amount=amount_rub,  # Always store in RUB so the topup hook
                             # credits a clean kopeck count regardless
                             # of which provider's currency we displayed.
        currency=currency,
        action=models.InvoiceAction.new_subscription,
        kind="topup",
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    try:
        provider = get_provider(body.provider)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        provider_invoice = provider.create_invoice(
            invoice_id=invoice.id,
            amount=amount,
            currency=currency,
            # Stage 9d: neutral description (no "VPN" / no "topup" literal).
            description=f"Order #{invoice.id}",
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"payment provider error: {exc}")

    payment = models.Payment(
        invoice_id=invoice.id,
        amount=amount_rub,
        currency=currency,
        status=models.PaymentStatus.pending,
        provider=provider.name,
        external_id=provider_invoice.external_id,
    )
    db.add(payment)
    db.commit()

    return TopupResponse(
        invoice_id=invoice.id,
        provider=provider.name,
        pay_url=provider_invoice.pay_url,
        amount=provider_invoice.amount,
        currency=provider_invoice.currency,
    )


class ActivateRequest(BaseModel):
    plan_id: int


class TrialActivateWebAppResponse(BaseModel):
    trial_amount_kopecks: int
    referral_bonus_kopecks: int
    balance_kopecks: int
    trial_expires_at: str


@webapp_router.post("/trial/activate", response_model=TrialActivateWebAppResponse)
@limiter.limit("5/minute")
def webapp_activate_trial(
    request: Request,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Grant the one-time trial bonus to the authenticated WebApp user.

    Thin wrapper around :func:`services.trial.activate_trial` — the
    admin-token entrypoint in ``api_extensions.py`` shares the same
    service function, so WebApp and bot paths can't drift. Returns
    409 on a repeat tap so the UI knows to hide the banner.
    """
    from .services import trial as trial_svc

    try:
        result = trial_svc.activate_trial(db, user.id)
    except trial_svc.TrialAlreadyActivated:
        raise HTTPException(status_code=409, detail="Trial already activated")
    except trial_svc.NoTrialPlan:
        raise HTTPException(status_code=503, detail="No trial plan configured")
    db.commit()
    return TrialActivateWebAppResponse(
        trial_amount_kopecks=result.trial_amount_kopecks,
        referral_bonus_kopecks=result.referral_bonus_kopecks,
        balance_kopecks=result.balance_kopecks,
        trial_expires_at=result.trial_expires_at.isoformat(),
    )


# ── Хелперы prorated-рефанда для single-sub активации ───────────────
# Живут здесь (а не в services/balance.py), потому что нужны только
# webapp-флоу «купить план поверх действующей подписки». Расчёт — тот
# же, что в balance.change_plan: floor(price * remaining / duration).

def _prorated_sub_refund_kopecks(sub: models.Subscription) -> int:
    """Остаток по подписке в копейках (0, если возвращать нечего).

    remaining_days капится duration_days плана, чтобы «удлинённый»
    expires_at (freeze добавляет FREEZE_DAYS) не дал рефанд больше
    цены плана.
    """
    import math

    from .services import balance as balance_svc

    plan = sub.plan
    if plan is None:
        return 0
    price = balance_svc.plan_price_kopecks(plan)
    now = utcnow_aware()
    if price <= 0 or not sub.expires_at or sub.expires_at <= now:
        return 0
    total_days = plan.duration_days or 1
    remaining_days = (sub.expires_at - now).total_seconds() / 86400
    remaining_days = min(max(remaining_days, 0.0), float(total_days))
    return int(math.floor(price * remaining_days / total_days))


def _refund_subscription_remainder(
    db: Session,
    sub: models.Subscription,
    *,
    reference: str,
    note: str | None = None,
) -> int:
    """Кредитует prorated-остаток подписки в кошелёк (kind=refund).

    Возвращает сумму в копейках (0 — если рефандить нечего). Пишет
    через balance._record_tx, который только flush-ит без commit —
    коммит на вызывающем, поэтому блок «refund + charge» в
    webapp_activate остаётся атомарным.
    """
    from .services import balance as balance_svc

    user = balance_svc._lock_user(db, sub.user_id)
    # Под блокировкой перечитываем подписку: параллельный запрос мог
    # уже рефанднуть и заблокировать её — второй рефанд не пишем.
    db.refresh(sub)
    if sub.status not in (
        models.SubscriptionStatus.active,
        models.SubscriptionStatus.frozen,
    ):
        return 0
    refund = _prorated_sub_refund_kopecks(sub)
    if refund <= 0:
        return 0
    balance_svc._record_tx(
        db, user,
        amount_kopecks=refund,
        kind=models.BalanceTxKind.refund,
        reference=reference,
        note=note,
    )
    return refund


class ActivateResponse(BaseModel):
    subscription_id: int
    sub_token: str | None
    expires_at: datetime
    balance_kopecks: int
    plan_price_kopecks: int
    plan_duration_days: int
    # Kopecks credited back to the wallet from prorated remainder(s)
    # of any subscription(s) that were terminated during this
    # activation. 0 on a first-time activation (user had no prior
    # active sub) or when the old sub had no unused period left.
    # The webapp surfaces this in the "subscription activated" screen
    # so the user visibly sees the refund they were warned about.
    refunded_from_previous_kopecks: int = 0


@webapp_router.post("/subscriptions/activate", response_model=ActivateResponse)
@limiter.limit("5/minute")
def webapp_activate(
    request: Request,
    body: ActivateRequest,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Buy a plan — **single-subscription model**.

    Invariant after success: the user has exactly one active/frozen
    subscription (the new one). If they already had an active/frozen
    sub on a **different** plan, we:

    1. Provision the new sub on ``plan``.
    2. Credit the prorated remainder of every existing active/frozen
       sub back to the wallet as ``kind=refund`` ledger rows.
    3. Charge the full new plan price (``activate_subscription``).
    4. Revoke every old sub via
       ``revoke_subscription_devices`` — same effect as admin's
       ``/subscriptions/{id}/disable``: ``status=blocked`` +
       devices revoked + ansible cleanup task enqueued. The old sub
       then disappears from ``/me`` (which filters out ``blocked``).

    If they re-click the plan they're already on, we 400 instead of
    silently creating a duplicate — the UI hides the activate button
    on the current plan, but we defend at the API layer too.

    On insufficient balance after the refund credit, we 402 with
    ``suggested_topup_kopecks`` so the frontend can pre-fill the
    topup modal. The user is always warned about the old-sub
    termination in the webapp confirm dialog *before* the request
    goes out (see ``webapp/src/pages/Plans.tsx``), so this endpoint
    trusts the caller has consented — no two-step "review then
    commit" flow.
    """
    from .services import balance as balance_svc
    from .services.provisioning import ProvisioningOrchestrator

    plan = db.get(models.Plan, body.plan_id)
    if not plan or not plan.is_visible:
        raise HTTPException(status_code=404, detail="Plan not found")

    plan_price = balance_svc.plan_price_kopecks(plan)
    if plan_price <= 0:
        raise HTTPException(
            status_code=400, detail="Plan has no price configured"
        )

    # Гонка двойной активации (двойной тап / ретрай сети): весь блок
    # «проверить existing_subs → провижининг → списание» — read-then-act.
    # Берём FOR UPDATE-блокировку на строку users ДО проверки, чтобы
    # параллельный запрос ждал здесь и увидел уже созданную подписку
    # (получит 400 «Already on plan»), а не активировал вторую и не
    # списал деньги дважды. refresh заодно перечитывает баланс.
    db.refresh(user, with_for_update=True)

    # Single-sub invariant: every active/frozen sub must be terminated
    # before the new one goes live. We look across *any* plan (not
    # just "different plan") so the 400 below fires for same-plan
    # re-clicks.
    existing_subs = (
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
    for existing in existing_subs:
        if existing.plan_id == plan.id:
            raise HTTPException(
                status_code=400,
                detail=f"Already on plan '{plan.name}'",
            )

    # Pre-flight balance check uses the projected balance *after* the
    # prorated refund is credited. That way a user with a ₽0 wallet but
    # a ₽300 refund pending on their old sub can still switch to a
    # ₽250 plan.
    refund_estimate = sum(
        _prorated_sub_refund_kopecks(s) for s in existing_subs
    )
    projected_balance = (user.balance_kopecks or 0) + refund_estimate
    if projected_balance < plan_price:
        shortfall = plan_price - projected_balance
        suggested = max(balance_svc.min_topup_kopecks(db), shortfall)
        raise HTTPException(
            status_code=402,
            detail={
                "code": "insufficient_balance",
                "balance_kopecks": user.balance_kopecks or 0,
                "required_kopecks": plan_price,
                "refund_estimate_kopecks": refund_estimate,
                "suggested_topup_kopecks": suggested,
            },
        )

    orchestrator = ProvisioningOrchestrator(db)
    try:
        sub, _task = orchestrator.provision_subscription(user, plan)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Atomic "refund + charge" block. Both go through _record_tx which
    # only flushes (no commit), so a ValueError from activate_subscription
    # rolls back the refund credit too. Same failure pattern as the
    # original webapp_activate: rollback, mark the just-provisioned sub
    # expired, and surface 402 to the caller.
    refunded_total = 0
    try:
        for old in existing_subs:
            old_name = old.plan.name if old.plan else "?"
            refunded_total += _refund_subscription_remainder(
                db, old,
                reference=f"switch:{sub.id}:{old.id}",
                note=f"switch {old_name} -> {plan.name}",
            )
        balance_svc.activate_subscription(
            db, user.id, sub, reference=f"activate:{sub.id}"
        )
    except ValueError as exc:
        db.rollback()
        sub.status = models.SubscriptionStatus.expired
        db.add(sub)
        db.commit()
        raise HTTPException(status_code=402, detail=str(exc))

    # Now revoke the old subs. revoke_subscription_devices commits
    # internally (per-device revoke_device calls commit), so this also
    # persists the refund + charge from the block above. If an
    # individual revoke raises (ansible task create failed, etc.) we
    # log and keep going — the new sub is already live, and the
    # reconciler will flag any stuck old sub on its next tick.
    for old in existing_subs:
        old_name = old.plan.name if old.plan else "?"
        try:
            orchestrator.revoke_subscription_devices(
                old, reason=f"user switched {old_name} -> {plan.name}"
            )
        except Exception as exc:
            logger.warning(
                "webapp.activate revoke_old_failed user=%s old_sub=%s: %s",
                user.id, old.id, exc,
            )

    db.commit()
    db.refresh(user)
    db.refresh(sub)

    # Return the primary device's per-device sub_token so the QR/link
    # exposes only that device's credentials (not the entire subscription).
    # Falls back to the subscription-level token for safety.
    primary_device = next(
        (d for d in sub.devices if d.sub_token),
        None,
    )
    token = primary_device.sub_token if primary_device else sub.sub_token

    return ActivateResponse(
        subscription_id=sub.id,
        sub_token=token,
        expires_at=sub.expires_at,
        balance_kopecks=user.balance_kopecks or 0,
        plan_price_kopecks=plan_price,
        plan_duration_days=plan.duration_days,
        refunded_from_previous_kopecks=refunded_total,
    )


# ── Toggle auto-renew ────────────────────────────────────────────────

class AutoRenewToggleRequest(BaseModel):
    auto_renew: bool


class AutoRenewToggleResponse(BaseModel):
    subscription_id: int
    auto_renew: bool


@webapp_router.post(
    "/subscriptions/{subscription_id}/auto_renew",
    response_model=AutoRenewToggleResponse,
)
def webapp_toggle_auto_renew(
    subscription_id: int,
    body: AutoRenewToggleRequest,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Toggle auto-renewal. Off = subscription expires at paid_until, no charge."""
    sub = db.get(models.Subscription, subscription_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status not in (
        models.SubscriptionStatus.active,
        models.SubscriptionStatus.frozen,
    ):
        raise HTTPException(
            status_code=400, detail="Cannot change auto-renew for this subscription"
        )

    sub.auto_renew = body.auto_renew
    db.add(sub)
    db.commit()

    return AutoRenewToggleResponse(
        subscription_id=sub.id,
        auto_renew=sub.auto_renew,
    )


# ── Change plan (proration) ──────────────────────────────────────────

class ChangePlanRequest(BaseModel):
    plan_id: int


class ChangePlanResponse(BaseModel):
    subscription_id: int
    new_plan_name: str
    expires_at: datetime
    refunded_kopecks: int
    charged_kopecks: int
    balance_kopecks: int


@webapp_router.post(
    "/subscriptions/{subscription_id}/change_plan",
    response_model=ChangePlanResponse,
)
def webapp_change_plan(
    subscription_id: int,
    body: ChangePlanRequest,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Switch plan with proration: refund remaining old, charge full new."""
    from .services import balance as balance_svc

    # Та же защита от гонки, что в webapp_activate: блокируем строку
    # users до чтения подписки, чтобы два параллельных change_plan не
    # прошли оба преflight и не списали/рефанднули дважды. Второй
    # запрос дождётся коммита первого и получит 400 «Already on this
    # plan» по свежему plan_id.
    db.refresh(user, with_for_update=True)

    sub = db.get(models.Subscription, subscription_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(status_code=400, detail="Subscription is not active")
    if sub.plan_id == body.plan_id:
        raise HTTPException(status_code=400, detail="Already on this plan")

    new_plan = db.get(models.Plan, body.plan_id)
    if not new_plan or not new_plan.is_visible:
        raise HTTPException(status_code=404, detail="Plan not found")

    new_price = balance_svc.plan_price_kopecks(new_plan)

    # Pre-flight: estimate proration refund to check if balance is enough.
    old_price = balance_svc.plan_price_kopecks(sub.plan)
    now = utcnow_aware()
    refund_estimate = 0
    if old_price > 0 and sub.expires_at and sub.expires_at > now:
        import math
        remaining_secs = (sub.expires_at - now).total_seconds()
        remaining_days = max(remaining_secs / 86400, 0)
        refund_estimate = int(math.floor(old_price * remaining_days / sub.plan.duration_days))

    # Account for extra device surcharge on downgrade: if the user has
    # more live devices than the new plan bundles, those devices become
    # paid slots immediately.
    live_devices = balance_svc._live_device_count(db, sub.id)
    new_bundled = new_plan.max_devices or 1
    overflow = max(live_devices - new_bundled, 0)
    device_surcharge = overflow * balance_svc.EXTRA_DEVICE_MONTHLY_KOPECKS

    total_required = new_price + device_surcharge
    projected_balance = (user.balance_kopecks or 0) + refund_estimate
    if projected_balance < total_required:
        shortfall = total_required - projected_balance
        raise HTTPException(
            status_code=402,
            detail={
                "code": "insufficient_balance",
                "balance_kopecks": user.balance_kopecks or 0,
                "required_kopecks": total_required,
                "refund_estimate_kopecks": refund_estimate,
                "device_surcharge_kopecks": device_surcharge,
                "suggested_topup_kopecks": max(
                    balance_svc.min_topup_kopecks(db), shortfall
                ),
            },
        )

    try:
        result = balance_svc.change_plan(db, sub, new_plan)
    except ValueError as exc:
        raise HTTPException(status_code=402, detail=str(exc))

    db.commit()
    db.refresh(user)
    db.refresh(sub)

    return ChangePlanResponse(
        subscription_id=sub.id,
        new_plan_name=new_plan.name,
        expires_at=sub.expires_at,
        refunded_kopecks=result["refunded_kopecks"],
        charged_kopecks=result["charged_kopecks"],
        balance_kopecks=user.balance_kopecks or 0,
    )


class MigrateNodeResponse(BaseModel):
    subscription_id: int
    old_node_id: int
    old_node_name: str
    new_node_id: int
    new_node_name: str
    new_node_region: str | None
    task_id: int


@webapp_router.post(
    "/subscriptions/{subscription_id}/migrate_node",
    response_model=MigrateNodeResponse,
)
def webapp_migrate_node(
    subscription_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Relocate an active subscription to a different VPN node.

    Same mechanics as the drain-tick migration used by stage 5 downscale:
    preserves ``sub_token`` so installed clients keep working (they just
    refetch ``/sub/{token}`` and pick up new host URIs on the next profile
    update). Old devices on the previous node are revoked in the
    background, a fresh device is provisioned on the newly chosen node.

    Fails with 503 if ``choose_node`` cannot find any eligible alternative
    in the plan's pools (e.g. user is already on the only available node).
    Free of charge — same plan, same expiry, just a different pop.
    """
    from .services.provisioning import ProvisioningOrchestrator

    sub = db.get(models.Subscription, subscription_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(
            status_code=400,
            detail=f"Subscription is {sub.status.value}, must be active to migrate",
        )
    if sub.node is None:
        raise HTTPException(status_code=400, detail="Subscription has no node")
    if sub.plan is None:
        raise HTTPException(status_code=400, detail="Subscription has no plan")

    old_node = sub.node
    orchestrator = ProvisioningOrchestrator(db)
    try:
        target, _device, task = orchestrator.migrate_subscription_to_new_node(sub)
    except RuntimeError as exc:
        # No eligible alternative node — tell the client so the UI can
        # show a friendly "no other nodes available" message instead of
        # a generic 500.
        logger.info(
            "webapp migrate_node failed for sub %s: %s", sub.id, exc
        )
        raise HTTPException(status_code=503, detail=str(exc))

    db.commit()
    db.refresh(sub)

    return MigrateNodeResponse(
        subscription_id=sub.id,
        old_node_id=old_node.id,
        old_node_name=old_node.name,
        new_node_id=target.id,
        new_node_name=target.name,
        new_node_region=target.region,
        task_id=task.id,
    )


class FreezeResponse(BaseModel):
    subscription_id: int
    status: str
    frozen_until: datetime | None
    can_freeze_again: bool


@webapp_router.post("/subscriptions/{subscription_id}/freeze", response_model=FreezeResponse)
def webapp_freeze(
    subscription_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Pause a subscription for FREEZE_DAYS days. 1 per calendar year."""
    from .services import balance as balance_svc

    sub = db.get(models.Subscription, subscription_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        balance_svc.freeze_subscription(db, sub)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(sub)

    return FreezeResponse(
        subscription_id=sub.id,
        status=sub.status.value,
        frozen_until=sub.frozen_until,
        can_freeze_again=False,  # just froze — can't freeze again this year
    )


class UnfreezeResponse(BaseModel):
    subscription_id: int
    status: str
    expires_at: datetime | None


@webapp_router.post("/subscriptions/{subscription_id}/unfreeze", response_model=UnfreezeResponse)
def webapp_unfreeze(
    subscription_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Resume a frozen subscription. Early unfreeze blocks re-freeze until next year."""
    from .services import balance as balance_svc

    sub = db.get(models.Subscription, subscription_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        balance_svc.unfreeze_subscription(db, sub, auto=False)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(sub)

    return UnfreezeResponse(
        subscription_id=sub.id,
        status=sub.status.value,
        expires_at=sub.expires_at,
    )


class CancelSubscriptionResponse(BaseModel):
    subscription_id: int
    auto_renew: bool
    expires_at: datetime | None


@webapp_router.post(
    "/subscriptions/{subscription_id}/cancel",
    response_model=CancelSubscriptionResponse,
)
def webapp_cancel_subscription(
    subscription_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Cancel = turn off auto_renew. Access continues until expires_at."""
    sub = db.get(models.Subscription, subscription_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status not in (
        models.SubscriptionStatus.active,
        models.SubscriptionStatus.frozen,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel subscription in status {sub.status.value}",
        )

    sub.auto_renew = False
    db.add(sub)
    db.commit()
    db.refresh(sub)

    return CancelSubscriptionResponse(
        subscription_id=sub.id,
        auto_renew=sub.auto_renew,
        expires_at=sub.expires_at,
    )


class AddDeviceResponse(BaseModel):
    subscription_id: int
    device_id: int
    device_count: int
    extra_device_slots: int
    charged_kopecks: int
    balance_kopecks: int


@webapp_router.post(
    "/subscriptions/{subscription_id}/devices",
    response_model=AddDeviceResponse,
)
def webapp_add_device(
    subscription_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Provision an additional device on an active subscription.

    V2 billing: the plan price covers up to ``plan.max_devices`` devices
    per period. Each device above that requires a paid slot at
    ``EXTRA_DEVICE_MONTHLY_KOPECKS`` per month; the slot is persisted
    on ``Subscription.extra_device_slots`` and billed on every future
    renewal until the user changes plan or cancels.

    Flow:

    1. ``capacity = plan.max_devices + sub.extra_device_slots`` is the
       free envelope (plan bundle + already-bought slots).
    2. If ``live_devices + 1 <= capacity`` → free, just provision
       (e.g. user removed a device earlier and is refilling).
    3. Otherwise pro-rate ``EXTRA_DEVICE_MONTHLY_KOPECKS`` for the
       remainder of the current period, check wallet, charge, bump
       ``extra_device_slots`` by 1, and provision.

    On insufficient balance we return **402** with
    ``suggested_topup_kopecks`` so the frontend can pop the topup
    modal (same pattern as ``/activate`` and ``/change_plan``).
    """
    from .services import balance as balance_svc
    from .services.provisioning import ProvisioningOrchestrator

    # FOR UPDATE-блокировка строки подписки на весь запрос: два
    # параллельных add_device (даблклик) сериализуются на этом локе,
    # поэтому second-запрос читает уже инкрементированный
    # extra_device_slots и не затирает чужое списание.
    sub = (
        db.query(models.Subscription)
        .filter(models.Subscription.id == subscription_id)
        .with_for_update()
        .first()
    )
    if not sub or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(
            status_code=400,
            detail=f"subscription is {sub.status.value}, must be active",
        )
    if sub.plan is None:
        raise HTTPException(status_code=400, detail="subscription has no plan")

    current_devices = balance_svc._live_device_count(db, sub.id)
    max_bundled = sub.plan.max_devices or 1
    current_slots = sub.extra_device_slots or 0
    capacity = max_bundled + current_slots
    new_device_count = current_devices + 1
    # Only charge if the new device pushes us outside the current paid
    # envelope. Re-filling a revoked slot (current_devices < capacity)
    # is free because the slot was already paid for.
    needs_new_slot = new_device_count > capacity

    fee = (
        balance_svc.prorated_extra_device_fee(sub)
        if needs_new_slot
        else 0
    )
    if fee > 0 and (user.balance_kopecks or 0) < fee:
        shortfall = fee - (user.balance_kopecks or 0)
        suggested = max(balance_svc.min_topup_kopecks(db), shortfall)
        raise HTTPException(
            status_code=402,
            detail={
                "code": "insufficient_balance",
                "balance_kopecks": user.balance_kopecks or 0,
                "required_kopecks": fee,
                "suggested_topup_kopecks": suggested,
                "hint": (
                    f"Дополнительное устройство — {fee / 100:.0f} ₽ "
                    f"за остаток текущего периода. Пополни баланс."
                ),
            },
        )

    orchestrator = ProvisioningOrchestrator(db)
    try:
        device, _task = orchestrator.reprovision_subscription(
            sub, device_name=f"device-{new_device_count}"
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    charged = 0
    if fee > 0:
        try:
            charged = balance_svc.charge_extra_device(
                db,
                user.id,
                sub,
                fee,
                reference=f"extra_device:{sub.id}:{device.id}",
            )
        except ValueError as exc:
            # Shouldn't normally hit — we pre-checked the balance above,
            # but another request may have just drained the wallet. Roll
            # back the provisioning we optimistically started.
            orchestrator.revoke_device(
                device, reason="add_device: insufficient balance", background=True
            )
            db.commit()
            raise HTTPException(status_code=402, detail=str(exc))
        # Слот уже проинкрементирован внутри charge_extra_device
        # (services/balance.py) — не перезаписываем current_slots + 1,
        # иначе конкурентная покупка второго слота была бы затёрта.

    db.commit()
    db.refresh(user)
    db.refresh(sub)

    return AddDeviceResponse(
        subscription_id=sub.id,
        device_id=device.id,
        device_count=new_device_count,
        extra_device_slots=sub.extra_device_slots or 0,
        charged_kopecks=charged,
        balance_kopecks=user.balance_kopecks or 0,
    )


# ── Device management (rename / remove) ──────────────────────────


class RenameDeviceRequest(BaseModel):
    name: str


class RenameDeviceResponse(BaseModel):
    device_id: int
    name: str


@webapp_router.patch(
    "/devices/{device_id}",
    response_model=RenameDeviceResponse,
)
def webapp_rename_device(
    device_id: int,
    body: RenameDeviceRequest,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Rename a device (user-facing label only, no infra changes)."""
    device = db.get(models.Device, device_id)
    if not device or device.user_id != user.id:
        raise HTTPException(status_code=404, detail="Device not found")

    name = body.name.strip()[:64]
    if not name:
        raise HTTPException(status_code=400, detail="Name must not be empty")

    device.name = name
    db.commit()
    return RenameDeviceResponse(device_id=device.id, name=device.name)


class RemoveDeviceResponse(BaseModel):
    device_id: int
    device_count: int
    extra_device_slots: int
    balance_kopecks: int


@webapp_router.delete(
    "/devices/{device_id}",
    response_model=RemoveDeviceResponse,
)
def webapp_remove_device(
    device_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Revoke a device and its credential on the VPN node.

    V3: removing a device also decrements ``extra_device_slots`` (if the
    device was in the paid overflow zone), so the monthly renewal cost
    goes down. No refund for the current period — the slot was already
    paid — but future renewals no longer include the surcharge.

    The last device on a subscription cannot be removed — that's
    effectively a subscription cancel, handled separately.
    """
    from .services import balance as balance_svc
    from .services.provisioning import ProvisioningOrchestrator

    device = db.get(models.Device, device_id)
    if not device or device.user_id != user.id:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.status == models.DeviceStatus.revoked:
        raise HTTPException(status_code=400, detail="Device already revoked")

    sub = device.subscription
    if not sub:
        raise HTTPException(status_code=400, detail="No linked subscription")

    live_count = balance_svc._live_device_count(db, sub.id)
    if live_count <= 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove the last device — cancel the subscription instead",
        )

    orchestrator = ProvisioningOrchestrator(db)
    orchestrator.revoke_device(device, reason="user_removed", background=True)

    # Free a paid slot if the removed device was in the overflow zone.
    bundled = (sub.plan.max_devices if sub.plan else 1) or 1
    if (sub.extra_device_slots or 0) > 0 and live_count > bundled:
        sub.extra_device_slots -= 1
        db.add(sub)

    db.commit()
    db.refresh(user)
    db.refresh(sub)

    return RemoveDeviceResponse(
        device_id=device.id,
        device_count=live_count - 1,
        extra_device_slots=sub.extra_device_slots or 0,
        balance_kopecks=user.balance_kopecks or 0,
    )


# ── Stage 5.5: ledger history + referral info ──────────────────────


class TransactionRow(BaseModel):
    id: int
    amount_kopecks: int
    kind: str
    reference: str | None
    note: str | None
    created_at: datetime


class TransactionsResponse(BaseModel):
    items: list[TransactionRow]
    has_more: bool


@webapp_router.get("/transactions", response_model=TransactionsResponse)
def webapp_transactions(
    limit: int = 50,
    offset: int = 0,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Paginated balance ledger for the History page."""
    limit = max(1, min(limit, 100))
    rows = (
        db.query(models.BalanceTransaction)
        .filter(models.BalanceTransaction.user_id == user.id)
        .order_by(models.BalanceTransaction.id.desc())
        .offset(offset)
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return TransactionsResponse(
        items=[
            TransactionRow(
                id=r.id,
                amount_kopecks=r.amount_kopecks,
                kind=r.kind.value,
                reference=r.reference,
                note=r.note,
                created_at=r.created_at,
            )
            for r in rows
        ],
        has_more=has_more,
    )


class ReferralInfoResponse(BaseModel):
    code: str | None
    bonus_kopecks: int
    invited_count: int
    earned_kopecks: int
    share_url: str | None


@webapp_router.get("/referral", response_model=ReferralInfoResponse)
def webapp_referral(
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Return the user's referral code (creating one on first call) +
    counters for the Home referral block."""
    import secrets

    from .services import balance as balance_svc

    code_row = (
        db.query(models.ReferralCode)
        .filter(models.ReferralCode.owner_id == user.id, models.ReferralCode.is_active == True)  # noqa: E712
        .order_by(models.ReferralCode.id.desc())
        .first()
    )
    if code_row is None:
        # Mint one. 6 chars urlsafe is enough; collisions are caught by
        # the unique index on the next attempt.
        for _ in range(5):
            candidate = secrets.token_urlsafe(5)[:6].upper()
            exists = (
                db.query(models.ReferralCode)
                .filter(models.ReferralCode.code == candidate)
                .first()
            )
            if not exists:
                code_row = models.ReferralCode(owner_id=user.id, code=candidate)
                db.add(code_row)
                db.commit()
                db.refresh(code_row)
                break

    invited = (
        db.query(models.User)
        .filter(models.User.referred_by_id == user.id)
        .count()
    )
    earned = (
        db.query(models.BalanceTransaction)
        .filter(
            models.BalanceTransaction.user_id == user.id,
            models.BalanceTransaction.kind == models.BalanceTxKind.bonus,
        )
        .with_entities(models.BalanceTransaction.amount_kopecks)
        .all()
    )
    earned_total = sum(int(row[0]) for row in earned if row[0] and row[0] > 0)

    bot_username = os.getenv("BOT_USERNAME")
    share_url = (
        f"https://t.me/{bot_username}?start=ref_{code_row.code}"
        if code_row and bot_username
        else None
    )

    return ReferralInfoResponse(
        code=code_row.code if code_row else None,
        bonus_kopecks=balance_svc.REFERRAL_BONUS_KOPECKS,
        invited_count=invited,
        earned_kopecks=earned_total,
        share_url=share_url,
    )


class HealthPingReportResponse(BaseModel):
    ok: bool
    subscription_id: int | None
    node_id: int | None
    # operator-routing P1 (operator_routing_roadmap.md): если юзера переселили,
    # отдаём report_id + инфо о новой ноде, чтобы webapp показал «поменяли
    # сервер» и спросил мобильного оператора (POST /webapp/report-operator).
    migrated: bool = False
    report_id: int | None = None
    target_node_name: str | None = None


@webapp_router.post("/health-ping-report", response_model=HealthPingReportResponse)
def webapp_health_ping_report(
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """User pressed 'VPN doesn't work' in the webapp.

    Records a self-reported bad answer in AuditLog so the admin
    `/health-pings` dashboard picks it up alongside prompted ones
    (from the scheduled bot ping). We attach it to the user's first
    active subscription if they have one, so per-node aggregation
    works; if not, we still persist the complaint without node_id.

    No rate-limit beyond the standard SlowAPI middleware — users
    clicking their own 'SOS' button are the ones we *want* to hear
    from. Legitimate spam is handled client-side (5-min disable
    after click).
    """
    sub = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.user_id == user.id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .order_by(models.Subscription.id.asc())
        .first()
    )
    node_id = sub.node_id if sub else None
    sub_id = sub.id if sub else None

    db.add(
        models.AuditLog(
            actor=str(user.id),
            actor_type=models.AuditActor.user,
            action="health_ping_response",
            target_type="subscription",
            target_id=sub_id,
            extra={
                "telegram_id": user.telegram_id,
                "answer": "bad",
                "node_id": node_id,
                "source": "self_reported",
            },
        )
    )

    # «Не работает» → сразу делаем юзеру то же, что админская «обновить
    # подписку»: переселяем на свободную ноду + БАНИМ проблемную для него +
    # краудсорс-эскалация «плохости» ноды. _do_failover сам throttle'ит
    # (5 мин/sub). Best-effort — не ломаем user-facing ответ.
    migrated = False
    report_id: int | None = None
    target_node_name: str | None = None
    if sub is not None:
        from .api.client_control import _do_failover

        try:
            res = _do_failover(db, sub, kind="user_reported", actor=f"user:{user.id}")
            if res.action == "migrated":
                # _do_failover уже закоммитил миграцию + OperatorNodeReport
                # (через внутренний _audit). Прокидываем report_id наверх,
                # чтобы webapp одним тапом проставил оператора.
                migrated = True
                report_id = res.report_id
                target_node_name = res.target_node_name
        except Exception:  # noqa: BLE001
            # Roll back a mid-migration failure so the trailing db.commit()
            # can't flush a half-migrated sub (inner commits already persisted
            # the audit/migration rows we care about).
            if db.is_active:
                db.rollback()
            logger.exception(
                "webapp health-ping-report: failover for sub %s failed", sub.id
            )

    db.commit()
    return HealthPingReportResponse(
        ok=True,
        subscription_id=sub_id,
        node_id=node_id,
        migrated=migrated,
        report_id=report_id,
        target_node_name=target_node_name,
    )


class WebappSetOperatorRequest(BaseModel):
    report_id: int
    operator: str


@webapp_router.post("/report-operator")
def webapp_set_operator(
    body: WebappSetOperatorRequest,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Юзер выбрал свой мобильный оператор после «VPN не работает» →
    проставляем его на OperatorNodeReport (operator-routing P1, см.
    operator_routing_roadmap.md). Репорт обязан принадлежать ЭТОМУ юзеру
    (anti-forge: чужой report_id не прокатит). Карьер вне таксономии → unknown.
    """
    from .api.client_control import _OPERATORS

    report = db.get(models.OperatorNodeReport, body.report_id)
    if report is None or report.user_id != user.id:
        raise HTTPException(status_code=404, detail="Report not found")
    report.operator = body.operator if body.operator in _OPERATORS else "unknown"
    db.commit()
    return {"report_id": report.id, "operator": report.operator}


class ReportBrokenDeviceRequest(BaseModel):
    device_id: int


@webapp_router.post("/report-broken-device", response_model=HealthPingReportResponse)
def webapp_report_broken_device(
    body: ReportBrokenDeviceRequest,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Per-device «ЭТО устройство не работает» (multi-device юзер выбрал одно).

    Перетряхиваем ноды ТОЛЬКО этого устройства (``failover_device``) — соседние
    девайсы не трогаем, ноду user-wide не баним. Репорт оператора пишем с
    ``device_id`` (матрица оператор×нода). Anti-forge: устройство обязано
    принадлежать юзеру. Затем webapp одним тапом проставляет карьер
    (``/webapp/report-operator``). См. operator_routing_roadmap.md.
    """
    from .services.provisioning import ProvisioningOrchestrator

    device = db.get(models.Device, body.device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.status in (models.DeviceStatus.disabled, models.DeviceStatus.revoked):
        raise HTTPException(status_code=400, detail="device is not active")

    sub_id = device.subscription_id
    db.add(
        models.AuditLog(
            actor=str(user.id),
            actor_type=models.AuditActor.user,
            action="health_ping_response",
            target_type="subscription",
            target_id=sub_id,
            extra={
                "telegram_id": user.telegram_id,
                "answer": "bad",
                "source": "self_reported",
                "scope": "device",
                "device_id": device.id,
            },
        )
    )

    migrated = False
    report_id: int | None = None
    target_node_name: str | None = None
    old_primary: int | None = None
    target = None
    try:
        target, new_device, _task, old_primary = ProvisioningOrchestrator(
            db
        ).failover_device(device)
    except RuntimeError:
        # Нет свежей ноды (всё исключено/нездорово) — аудит оставляем, миграции
        # нет; webapp покажет «попробуй позже».
        target = None
    except Exception:  # noqa: BLE001
        if db.is_active:
            db.rollback()
        logger.exception(
            "webapp report-broken-device: failover failed for device %s", device.id
        )
        target = None

    if target is not None:
        report = models.OperatorNodeReport(
            user_id=user.id,
            subscription_id=sub_id,
            device_id=new_device.id,
            operator=None,
            failed_node_id=old_primary,
            target_node_id=target.id,
            target_access_username=new_device.access_username,
            outcome="pending",
        )
        db.add(report)
        db.flush()
        migrated = True
        report_id = report.id
        target_node_name = target.name

    db.commit()
    return HealthPingReportResponse(
        ok=True,
        subscription_id=sub_id,
        node_id=old_primary,
        migrated=migrated,
        report_id=report_id,
        target_node_name=target_node_name,
    )
