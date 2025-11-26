from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel


class CredentialOut(BaseModel):
    proto: str
    config_text: str

    class Config:
        orm_mode = True


class SubscriptionCreate(BaseModel):
    telegram_id: str
    plan_id: int
    email: Optional[str] = None


class SubscriptionOut(BaseModel):
    id: int
    plan_name: str
    server: str
    expires_at: datetime
    status: str
    credentials: List[CredentialOut]


class DisableRequest(BaseModel):
    reason: str | None = None


class PaymentCreate(BaseModel):
    subscription_id: int
    amount: float
    currency: str = "USD"
    status: str = "pending"
    provider: str = "manual"
    external_id: str | None = None
