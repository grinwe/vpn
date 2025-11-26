from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import logging
from .db import SessionLocal
from . import models, schemas
from .services.provisioning import (
    deprovision_shadowtls_ss_user,
    provision_subscription,
)

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_or_create_user(db: Session, telegram_id: str, email: str | None = None) -> models.User:
    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        user = models.User(telegram_id=telegram_id, email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _create_subscription_for_user(db: Session, user: models.User, plan: models.Plan) -> models.Subscription:
    try:
        with db.begin():
            sub = provision_subscription(db, user, plan)
        db.refresh(sub)
        return sub
    except Exception as exc:  # noqa: BLE001
        logger.exception("Provisioning failed for user %s", user.telegram_id)
        db.rollback()
        raise HTTPException(status_code=500, detail="Provisioning failed") from exc


def _invoice_with_credentials(
    invoice: models.Invoice,
    credentials: list[models.Credential] | None = None,
) -> schemas.InvoicePaidOut:
    creds = credentials or []
    return schemas.InvoicePaidOut(
        id=invoice.id,
        user_id=invoice.user_id,
        user_telegram_id=invoice.user.telegram_id if invoice.user else None,
        plan_id=invoice.plan_id,
        plan_name=invoice.plan.name if invoice.plan else "",
        amount=float(invoice.amount),
        currency=invoice.currency,
        status=invoice.status.value,
        created_at=invoice.created_at,
        credentials=[schemas.CredentialOut.from_orm(c) for c in creds],
    )


@router.post("/subscriptions", response_model=list[schemas.CredentialOut])
def create_subscription(payload: schemas.SubscriptionCreate, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, payload.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    user = _get_or_create_user(db, payload.telegram_id, payload.email)
    sub = _create_subscription_for_user(db, user, plan)
    return [schemas.CredentialOut.from_orm(cred) for cred in sub.credentials]


@router.post("/users/{user_id}/disable")
def disable_user(user_id: int, body: schemas.DisableRequest, db: Session = Depends(get_db)):
    subs = db.query(models.Subscription).filter(models.Subscription.user_id == user_id).all()
    if not subs:
        raise HTTPException(status_code=404, detail="User or subscriptions not found")
    for sub in subs:
        sub.status = models.SubscriptionStatus.blocked
        sub.notes = body.reason
    db.commit()
    return {"disabled": len(subs)}


@router.get("/users/{user_id}", response_model=list[schemas.SubscriptionOut])
def get_user(user_id: int, db: Session = Depends(get_db)):
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
            server=sub.server.name,
            expires_at=sub.expires_at,
            status=sub.status.value,
            credentials=[schemas.CredentialOut.from_orm(c) for c in sub.credentials],
        )
        result.append(item)
    return result


@router.post("/subscriptions/{subscription_id}/disable")
def disable_subscription(subscription_id: int, db: Session = Depends(get_db)):
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != models.SubscriptionStatus.active:
        raise HTTPException(status_code=400, detail="Subscription is not active")

    username = f"user-{sub.user_id}-{sub.id}"
    try:
        deprovision_shadowtls_ss_user(sub.server, username)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Deprovision failed for subscription %s", sub.id)
        raise HTTPException(status_code=500, detail="Deprovision failed") from exc

    sub.status = models.SubscriptionStatus.blocked
    sub.expires_at = datetime.utcnow()
    db.commit()
    return {"subscription_id": sub.id, "status": sub.status.value}


@router.get("/subscriptions/{subscription_id}/status", response_model=schemas.SubscriptionStatusOut)
def get_subscription_status(subscription_id: int, db: Session = Depends(get_db)):
    sub = db.get(models.Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    is_active = sub.status == models.SubscriptionStatus.active and sub.expires_at > datetime.utcnow()
    return schemas.SubscriptionStatusOut(
        plan_name=sub.plan.name,
        server_name=sub.server.name,
        expires_at=sub.expires_at,
        is_active=is_active,
        proto_configs=[schemas.CredentialOut.from_orm(c) for c in sub.credentials],
    )


@router.post("/payments")
def create_payment(body: schemas.PaymentCreate, db: Session = Depends(get_db)):
    sub = db.get(models.Subscription, body.subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    payment = models.Payment(
        subscription_id=sub.id,
        amount=body.amount,
        currency=body.currency,
        status=models.PaymentStatus(body.status),
        provider=body.provider,
        external_id=body.external_id,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return {"id": payment.id, "status": payment.status.value}


@router.post("/invoices", response_model=schemas.InvoiceOut)
def create_invoice(body: schemas.InvoiceCreate, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, body.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    user = _get_or_create_user(db, body.telegram_id)
    amount = body.amount if body.amount is not None else float(plan.price)
    invoice = models.Invoice(
        user_id=user.id,
        plan_id=plan.id,
        amount=amount,
        currency=body.currency,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return schemas.InvoiceOut.from_orm(invoice)


@router.get("/plans", response_model=list[schemas.PlanOut])
def list_plans(db: Session = Depends(get_db)):
    plans = db.query(models.Plan).order_by(models.Plan.id).all()
    return [schemas.PlanOut.from_orm(plan) for plan in plans]


@router.get("/invoices", response_model=list[schemas.InvoiceListItem])
def list_invoices(status: str | None = None, limit: int = 10, db: Session = Depends(get_db)):
    query = db.query(models.Invoice).order_by(models.Invoice.created_at.desc())
    if status:
        try:
            invoice_status = models.InvoiceStatus(status)
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid status") from exc
        query = query.filter(models.Invoice.status == invoice_status)

    invoices = query.limit(limit).all()
    result: list[schemas.InvoiceListItem] = []
    for inv in invoices:
        result.append(
            schemas.InvoiceListItem(
                id=inv.id,
                user_id=inv.user_id,
                user_telegram_id=inv.user.telegram_id if inv.user else None,
                plan_id=inv.plan_id,
                plan_name=inv.plan.name if inv.plan else "",
                amount=float(inv.amount),
                currency=inv.currency,
                status=inv.status.value,
                created_at=inv.created_at,
            )
        )
    return result


@router.post("/invoices/{invoice_id}/mark_paid", response_model=schemas.InvoicePaidOut)
def mark_invoice_paid(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.get(models.Invoice, invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status == models.InvoiceStatus.paid:
        subscription = (
            db.query(models.Subscription)
            .filter(models.Subscription.user_id == invoice.user_id)
            .order_by(models.Subscription.created_at.desc())
            .first()
        )
        credentials = subscription.credentials if subscription else []
        return _invoice_with_credentials(invoice, credentials)

    invoice.status = models.InvoiceStatus.paid
    db.commit()
    db.refresh(invoice)

    plan = db.get(models.Plan, invoice.plan_id)
    user = db.get(models.User, invoice.user_id)
    credentials: list[models.Credential] = []
    if plan and user:
        sub = _create_subscription_for_user(db, user, plan)
        credentials = sub.credentials

    return _invoice_with_credentials(invoice, credentials)
