"""Tiny object factories used across the integration tests.

Intentionally NOT a full-featured factory_boy / polyfactory setup — we
just need compact helpers that create the few linked rows each test
wants. Each helper commits, so the returned objects are usable across
sessions.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import models


def make_node(
    db: Session,
    *,
    name: str = "test-node-1",
    region: str = "eu",
    host: str = "198.51.100.10",
    status: models.VPNNodeStatus = models.VPNNodeStatus.active,
    max_users: int | None = 100,
) -> models.VPNNode:
    node = models.VPNNode(
        name=name,
        region=region,
        host=host,
        status=status,
        is_active=True,
        health_score=100,
        max_users=max_users,
        last_health_check_at=datetime.utcnow(),
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def make_config(
    db: Session,
    node: models.VPNNode,
    *,
    name: str = "test-vless",
    protocol: models.VPNConfigProtocol = models.VPNConfigProtocol.vless_reality,
    port: int = 443,
    sni: str | None = "www.microsoft.com",
) -> models.VPNConfig:
    cfg = models.VPNConfig(
        node_id=node.id,
        name=name,
        protocol=protocol,
        port=port,
        sni=sni,
        public_key="dummy-pubkey",
        settings={"short_id": "deadbeefdeadbeef", "dest": "www.microsoft.com:443"},
        is_enabled=True,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def make_plan(
    db: Session,
    *,
    name: str = "test-plan",
    traffic_limit_mb: int | None = 10_000,
    max_devices: int = 3,
) -> models.Plan:
    plan = models.Plan(
        name=name,
        duration_days=30,
        max_devices=max_devices,
        price=10,
        traffic_limit_mb=traffic_limit_mb,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def make_user(db: Session, *, telegram_id: str = "tg-1") -> models.User:
    user = models.User(telegram_id=telegram_id)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_subscription(
    db: Session,
    user: models.User,
    plan: models.Plan,
    node: models.VPNNode,
    *,
    traffic_limit_mb: int | None = None,
) -> models.Subscription:
    sub = models.Subscription(
        user_id=user.id,
        plan_id=plan.id,
        node_id=node.id,
        expires_at=datetime.utcnow() + timedelta(days=30),
        status=models.SubscriptionStatus.active,
        traffic_limit_mb=traffic_limit_mb if traffic_limit_mb is not None else plan.traffic_limit_mb,
        traffic_used_mb=0,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def make_subscription_with_device(
    db: Session,
    user: models.User,
    plan: models.Plan,
    node: models.VPNNode,
    *,
    access_username: str | None = None,
) -> models.Subscription:
    """Stage 7 helper: a sub plus one active Device, since capacity
    bookkeeping is now per-device. Reuses the first config on the node
    or makes a fresh one if the node has none yet."""
    sub = make_subscription(db, user, plan, node)
    cfg = next(iter(node.configs), None) if node.configs else None
    if cfg is None:
        cfg = make_config(db, node)
    make_device(
        db,
        sub,
        cfg,
        access_username=access_username or f"user-{user.id}-{sub.id}",
    )
    db.refresh(sub)
    return sub


def make_device(
    db: Session,
    sub: models.Subscription,
    config: models.VPNConfig,
    *,
    access_username: str = "user-1-1",
) -> models.Device:
    device = models.Device(
        user_id=sub.user_id,
        subscription_id=sub.id,
        config_id=config.id,
        name="primary",
        status=models.DeviceStatus.active,
        access_username=access_username,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device
