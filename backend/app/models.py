from datetime import datetime
import enum
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
from sqlalchemy.dialects.postgresql import JSONB
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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    notes = Column(Text)

    pool = relationship("ServerPool", back_populates="nodes")
    configs = relationship("VPNConfig", back_populates="node", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="node")


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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.utcnow)
    invoices = relationship("Invoice", back_populates="user")
    devices = relationship("Device", back_populates="user")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    node_id = Column(Integer, ForeignKey("vpn_nodes.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
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
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    revoked_at = Column(DateTime, nullable=True)

    subscription = relationship("Subscription", back_populates="credentials")
    device = relationship("Device", back_populates="credentials")
    config = relationship("VPNConfig", back_populates="credentials")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=False)
    amount = Column(Numeric(10, 2), default=0)
    currency = Column(String, default="USD")
    status = Column(Enum(PaymentStatus), default=PaymentStatus.pending)
    provider = Column(String, default="manual")
    external_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subscription = relationship("Subscription", back_populates="payments")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    amount = Column(Numeric(10, 2), default=0)
    currency = Column(String, default="USD")
    status = Column(Enum(InvoiceStatus), default=InvoiceStatus.pending)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="invoices")
    plan = relationship("Plan")


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
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    actor = Column(String, nullable=False)
    actor_type = Column(Enum(AuditActor), default=AuditActor.system)
    action = Column(String, nullable=False)
    target_type = Column(String, nullable=False)
    target_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    metadata = Column(JSONB, nullable=True)
