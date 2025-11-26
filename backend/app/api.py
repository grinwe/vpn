from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from .db import SessionLocal
from . import models, schemas
from .services.provisioning import provision_subscription

router = APIRouter(prefix="/api")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/subscriptions", response_model=list[schemas.CredentialOut])
def create_subscription(payload: schemas.SubscriptionCreate, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, payload.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    user = db.query(models.User).filter_by(telegram_id=payload.telegram_id).first()
    if not user:
        user = models.User(telegram_id=payload.telegram_id, email=payload.email)
        db.add(user)
        db.commit()
        db.refresh(user)

    sub = provision_subscription(db, user, plan)
    db.commit()
    db.refresh(sub)
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
