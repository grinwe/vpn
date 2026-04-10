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
import os
import time
from datetime import datetime
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import models, schemas
from .api import _subscriptions_for_user
from .config import get_settings
from .db import SessionLocal
from .services.payments.base import ProviderError, get_provider

webapp_router = APIRouter(prefix="/api/webapp", tags=["webapp"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Telegram initData verification ----------

def _verify_init_data(init_data: str, bot_token: str) -> dict:
    """Validate Telegram WebApp initData and return parsed fields.

    Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    Algorithm: split query-string, pull out ``hash``, sort the rest as
    ``key=value`` lines joined by ``\n``, HMAC-SHA256 it with a secret key
    derived as ``HMAC-SHA256("WebAppData", bot_token)``.
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

    # auth_date is unix seconds — reject anything older than 24h to limit
    # replay attacks even if a user accidentally pasted their initData
    # somewhere public.
    auth_date = int(parsed.get("auth_date", "0"))
    if auth_date and (time.time() - auth_date) > 86400:
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
def webapp_auth(body: AuthRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not set")
    if not settings.webapp_jwt_secret:
        raise HTTPException(status_code=500, detail="WEBAPP_JWT_SECRET not set")

    parsed = _verify_init_data(body.init_data, settings.bot_token)
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
    created_at: datetime | None


class SubscriptionWebAppExtra(BaseModel):
    """Per-subscription balance fields the canonical SubscriptionOut
    doesn't carry. Returned alongside it as a parallel list keyed by
    subscription id so we don't churn the schema."""
    subscription_id: int
    plan_name: str | None
    daily_rate_kopecks: int | None
    daily_cost_kopecks: int | None
    days_remaining: int | None
    next_charge_at: datetime | None
    frozen_until: datetime | None
    can_freeze: bool
    freeze_days_left_in_year: int
    device_count: int
    bundled_devices: int
    extra_device_daily_kopecks: int
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


def _build_subscription_extras(
    db: Session, user: models.User
) -> tuple[list[SubscriptionWebAppExtra], int | None]:
    """Compute per-sub balance metadata for the WebApp.

    Returns ``(extras, min_days_remaining)`` so the caller can populate
    both fields without re-walking the sub list. ``min_days_remaining``
    is None when the user has no balance-billed subs at all (i.e. only
    legacy invoice subs or none).
    """
    from .services import balance as balance_svc

    # Active or frozen — frozen subs aren't burning balance but the
    # user still wants to see "осталось N дней в году заморозки" on
    # their card. Expired/blocked are excluded — that's done.
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
        # Treat zero-device subs as 1-device for cost preview, mirroring
        # _daily_cost_kopecks. Otherwise the UI would show "∞ days" for
        # a freshly-created sub before its first device lands.
        billable_devices = max(live_devices, 1)
        daily_rate = plan.daily_rate_kopecks if plan else None
        # Use the canonical formula so the UI matches what the worker
        # tick will actually charge — base + extras over plan cap.
        try:
            daily_cost = (
                balance_svc._daily_cost_kopecks(plan, billable_devices) if plan else None
            )
        except RuntimeError:
            daily_cost = None
        # Runway = prepaid bucket + wallet fallback. charge_subscription
        # drains prepaid first, then pulls from the wallet as kind=spend,
        # so the UI needs to reflect both. For the typical 1-sub user
        # this is exact; multi-sub users will see a slightly optimistic
        # number since the wallet is shared, good enough for a hint.
        days = (
            balance_svc.sub_days_remaining(
                sub, billable_devices, user.balance_kopecks or 0
            )
            if plan and daily_rate
            else None
        )

        # Year-budget remaining for the freeze button copy
        current_year = utcnow_aware().year
        used = sub.frozen_days_used or 0
        if sub.frozen_year != current_year:
            used = 0
        days_left_in_year = max(
            balance_svc.FREEZE_YEAR_BUDGET_DAYS - used, 0
        )
        # Can freeze: only active subs with budget left for one full
        # period. Frozen subs can't be re-frozen until they unfreeze.
        can_freeze = (
            sub.status == models.SubscriptionStatus.active
            and days_left_in_year >= balance_svc.MAX_FREEZE_DAYS_PER_PERIOD
        )

        extras.append(
            SubscriptionWebAppExtra(
                subscription_id=sub.id,
                plan_name=plan.name if plan else None,
                daily_rate_kopecks=daily_rate,
                daily_cost_kopecks=daily_cost,
                days_remaining=days,
                next_charge_at=sub.next_charge_at,
                frozen_until=sub.frozen_until,
                can_freeze=can_freeze,
                freeze_days_left_in_year=days_left_in_year,
                device_count=live_devices,
                bundled_devices=(plan.max_devices if plan else 1) or 1,
                extra_device_daily_kopecks=balance_svc.EXTRA_DEVICE_DAILY_KOPECKS,
                devices=[
                    DeviceSummary(
                        id=d.id,
                        name=d.name or f"device-{d.id}",
                        status=d.status.value,
                        created_at=getattr(d, "created_at", None),
                    )
                    for d in live_device_rows
                ],
            )
        )

        if days is not None and sub.status == models.SubscriptionStatus.active:
            min_days = days if min_days is None else min(min_days, days)

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
        has_active_balance_sub=any(e.daily_rate_kopecks for e in extras),
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
def webapp_topup(
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
def webapp_activate_trial(
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


class ActivateResponse(BaseModel):
    subscription_id: int
    sub_token: str | None
    days_remaining: int
    balance_kopecks: int
    daily_cost_kopecks: int
    next_charge_at: datetime | None


@webapp_router.post("/subscriptions/activate", response_model=ActivateResponse)
def webapp_activate(
    body: ActivateRequest,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Buy a plan: upfront-debit the full price into ``sub.prepaid_kopecks``.

    Flow:
      1. Validate the plan is balance-billable (has a daily rate).
      2. Pre-flight: wallet must cover the *full* plan price (not just
         one day). Daily-only buys are gone — user commits for the
         whole plan window, freeze still lets them pause mid-window.
      3. Provision the subscription.
      4. Call ``balance.activate_prepaid`` which debits the wallet and
         credits ``sub.prepaid_kopecks`` in a single row-locked tx.
      5. Charge day 1 out of the fresh bucket so the tick anchors at
         now+24h.

    On insufficient balance returns 402 with the suggested topup
    amount so the WebApp can pre-fill the topup modal.
    """
    from .services import balance as balance_svc
    from .services.provisioning import ProvisioningOrchestrator
    from .time_utils import utcnow

    plan = db.get(models.Plan, body.plan_id)
    if not plan or not plan.is_visible:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan.daily_rate_kopecks or plan.daily_rate_kopecks <= 0:
        raise HTTPException(
            status_code=400,
            detail="This plan has no daily rate; use /checkout for one-shot billing",
        )

    plan_price = balance_svc.plan_price_kopecks(plan)
    if plan_price <= 0:
        raise HTTPException(
            status_code=400, detail="Plan has no price configured"
        )

    # Pre-flight: need the FULL plan price, not just one day. The actual
    # debit runs under a row lock inside activate_prepaid so concurrent
    # purchases can't double-spend.
    if (user.balance_kopecks or 0) < plan_price:
        shortfall = plan_price - (user.balance_kopecks or 0)
        suggested = max(balance_svc.MIN_TOPUP_KOPECKS, shortfall)
        raise HTTPException(
            status_code=402,
            detail={
                "code": "insufficient_balance",
                "balance_kopecks": user.balance_kopecks or 0,
                "required_kopecks": plan_price,
                "suggested_topup_kopecks": suggested,
            },
        )

    orchestrator = ProvisioningOrchestrator(db)
    try:
        sub, _task = orchestrator.provision_subscription(user, plan)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Commit the prepaid debit + anchor + day-1 charge together. If
    # activate_prepaid raises (someone drained the wallet between the
    # pre-flight and here) we have to unwind the provisioned sub.
    sub.next_charge_at = utcnow()
    db.add(sub)
    try:
        balance_svc.activate_prepaid(
            db, user.id, sub, reference=f"activate:{sub.id}"
        )
    except ValueError as exc:
        db.rollback()
        # Don't leave an orphaned sub — revoke its device and mark
        # expired so run_renewal_check cleans up.
        sub.status = models.SubscriptionStatus.expired
        sub.next_charge_at = None
        sub.prepaid_kopecks = 0
        db.add(sub)
        db.commit()
        raise HTTPException(status_code=402, detail=str(exc))

    # Day 1 charge out of the fresh bucket.
    balance_svc.charge_subscription(db, sub)
    db.commit()
    db.refresh(user)
    db.refresh(sub)

    return ActivateResponse(
        subscription_id=sub.id,
        sub_token=sub.sub_token,
        days_remaining=balance_svc.sub_days_remaining(sub, 1, user.balance_kopecks or 0),
        balance_kopecks=user.balance_kopecks or 0,
        daily_cost_kopecks=plan.daily_rate_kopecks,
        next_charge_at=sub.next_charge_at,
    )


class FreezeResponse(BaseModel):
    subscription_id: int
    status: str
    frozen_until: datetime | None
    freeze_days_left_in_year: int


@webapp_router.post("/subscriptions/{subscription_id}/freeze", response_model=FreezeResponse)
def webapp_freeze(
    subscription_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Pause a subscription. See ``services.balance.freeze_subscription``."""
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

    used = sub.frozen_days_used or 0
    return FreezeResponse(
        subscription_id=sub.id,
        status=sub.status.value,
        frozen_until=sub.frozen_until,
        freeze_days_left_in_year=max(
            balance_svc.FREEZE_YEAR_BUDGET_DAYS - used, 0
        ),
    )


class UnfreezeResponse(BaseModel):
    subscription_id: int
    status: str
    next_charge_at: datetime | None


@webapp_router.post("/subscriptions/{subscription_id}/unfreeze", response_model=UnfreezeResponse)
def webapp_unfreeze(
    subscription_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Resume a frozen subscription."""
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
        next_charge_at=sub.next_charge_at,
    )


class CancelSubscriptionResponse(BaseModel):
    subscription_id: int
    status: str
    refunded_kopecks: int
    balance_kopecks: int


@webapp_router.post(
    "/subscriptions/{subscription_id}/cancel",
    response_model=CancelSubscriptionResponse,
)
def webapp_cancel_subscription(
    subscription_id: int,
    user: models.User = Depends(require_webapp_user),
    db: Session = Depends(get_db),
):
    """Cancel (unsubscribe): revoke devices, refund prepaid remainder, expire."""
    from .services import balance as balance_svc
    from .services.provisioning import ProvisioningOrchestrator

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

    # Revoke all live devices on the node.
    orchestrator = ProvisioningOrchestrator(db)
    for device in list(sub.devices):
        if device.status in (
            models.DeviceStatus.revoked,
            models.DeviceStatus.disabled,
        ):
            continue
        try:
            orchestrator.revoke_device(
                device, reason=f"user cancelled sub {sub.id}", background=True
            )
        except Exception:
            pass  # best-effort; device will be cleaned up by worker

    # Refund unused prepaid back to wallet.
    refunded = balance_svc.refund_prepaid(
        db, sub, reference=f"cancel:{sub.id}"
    )

    sub.status = models.SubscriptionStatus.expired
    sub.next_charge_at = None
    sub.frozen_at = None
    sub.frozen_until = None
    db.add(sub)
    db.commit()
    db.refresh(user)

    return CancelSubscriptionResponse(
        subscription_id=sub.id,
        status=sub.status.value,
        refunded_kopecks=refunded,
        balance_kopecks=user.balance_kopecks or 0,
    )


class AddDeviceResponse(BaseModel):
    subscription_id: int
    device_id: int
    device_count: int
    new_daily_cost_kopecks: int


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

    Stage 4 hitvpn-style: each device above ``plan.max_devices`` adds
    ``EXTRA_DEVICE_KOPECKS_PER_MONTH`` (≈100₽/mo) to the daily charge.
    Pre-flight: user must have at least one day of the *new* daily
    cost in balance — otherwise the very next charge tick would
    immediately flip the sub to expired.
    """
    from .services import balance as balance_svc
    from .services.provisioning import ProvisioningOrchestrator

    sub = db.get(models.Subscription, subscription_id)
    if not sub or sub.user_id != user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(
            status_code=400,
            detail=f"subscription is {sub.status.value}, must be active",
        )
    if sub.plan is None or sub.plan.daily_rate_kopecks is None:
        raise HTTPException(
            status_code=400, detail="plan has no daily rate (legacy plan)"
        )

    current_devices = balance_svc._live_device_count(db, sub.id)
    new_device_count = current_devices + 1
    new_daily_cost = balance_svc._daily_cost_kopecks(sub.plan, new_device_count)

    # Add-device doesn't touch the wallet — the extra cost drains the
    # sub's prepaid bucket faster. Gate on that bucket being able to
    # cover at least one day at the *new* rate; otherwise the next
    # charge tick immediately expires the sub.
    if (sub.prepaid_kopecks or 0) < new_daily_cost:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "insufficient_prepaid_for_device",
                "prepaid_kopecks": sub.prepaid_kopecks or 0,
                "required_kopecks": new_daily_cost,
                "hint": "Remainder of the current plan can't cover this device. "
                        "Buy a new plan or wait for the current one to renew.",
            },
        )

    orchestrator = ProvisioningOrchestrator(db)
    try:
        device, _task = orchestrator.reprovision_subscription(
            sub, device_name=f"device-{new_device_count}"
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    db.commit()
    db.refresh(sub)

    return AddDeviceResponse(
        subscription_id=sub.id,
        device_id=device.id,
        device_count=new_device_count,
        new_daily_cost_kopecks=new_daily_cost,
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
