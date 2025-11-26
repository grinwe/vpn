from datetime import datetime
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
from sqlalchemy.orm import relationship
from .db import Base
import enum


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    blocked = "blocked"
    expired = "expired"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"
    refunded = "refunded"


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
    servers = relationship("Server", back_populates="pool")
    plans = relationship("Plan", secondary=plan_serverpool, back_populates="server_pools")


class Server(Base):
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    host = Column(String, nullable=False)
    location = Column(String, nullable=False)
    shadowtls_port = Column(Integer, default=443)
    vless_port = Column(Integer, default=9443)
    is_active = Column(Boolean, default=True)
    pool_id = Column(Integer, ForeignKey("server_pools.id"))
    pool = relationship("ServerPool", back_populates="servers")


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    duration_days = Column(Integer, nullable=False)
    max_devices = Column(Integer, default=1)
    price = Column(Numeric(10, 2), default=0)
    server_pools = relationship("ServerPool", secondary=plan_serverpool, back_populates="plans")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    status = Column(Enum(SubscriptionStatus), default=SubscriptionStatus.active)
    notes = Column(Text)

    user = relationship("User")
    plan = relationship("Plan")
    server = relationship("Server")
    credentials = relationship("Credential", back_populates="subscription")
    payments = relationship("Payment", back_populates="subscription")


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=False)
    proto = Column(String, nullable=False)  # 'shadowtls+ss', 'vless-reality'
    config_text = Column(Text, nullable=False)  # vless://..., ss://..., yaml
    created_at = Column(DateTime, default=datetime.utcnow)

    subscription = relationship("Subscription", back_populates="credentials")


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
