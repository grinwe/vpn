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


class SubscriptionStatusOut(BaseModel):
    plan_name: str
    server_name: str
    expires_at: datetime
    is_active: bool
    proto_configs: List[CredentialOut]


class PaymentCreate(BaseModel):
    subscription_id: int
    amount: float
    currency: str = "USD"
    status: str = "pending"
    provider: str = "manual"
    external_id: str | None = None


class InvoiceCreate(BaseModel):
    telegram_id: str
    plan_id: int
    amount: float | None = None
    currency: str = "USD"


class InvoiceOut(BaseModel):
    id: int
    user_id: int
    plan_id: int
    amount: float
    currency: str
    status: str
    created_at: datetime

    class Config:
        orm_mode = True
