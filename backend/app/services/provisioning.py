"""Provisioning orchestration and helpers."""
from __future__ import annotations

import base64
import logging
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from prometheus_client import Counter
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..db import SessionLocal
from .ansible_runner import build_inventory_for_node, run_playbook

logger = logging.getLogger(__name__)
TASK_STATUS_COUNTER = Counter("vpn_provisioning_tasks_total", "Provisioning tasks processed", ["status"])

MAX_CONCURRENT_ANSIBLE = int(os.getenv("MAX_CONCURRENT_ANSIBLE", "3"))
_ansible_semaphore = threading.Semaphore(MAX_CONCURRENT_ANSIBLE)


def choose_node(db: Session, plan: models.Plan, node_id: int | None = None) -> models.VPNNode:
    """Pick an active VPN node based on plan pools or explicit id."""
    query = db.query(models.VPNNode).filter(models.VPNNode.is_active.is_(True))
    if node_id:
        node = query.filter(models.VPNNode.id == node_id).first()
        if not node:
            raise RuntimeError("Requested node is not active or missing")
        return node

    pools = plan.server_pools or []
    if pools:
        pool_ids = [p.id for p in pools]
        query = query.filter(models.VPNNode.pool_id.in_(pool_ids))

    # Pick node with fewest active subscriptions
    node = (
        query
        .outerjoin(
            models.Subscription,
            (models.Subscription.node_id == models.VPNNode.id)
            & (models.Subscription.status == models.SubscriptionStatus.active),
        )
        .group_by(models.VPNNode.id)
        .order_by(func.count(models.Subscription.id).asc())
        .first()
    )
    if not node:
        raise RuntimeError("No active VPN nodes available for plan")
    return node


def choose_config(node: models.VPNNode, preferred_protocol: models.VPNConfigProtocol | None = None) -> models.VPNConfig:
    configs = [cfg for cfg in node.configs if cfg.is_enabled]
    if preferred_protocol:
        filtered = [cfg for cfg in configs if cfg.protocol == preferred_protocol]
        if filtered:
            return filtered[0]
    if not configs:
        raise RuntimeError("No enabled VPN configs found for node")
    return configs[0]


def _build_shadowtls_credential(node: models.VPNNode, config: models.VPNConfig, username: str, password: str) -> str:
    method = (config.settings or {}).get("method", "chacha20-ietf-poly1305")
    payload = f"{method}:{password}@{node.host}:{config.port}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"ss://{encoded}#shadowtls-{username}"


def _build_vless_reality_credential(node: models.VPNNode, config: models.VPNConfig, user_id: str) -> str:
    sni = config.sni or config.fallback or (config.settings or {}).get("server_name", "")
    params = {
        "security": "reality",
        "sni": sni,
        "pbk": config.public_key or (config.settings or {}).get("public_key", ""),
        "flow": "xtls-rprx-vision",
        "fp": "chrome",
        "type": "tcp",
    }
    query = "&".join([f"{k}={v}" for k, v in params.items() if v])
    return f"vless://{user_id}@{node.host}:{config.port}?{query}#reality-{node.region}"


class ProvisioningOrchestrator:
    """Coordinates provisioning tasks and Ansible execution."""

    def __init__(self, db: Session):
        self.db = db

    def create_task(self, target_type: str, target_id: int, action: str, payload: dict[str, Any] | None) -> models.ProvisioningTask:
        task = models.ProvisioningTask(
            target_type=target_type,
            target_id=target_id,
            action=action,
            payload=payload or {},
            status=models.ProvisioningTaskStatus.pending,
        )
        self.db.add(task)
        self.db.flush()
        return task

    def _mark_task(self, task: models.ProvisioningTask, status: models.ProvisioningTaskStatus, *, error: str | None = None, result: dict[str, Any] | None = None) -> None:
        task.status = status
        task.error_message = error
        task.result = result
        task.finished_at = datetime.utcnow()
        self.db.add(task)
        self.db.commit()
        TASK_STATUS_COUNTER.labels(status=status.value).inc()

    def run_task(self, task: models.ProvisioningTask, node: models.VPNNode | None = None) -> models.ProvisioningTask:
        task.started_at = datetime.utcnow()
        task.status = models.ProvisioningTaskStatus.running
        self.db.commit()

        result_payload: dict[str, Any] | None = None
        try:
            result_payload = self._execute_task(task, node=node)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Provisioning task %s failed", task.id)
            self._mark_task(task, models.ProvisioningTaskStatus.failed, error=str(exc), result=result_payload)
            self._handle_task_outcome(task, success=False)
            return task

        self._mark_task(task, models.ProvisioningTaskStatus.success, result=result_payload)
        self._handle_task_outcome(task, success=True)
        return task

    def run_task_async(self, task: models.ProvisioningTask, node: models.VPNNode | None = None) -> None:
        """Execute a provisioning task in a background thread with a fresh DB session.

        The request-scoped session (`self.db`) must not be shared across threads, so we
        pass lightweight identifiers to the worker and open a new `SessionLocal` there.
        """

        thread = threading.Thread(
            target=self._run_task_in_new_session,
            args=(task.id, node.id if node else None),
            daemon=True,
        )
        thread.start()

    def _run_task_in_new_session(self, task_id: int, node_id: int | None = None) -> None:
        session = SessionLocal()
        try:
            orchestrator = ProvisioningOrchestrator(session)
            task = session.get(models.ProvisioningTask, task_id)
            if not task:
                logger.error("Provisioning task %s not found for async execution", task_id)
                return

            node: models.VPNNode | None = None
            if node_id:
                node = session.get(models.VPNNode, node_id)
            elif task.target_type == "device":
                device = session.get(models.Device, task.target_id)
                if device:
                    node = device.config.node if device.config else device.subscription.node

            orchestrator.run_task(task, node=node)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Async provisioning task %s failed before completion", task_id)
            if session.is_active:
                session.rollback()
            try:
                task = session.get(models.ProvisioningTask, task_id)
                if task:
                    orchestrator = ProvisioningOrchestrator(session)
                    orchestrator._mark_task(  # noqa: SLF001
                        task,
                        models.ProvisioningTaskStatus.failed,
                        error=str(exc),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to mark task %s as failed", task_id)
                session.rollback()
        finally:
            session.close()

    def reset_failed_task(self, task: models.ProvisioningTask) -> None:
        task.status = models.ProvisioningTaskStatus.pending
        task.error_message = None
        task.result = None
        task.started_at = None
        task.finished_at = None
        self.db.add(task)
        self.db.commit()

    def _handle_task_outcome(self, task: models.ProvisioningTask, *, success: bool) -> None:
        if task.target_type != "device":
            return

        device = self.db.get(models.Device, task.target_id)
        if not device:
            return

        if success:
            if task.action == "apply":
                device.status = models.DeviceStatus.active
                for cred in device.credentials:
                    cred.is_active = True
                    cred.revoked_at = None
            elif task.action == "revoke":
                device.status = models.DeviceStatus.revoked
                now = datetime.utcnow()
                for cred in device.credentials:
                    cred.is_active = False
                    cred.revoked_at = cred.revoked_at or now
        else:
            device.status = models.DeviceStatus.failed
        device.updated_at = datetime.utcnow()
        self.db.add(device)
        self.db.commit()

    def _execute_task(self, task: models.ProvisioningTask, node: models.VPNNode | None = None) -> dict[str, Any]:
        payload = task.payload or {}
        _ansible_semaphore.acquire()
        try:
            if task.target_type == "node":
                if not node:
                    node = self.db.get(models.VPNNode, task.target_id)
                if not node:
                    raise RuntimeError("VPN node not found for provisioning")
                inventory = build_inventory_for_node(node)
                result = run_playbook("site.yml", inventory, limit=node.name)
            elif task.target_type == "device":
                if not node:
                    raise RuntimeError("Node is required to provision device")
                inventory = build_inventory_for_node(node)
                result = run_playbook(
                    "playbooks/provision_device.yml",
                    inventory,
                    limit=node.name,
                    extra_vars=payload,
                )
            else:
                raise RuntimeError(f"Unsupported target type {task.target_type}")
        finally:
            _ansible_semaphore.release()

        payload = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }

        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or "ansible playbook failed")

        return payload

    def provision_subscription(
        self,
        user: models.User,
        plan: models.Plan,
        *,
        node_id: int | None = None,
        device_name: str | None = None,
    ) -> tuple[models.Subscription, models.ProvisioningTask]:
        node = choose_node(self.db, plan, node_id=node_id)
        config = choose_config(node)

        # Enforce max_devices limit across all active subscriptions for this user/plan
        active_device_count = (
            self.db.query(models.Device)
            .join(models.Subscription)
            .filter(
                models.Subscription.user_id == user.id,
                models.Subscription.plan_id == plan.id,
                models.Device.status.notin_([models.DeviceStatus.revoked, models.DeviceStatus.disabled]),
            )
            .count()
        )
        if active_device_count >= plan.max_devices:
            raise RuntimeError(f"Device limit reached for this plan (max {plan.max_devices})")

        subscription = models.Subscription(
            user_id=user.id,
            plan_id=plan.id,
            node_id=node.id,
            expires_at=datetime.utcnow() + timedelta(days=plan.duration_days),
            traffic_limit_mb=plan.traffic_limit_mb,
        )
        self.db.add(subscription)
        self.db.flush()

        device_label = device_name or "primary"
        username = f"user-{user.id}-{subscription.id}"
        password = secrets.token_urlsafe(12)
        credential_text: str
        if config.protocol == models.VPNConfigProtocol.shadowtls_ss:
            credential_text = _build_shadowtls_credential(node, config, username, password)
        else:
            user_uuid = uuid.uuid4()
            credential_text = _build_vless_reality_credential(node, config, str(user_uuid))

        device = models.Device(
            user_id=user.id,
            subscription_id=subscription.id,
            config_id=config.id,
            name=device_label,
            status=models.DeviceStatus.pending,
            access_username=username,
            connection_uri=credential_text,
        )
        self.db.add(device)
        self.db.flush()

        credential = models.Credential(
            subscription_id=subscription.id,
            device_id=device.id,
            config_id=config.id,
            proto=config.protocol.value,
            config_text=credential_text,
        )
        self.db.add(credential)

        task_payload = {
            "username": username,
            "password": password,
            "port": config.port,
            "method": (config.settings or {}).get("method", "chacha20-ietf-poly1305"),
            "config_proto": config.protocol.value,
            "state": "present",
        }
        task = self.create_task("device", device.id, "apply", task_payload)
        self.db.commit()
        self.run_task_async(task, node=node)
        self.db.refresh(subscription)
        return subscription, task

    def revoke_device(
        self, device: models.Device, *, reason: str | None = None, background: bool = True
    ) -> models.ProvisioningTask:
        payload = {
            "username": device.access_username,
            "port": device.config.port if device.config else None,
            "config_proto": device.config.protocol.value if device.config else None,
            "state": "absent",
            "reason": reason,
        }
        task = self.create_task("device", device.id, "revoke", payload)
        device.status = models.DeviceStatus.disabled
        for cred in device.credentials:
            cred.is_active = False
            cred.revoked_at = cred.revoked_at or datetime.utcnow()
        self.db.commit()
        node = device.config.node if device.config else device.subscription.node
        if background:
            self.run_task_async(task, node=node)
        else:
            self.run_task(task, node=node)
        return task

    def revoke_subscription_devices(
        self, subscription: models.Subscription, *, reason: str | None = None
    ) -> list[models.ProvisioningTask]:
        tasks: list[models.ProvisioningTask] = []
        for device in subscription.devices:
            tasks.append(self.revoke_device(device, reason=reason))
        subscription.status = models.SubscriptionStatus.blocked
        subscription.expires_at = datetime.utcnow()
        self.db.commit()
        return tasks
