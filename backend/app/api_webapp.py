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


class MeResponse(BaseModel):
    user: schemas.UserOut
    subscriptions: list[schemas.SubscriptionOut]


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
    user_out = schemas.UserOut(
        id=user.id,
        telegram_id=user.telegram_id,
        email=user.email,
        created_at=user.created_at,
        subscription_count=len(subs),
    )
    return MeResponse(user=user_out, subscriptions=subs)


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

    This is a thin wrapper over the existing ``POST /api/invoices`` +
    ``POST /api/invoices/{id}/checkout`` pair from ``api.py`` — we don't
    duplicate validation, we call the same provider abstraction so the
    bot's existing ``successful_payment`` handler picks up the result with
    zero changes. The bot already routes ``invoice_payload`` →
    ``/api/payments/webhook/telegram_stars`` → marks the Invoice paid →
    triggers provisioning, identical for bot-flow and webapp-flow invoices.
    """
    plan = db.get(models.Plan, body.plan_id)
    if not plan or not plan.is_visible:
        raise HTTPException(status_code=404, detail="Plan not found")

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
        amount=amount,
        currency=currency,
        action=models.InvoiceAction.new_subscription,
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
            description=f"VPN {plan.name}",
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
