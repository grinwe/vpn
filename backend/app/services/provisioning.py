"""Утилиты провижининга VPN-учёток на ноды.
В реальной установке сюда добавляется SSH/Ansible вызов или REST-агент на ноде.
"""
from __future__ import annotations
import random
import uuid
from datetime import datetime, timedelta
from typing import Dict
from sqlalchemy.orm import Session
from .. import models


def choose_server(db: Session, plan: models.Plan) -> models.Server:
    """Выбирает активный сервер из пулов, привязанных к плану."""
    pools = plan.server_pools or []
    servers = [srv for pool in pools for srv in pool.servers if srv.is_active]
    if not servers:
        servers = db.query(models.Server).filter(models.Server.is_active.is_(True)).all()
    if not servers:
        raise RuntimeError("No active servers available")
    return random.choice(servers)


def call_node_agent(server: models.Server, user_uuid: str) -> Dict[str, str]:
    """Заглушка: здесь будет вызов ansible/ssh/rest.
    Возвращаем набор готовых конфигов.
    """
    # TODO: реализовать реальный вызов ansible-runner или REST-агента
    ss_config = f"ss://method:password@{server.host}:{server.shadowtls_port}?plugin=shadowtls"
    vless_config = f"vless://{user_uuid}@{server.host}:{server.vless_port}?security=reality"
    return {
        "shadowtls+ss": ss_config,
        "vless-reality": vless_config,
    }


def provision_subscription(db: Session, user: models.User, plan: models.Plan) -> models.Subscription:
    server = choose_server(db, plan)
    sub = models.Subscription(
        user_id=user.id,
        plan_id=plan.id,
        server_id=server.id,
        expires_at=datetime.utcnow() + timedelta(days=plan.duration_days),
    )
    db.add(sub)
    db.flush()

    user_uuid = str(uuid.uuid4())
    configs = call_node_agent(server, user_uuid)
    for proto, cfg in configs.items():
        cred = models.Credential(subscription_id=sub.id, proto=proto, config_text=cfg)
        db.add(cred)
    return sub
