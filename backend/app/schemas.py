from datetime import datetime
from typing import Any, List, Optional
from pydantic import BaseModel, Field


class CredentialOut(BaseModel):
    id: int
    proto: str
    config_text: str
    device_id: int | None = None
    config_id: int | None = None

    class Config:
        orm_mode = True


class DeviceOut(BaseModel):
    id: int
    name: str
    status: str
    config_id: int
    access_username: str | None = None
    connection_uri: str | None = None

    class Config:
        orm_mode = True


class VPNConfigCreate(BaseModel):
    name: str
    protocol: str
    port: int
    sni: str | None = None
    public_key: str | None = None
    fallback: str | None = None
    settings: dict[str, Any] | None = None
    is_enabled: bool = True


class VPNConfigOut(VPNConfigCreate):
    id: int
    node_id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class VPNNodeCreate(BaseModel):
    name: str
    region: str
    host: str
    ssh_port: int = 22
    pool_id: int | None = None
    notes: str | None = None


class VPNNodeOut(VPNNodeCreate):
    id: int
    status: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class SubscriptionCreate(BaseModel):
    telegram_id: str
    plan_id: int
    email: Optional[str] = None
    node_id: int | None = None
    device_name: str | None = Field(default=None, description="Human readable device label")


class PlanOut(BaseModel):
    id: int
    name: str
    duration_days: int
    max_devices: int
    price: float
    traffic_limit_mb: int | None = None

    class Config:
        orm_mode = True


class SubscriptionOut(BaseModel):
    id: int
    plan_name: str
    node: str
    region: str
    expires_at: datetime
    status: str
    credentials: List[CredentialOut]
    devices: List[DeviceOut] = []


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
    user_id: int | None = None
    telegram_id: str | None = None
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
    updated_at: datetime

    class Config:
        orm_mode = True


class InvoiceListItem(BaseModel):
    id: int
    user_id: int
    user_telegram_id: str | None
    plan_id: int
    plan_name: str
    amount: float
    currency: str
    status: str
    created_at: datetime


class InvoicePaidOut(InvoiceListItem):
    credentials: list[CredentialOut]


class ProvisioningTaskOut(BaseModel):
    id: int
    target_type: str
    target_id: int
    action: str
    status: str
    payload: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    class Config:
        orm_mode = True
