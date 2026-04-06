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

    # Autoscale configuration. ``autoscale_enabled`` acts as the master
    # switch; when off, the scheduler ignores this pool entirely. The other
    # fields are defaults passed to :func:`services.node_spawner.spawn_node`
    # when a scale-up is triggered.
    autoscale_enabled = Column(Boolean, default=False)
    autoscale_provider_id = Column(Integer, ForeignKey("cloud_providers.id"), nullable=True)
    autoscale_region = Column(String, nullable=True)
    autoscale_plan = Column(String, nullable=True)
    autoscale_image = Column(String, nullable=True)
    # Trigger a scale-up when utilization (active_subs / total_capacity) is
    # above this fraction (0..1). Leave null to use the service default.
    autoscale_high_watermark = Column(Numeric(4, 3), nullable=True)
    # Hard upper bound on the number of nodes this pool may have.
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

    # Capacity / scheduling
    max_users = Column(Integer, nullable=True)
    max_bandwidth_mbps = Column(Integer, nullable=True)
    # Health: 0..100, updated by health-checker/probing service
    health_score = Column(Integer, default=100)
    last_health_check_at = Column(DateTime, nullable=True)
    # Regions in which this node is currently considered unreachable
    # (populated from HealthProbe aggregation). Example: ["ru", "by"].
    blocked_regions = Column(JSONB, nullable=True)
    cooldown_until = Column(DateTime, nullable=True)

    # Provisioning / cloud provider metadata
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
    server_pools = relationship("ServerPool", secondary=plan_serverpool, back_populates="plans")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    invoices = relationship("Invoice", back_populates="user")
    devices = relationship("Device", back_populates="user")


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
    proto = Column(String, nullable=False)  # 'shadowtls+ss', 'vless-reality'
    config_text = Column(Text, nullable=False)  # vless://..., ss://..., yaml
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
    # API token stored encrypted via app.security.crypto if key is configured,
    # otherwise plain text (dev only).
    api_token_enc = Column(Text, nullable=True)
    default_image = Column(String, nullable=True)
    ssh_key_ids = Column(JSONB, nullable=True)  # list of provider-side ssh key ids
    default_region = Column(String, nullable=True)
    default_plan = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    nodes = relationship("VPNNode", back_populates="provider")


class HealthProbe(Base):
    __tablename__ = "health_probes"

    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, ForeignKey("vpn_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    # Where the probe came from. Free-form label, e.g. "ru-mobile-mts", "kz", "eu".
    source_region = Column(String, nullable=False, index=True)
    source_kind = Column(String, nullable=True)  # "active" / "passive" / "client"
    result = Column(Enum(ProbeResult), nullable=False)
    latency_ms = Column(Integer, nullable=True)
    observed_at = Column(DateTime, default=utcnow, index=True)
    details = Column(JSONB, nullable=True)

    node = relationship("VPNNode", back_populates="probes")


class ApiToken(Base):
    """Scoped API token for non-admin callers (probe rigs, node collectors).

    Only the SHA-256 hash of the token is stored — the plaintext is shown
    once at creation time and never again. ``scopes`` is a list of string
    capabilities (e.g. ``probe:read``, ``probe:write``, ``traffic:write``);
    the admin token is treated as having all scopes without a row here.
    """

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
    # `metadata` is reserved by SQLAlchemy Declarative; expose as `extra`
    # but keep the column name for backward compat with existing DBs.
    extra = Column("metadata", JSONB, nullable=True)
