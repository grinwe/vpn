import enum

from .time_utils import utcnow
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import relationship
from .db import Base


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    blocked = "blocked"
    expired = "expired"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"
    refunded = "refunded"


class InvoiceStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"


class InvoiceAction(str, enum.Enum):
    new_subscription = "new_subscription"
    renewal = "renewal"


class VPNNodeStatus(str, enum.Enum):
    registering = "registering"
    active = "active"
    disabled = "disabled"
    error = "error"


class VPNConfigProtocol(str, enum.Enum):
    shadowtls_ss = "shadowtls+shadowsocks"
    vless_reality = "vless-reality"
    vless_ws_cdn = "vless-ws-cdn"
    hysteria2 = "hysteria2"


class DeviceStatus(str, enum.Enum):
    pending = "pending"
    failed = "failed"
    active = "active"
    disabled = "disabled"
    revoked = "revoked"


class ProvisioningTaskStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"


class AuditActor(str, enum.Enum):
    user = "user"
    admin = "admin"
    system = "system"


class ProbeResult(str, enum.Enum):
    ok = "ok"
    timeout = "timeout"
    refused = "refused"
    tls_fail = "tls_fail"
    unknown = "unknown"


class CloudProviderKind(str, enum.Enum):
    hetzner = "hetzner"
    vultr = "vultr"
    digitalocean = "digitalocean"
    manual = "manual"


plan_serverpool = Table(
    "plan_serverpool",
    Base.metadata,
    Column("plan_id", Integer, ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "server_pool_id",
        Integer,
        ForeignKey("server_pools.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class ServerPool(Base):
    __tablename__ = "server_pools"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)

    autoscale_enabled = Column(Boolean, default=False)
    autoscale_provider_id = Column(Integer, ForeignKey("cloud_providers.id"), nullable=True)
    autoscale_region = Column(String, nullable=True)
    autoscale_plan = Column(String, nullable=True)
    autoscale_image = Column(String, nullable=True)
    autoscale_high_watermark = Column(Numeric(4, 3), nullable=True)
    autoscale_max_nodes = Column(Integer, nullable=True)

    nodes = relationship("VPNNode", back_populates="pool")
    plans = relationship("Plan", secondary=plan_serverpool, back_populates="server_pools")


class VPNNode(Base):
    __tablename__ = "vpn_nodes"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    region = Column(String, nullable=False)
    host = Column(String, nullable=False)
    ssh_port = Column(Integer, default=22)
    status = Column(Enum(VPNNodeStatus), default=VPNNodeStatus.registering)
    is_active = Column(Boolean, default=True)
    pool_id = Column(Integer, ForeignKey("server_pools.id"))
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    notes = Column(Text)

    max_users = Column(Integer, nullable=True)
    max_bandwidth_mbps = Column(Integer, nullable=True)
    health_score = Column(Integer, default=100)
    last_health_check_at = Column(DateTime, nullable=True)
    blocked_regions = Column(JSONB, nullable=True)
    cooldown_until = Column(DateTime, nullable=True)

    provider_id = Column(Integer, ForeignKey("cloud_providers.id"), nullable=True)
    provider_external_id = Column(String, nullable=True)
    provider_region = Column(String, nullable=True)
    provider_plan = Column(String, nullable=True)
    monthly_cost = Column(Numeric(10, 2), nullable=True)

    pool = relationship("ServerPool", back_populates="nodes")
    configs = relationship("VPNConfig", back_populates="node", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="node")
    provider = relationship("CloudProvider", back_populates="nodes")
    probes = relationship("HealthProbe", back_populates="node", cascade="all, delete-orphan")


class VPNConfig(Base):
    __tablename__ = "vpn_configs"

    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, ForeignKey("vpn_nodes.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    protocol = Column(Enum(VPNConfigProtocol), nullable=False)
    port = Column(Integer, nullable=False)
    sni = Column(String, nullable=True)
    public_key = Column(String, nullable=True)
    fallback = Column(String, nullable=True)
    settings = Column(JSONB, nullable=True)
    is_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    node = relationship("VPNNode", back_populates="configs")
    credentials = relationship("Credential", back_populates="config")
    devices = relationship("Device", back_populates="config")


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    duration_days = Column(Integer, nullable=False)
    max_devices = Column(Integer, default=1)
    price = Column(Numeric(10, 2), default=0)
    traffic_limit_mb = Column(Integer, nullable=True)
    is_visible = Column(Boolean, default=True)
    server_pools = relationship("ServerPool", secondary=plan_serverpool, back_populates="plans")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    referred_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    invoices = relationship("Invoice", back_populates="user")
    devices = relationship("Device", back_populates="user")
    referral_codes = relationship(
        "ReferralCode", back_populates="owner", foreign_keys="ReferralCode.owner_id"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    node_id = Column(Integer, ForeignKey("vpn_nodes.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    expires_at = Column(DateTime, nullable=False)
    status = Column(Enum(SubscriptionStatus), default=SubscriptionStatus.active)
    notes = Column(Text)
    traffic_limit_mb = Column(Integer, nullable=True)
    traffic_used_mb = Column(Integer, default=0)
    auto_renew = Column(Boolean, default=False)
    # Stable token for the dynamic subscription link — survives migrations.
    sub_token = Column(String, unique=True, index=True, nullable=True)

    user = relationship("User")
    plan = relationship("Plan")
    node = relationship("VPNNode", back_populates="subscriptions")
    credentials = relationship("Credential", back_populates="subscription")
    devices = relationship("Device", back_populates="subscription")
    payments = relationship("Payment", back_populates="subscription")


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False)
    config_id = Column(Integer, ForeignKey("vpn_configs.id"), nullable=False)
    name = Column(String, nullable=False)
    status = Column(Enum(DeviceStatus), default=DeviceStatus.pending)
    access_username = Column(String, nullable=True)
    connection_uri = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    last_seen_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="devices")
    subscription = relationship("Subscription", back_populates="devices")
    config = relationship("VPNConfig", back_populates="devices")
    credentials = relationship("Credential", back_populates="device")


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=False)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    config_id = Column(Integer, ForeignKey("vpn_configs.id"), nullable=True)
    proto = Column(String, nullable=False)
    config_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    is_active = Column(Boolean, default=True)
    revoked_at = Column(DateTime, nullable=True)

    subscription = relationship("Subscription", back_populates="credentials")
    device = relationship("Device", back_populates="credentials")
    config = relationship("VPNConfig", back_populates="credentials")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True, index=True)
    amount = Column(Numeric(10, 2), default=0)
    currency = Column(String, default="USD")
    status = Column(Enum(PaymentStatus), default=PaymentStatus.pending)
    provider = Column(String, default="manual")
    external_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    subscription = relationship("Subscription", back_populates="payments")
    invoice = relationship("Invoice", foreign_keys=[invoice_id])


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    amount = Column(Numeric(10, 2), default=0)
    currency = Column(String, default="USD")
    status = Column(Enum(InvoiceStatus), default=InvoiceStatus.pending)
    action = Column(Enum(InvoiceAction), default=InvoiceAction.new_subscription)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="invoices")
    plan = relationship("Plan")
    subscription = relationship("Subscription")


class ProvisioningTask(Base):
    __tablename__ = "provisioning_tasks"

    id = Column(Integer, primary_key=True)
    target_type = Column(String, nullable=False)
    target_id = Column(Integer, nullable=False)
    action = Column(String, nullable=False)
    status = Column(Enum(ProvisioningTaskStatus), default=ProvisioningTaskStatus.pending)
    payload = Column(JSONB, nullable=True)
    result = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class CloudProvider(Base):
    __tablename__ = "cloud_providers"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    kind = Column(Enum(CloudProviderKind), nullable=False)
    api_token_enc = Column(Text, nullable=True)
    default_image = Column(String, nullable=True)
    ssh_key_ids = Column(JSONB, nullable=True)
    default_region = Column(String, nullable=True)
    default_plan = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    nodes = relationship("VPNNode", back_populates="provider")


class HealthProbe(Base):
    __tablename__ = "health_probes"

    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, ForeignKey("vpn_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    source_region = Column(String, nullable=False, index=True)
    source_kind = Column(String, nullable=True)
    result = Column(Enum(ProbeResult), nullable=False)
    latency_ms = Column(Integer, nullable=True)
    observed_at = Column(DateTime, default=utcnow, index=True)
    details = Column(JSONB, nullable=True)

    node = relationship("VPNNode", back_populates="probes")


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    scopes = Column(ARRAY(String), nullable=False, default=list)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    actor = Column(String, nullable=False)
    actor_type = Column(Enum(AuditActor), default=AuditActor.system)
    action = Column(String, nullable=False)
    target_type = Column(String, nullable=False)
    target_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    extra = Column("metadata", JSONB, nullable=True)


class ReferralCode(Base):
    """Referral invite code owned by a user.

    When a new user registers via a referral link containing this code,
    the invitee gets ``bonus_days`` added to their first subscription and
    the owner gets ``reward_days`` added to their active subscription.
    """
    __tablename__ = "referral_codes"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code = Column(String(32), unique=True, nullable=False, index=True)
    bonus_days = Column(Integer, default=3)
    reward_days = Column(Integer, default=3)
    uses = Column(Integer, default=0)
    max_uses = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    owner = relationship("User", back_populates="referral_codes", foreign_keys=[owner_id])
