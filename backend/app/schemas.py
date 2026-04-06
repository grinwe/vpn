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

    @classmethod
    def from_orm(cls, obj):  # type: ignore[override]
        # Transparently decrypt config_text on read.
        from .security import decrypt

        return cls(
            id=obj.id,
            proto=obj.proto,
            config_text=decrypt(obj.config_text) or "",
            device_id=getattr(obj, "device_id", None),
            config_id=getattr(obj, "config_id", None),
        )


class DeviceOut(BaseModel):
    id: int
    name: str
    status: str
    config_id: int
    access_username: str | None = None
    connection_uri: str | None = None

    class Config:
        orm_mode = True

    @classmethod
    def from_orm(cls, obj):  # type: ignore[override]
        from .security import decrypt

        return cls(
            id=obj.id,
            name=obj.name,
            status=obj.status.value if hasattr(obj.status, "value") else obj.status,
            config_id=obj.config_id,
            access_username=obj.access_username,
            connection_uri=decrypt(obj.connection_uri),
        )


class DeviceStatusOut(DeviceOut):
    credentials: list[CredentialOut] = []
    provisioning_task_id: int | None = None


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


class SubscriptionProvisionResponse(BaseModel):
    subscription_id: int
    status: str
    expires_at: datetime
    node_id: int
    plan_id: int
    device: DeviceStatusOut
    provisioning_task_id: int


class SubscriptionTrafficUpdate(BaseModel):
    used_mb: int = Field(..., ge=0, description="Traffic to add to the subscription usage in MB")


class SubscriptionTrafficOut(BaseModel):
    subscription_id: int
    status: str
    traffic_used_mb: int
    traffic_limit_mb: int | None = None
    over_limit: bool = False
    revocation_task_ids: list[int] = Field(default_factory=list)


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
    subscription_id: int | None = None
    amount: float | None = None
    currency: str = "USD"
    action: str = "new_subscription"


class InvoiceMarkPaidRequest(BaseModel):
    payment_id: int | None = None


class InvoiceOut(BaseModel):
    id: int
    user_id: int
    plan_id: int
    subscription_id: int | None = None
    amount: float
    currency: str
    status: str
    action: str
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
    subscription_id: int | None = None
    amount: float
    currency: str
    status: str
    action: str
    created_at: datetime


class InvoicePaidOut(InvoiceListItem):
    credentials: list[CredentialOut]
    provisioning_task_id: int | None = None
    device_id: int | None = None


class InvoiceCheckoutRequest(BaseModel):
    provider: str | None = Field(default=None, description="Payment provider name; defaults to server default")
    return_url: str | None = None


class InvoiceCheckoutOut(BaseModel):
    invoice_id: int
    provider: str
    external_id: str
    pay_url: str
    amount: float
    currency: str


class HealthProbeIn(BaseModel):
    source_region: str = Field(..., description="Probe origin label, e.g. 'ru-mts', 'kz', 'eu'")
    result: str = Field(..., description="ok|timeout|refused|tls_fail|unknown")
    latency_ms: int | None = None
    source_kind: str | None = None
    details: dict[str, Any] | None = None


class NodeTrafficSample(BaseModel):
    access_username: str = Field(..., description="Device.access_username the counter belongs to")
    uplink_bytes: int = Field(..., ge=0)
    downlink_bytes: int = Field(..., ge=0)


class NodeTrafficReport(BaseModel):
    collected_at: datetime = Field(..., description="Node-side wallclock when counters were read")
    window_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Length of the accounting window the samples cover, for diagnostics only",
    )
    samples: List[NodeTrafficSample]


class NodeTrafficSubscriptionResult(BaseModel):
    subscription_id: int
    used_mb_delta: int
    used_mb_total: int
    over_limit: bool
    revocation_task_ids: list[int] = Field(default_factory=list)


class NodeTrafficIngestOut(BaseModel):
    node_id: int
    accepted_samples: int
    unknown_usernames: list[str] = Field(default_factory=list)
    subscriptions: list[NodeTrafficSubscriptionResult] = Field(default_factory=list)


class ProbeTargetEndpoint(BaseModel):
    # One port to check on a node. A node with both ShadowTLS and VLESS
    # Reality will yield two endpoints. ``kind`` tells the agent which
    # check routine to run: plain TCP connect, or TLS handshake (with
    # ``sni`` as the expected SNI).
    protocol: str
    port: int
    kind: str = Field(..., description="tcp|tls")
    sni: str | None = None


class ProbeTarget(BaseModel):
    node_id: int
    name: str
    region: str
    host: str
    endpoints: list[ProbeTargetEndpoint]


class ProbeTargetList(BaseModel):
    generated_at: datetime
    targets: list[ProbeTarget]


class NodeHealthOut(BaseModel):
    node_id: int
    health_score: int
    blocked_regions: list[str] = Field(default_factory=list)
    overall_success_rate: float
    per_region: dict[str, float]
    migrated_subscriptions: list[int] = Field(default_factory=list)


class CloudProviderCreate(BaseModel):
    name: str
    kind: str
    api_token: str | None = None
    default_image: str | None = None
    default_region: str | None = None
    default_plan: str | None = None
    ssh_key_ids: list[str] | None = None
    is_active: bool = True


class CloudProviderOut(BaseModel):
    id: int
    name: str
    kind: str
    default_image: str | None
    default_region: str | None
    default_plan: str | None
    ssh_key_ids: list[str] | None
    is_active: bool
    created_at: datetime

    class Config:
        orm_mode = True


class PoolAutoscaleConfig(BaseModel):
    autoscale_enabled: bool | None = None
    autoscale_provider_id: int | None = None
    autoscale_region: str | None = None
    autoscale_plan: str | None = None
    autoscale_image: str | None = None
    autoscale_high_watermark: float | None = None
    autoscale_max_nodes: int | None = None


class PoolAutoscaleOut(BaseModel):
    pool_id: int
    pool_name: str
    autoscale_enabled: bool
    autoscale_provider_id: int | None
    autoscale_region: str | None
    autoscale_plan: str | None
    autoscale_image: str | None
    autoscale_high_watermark: float | None
    autoscale_max_nodes: int | None


class PoolDecisionOut(BaseModel):
    pool_id: int
    pool_name: str
    utilization: float
    total_capacity: int
    active_subs: int
    node_count: int
    scaled_up: bool
    new_node_id: int | None = None
    reason: str | None = None


class NodeSpawnRequest(BaseModel):
    provider_id: int
    name: str
    region: str
    plan: str
    image: str | None = None
    ssh_key_ids: list[str] | None = None
    pool_id: int | None = None
    user_data: str | None = None
    notes: str | None = None


class ProvisioningTaskOut(BaseModel):
    id: int
    target_type: str
    target_id: int
    action: str
    status: str
    payload: dict[str, Any] | None
    result: dict[str, Any] | None = None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    class Config:
        orm_mode = True


class ApiTokenCreate(BaseModel):
    name: str
    scopes: list[str]


class ApiTokenOut(BaseModel):
    id: int
    name: str
    scopes: list[str]
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None = None

    class Config:
        orm_mode = True


class ApiTokenCreatedOut(ApiTokenOut):
    # Plaintext token, shown only on creation. Store it somewhere safe.
    token: str


class StatsOut(BaseModel):
    users_total: int
    subscriptions_active: int
    subscriptions_total: int
    invoices_pending: int
    nodes_total: int
    nodes_active: int
    devices_active: int
    provisioning_tasks_pending: int
    provisioning_tasks_failed: int


class UserOut(BaseModel):
    id: int
    telegram_id: str | None = None
    email: str | None = None
    created_at: datetime
    subscription_count: int = 0

    class Config:
        orm_mode = True
