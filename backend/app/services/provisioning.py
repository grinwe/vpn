"""Утилиты провижининга VPN-учёток на ноды."""
from __future__ import annotations
import logging
import os
import random
import secrets
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
import paramiko
from .. import models


logger = logging.getLogger(__name__)


def choose_server(db: Session, plan: models.Plan) -> models.Server:
    """Выбирает активный сервер из пулов, привязанных к плану."""
    pools = plan.server_pools or []
    servers = [srv for pool in pools for srv in pool.servers if srv.is_active]
    if not servers:
        servers = db.query(models.Server).filter(models.Server.is_active.is_(True)).all()
    if not servers:
        raise RuntimeError("No active servers available")
    return random.choice(servers)


def _run_remote_command(server: models.Server, command: str) -> str:
    key_path = os.getenv("VPN_SSH_KEY_PATH", os.path.expanduser("~/.ssh/id_rsa"))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(server.host, username="root", key_filename=key_path)
        _, stdout, stderr = client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        output = stdout.read().decode().strip()
        error_output = stderr.read().decode().strip()
        if exit_status != 0:
            raise RuntimeError(error_output or output or "SSH command failed")
        return output
    finally:
        client.close()


def provision_shadowtls_ss_user(
    server: models.Server, username: str, password: str | None = None
) -> str:
    vpn_password = password or secrets.token_urlsafe(12)
    command = (
        "/usr/local/sbin/manage_vpn_user.sh add-shadowtls-ss "
        f"{username} {vpn_password} chacha20-ietf-poly1305 8388"
    )
    return _run_remote_command(server, command)


def deprovision_shadowtls_ss_user(server: models.Server, username: str) -> None:
    command = f"/usr/local/sbin/manage_vpn_user.sh del-shadowtls-ss {username}"
    _run_remote_command(server, command)


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

    username = f"user-{user.id}-{sub.id}"
    ss_url = provision_shadowtls_ss_user(server, username)
    cred = models.Credential(subscription_id=sub.id, proto="shadowtls+ss", config_text=ss_url)
    db.add(cred)
    return sub
