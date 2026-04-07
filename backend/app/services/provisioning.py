"""Provisioning orchestration and helpers."""
from __future__ import annotations

import base64
import logging
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote as urlquote

from ..time_utils import utcnow
from typing import Any

from prometheus_client import Counter
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..db import SessionLocal
from ..security import decrypt, encrypt
from .ansible_runner import build_inventory_for_node, run_playbook

logger = logging.getLogger(__name__)
TASK_STATUS_COUNTER = Counter("vpn_provisioning_tasks_total", "Provisioning tasks processed", ["status"])

MAX_CONCURRENT_ANSIBLE = int(os.getenv("MAX_CONCURRENT_ANSIBLE", "3"))
_ansible_semaphore = threading.Semaphore(MAX_CONCURRENT_ANSIBLE)

MIN_HEALTHY_SCORE = int(os.getenv("MIN_HEALTHY_SCORE", "50"))


# ── Node selection ────────────────────────────────────────────────────

def choose_node(
    db: Session,
    plan: models.Plan,
    node_id: int | None = None,
    *,
    exclude_node_ids: list[int] | None = None,
) -> models.VPNNode:
    """Pick a VPN node, respecting plan pools, capacity, health and cooldown.

    Uses SELECT FOR UPDATE SKIP LOCKED to prevent concurrent provisioners
    from over-committing a single node.
    """
    now = utcnow()
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

    if exclude_node_ids:
        query = query.filter(~models.VPNNode.id.in_(exclude_node_ids))

    query = query.filter(
        (models.VPNNode.cooldown_until.is_(None))
        | (models.VPNNode.cooldown_until < now)
    )
    query = query.filter(
        models.VPNNode.status.in_(
            [models.VPNNodeStatus.active, models.VPNNodeStatus.registering]
        )
    )
    query = query.filter(
        (models.VPNNode.health_score.is_(None))
        | (models.VPNNode.health_score >= MIN_HEALTHY_SCORE)
    )

    active_sub_count = func.count(models.Subscription.id).label("active_subs")
    rows = (
        query.outerjoin(
            models.Subscription,
            (models.Subscription.node_id == models.VPNNode.id)
            & (models.Subscription.status == models.SubscriptionStatus.active),
        )
        .group_by(models.VPNNode.id)
        .order_by(active_sub_count.asc())
        .with_entities(models.VPNNode, active_sub_count)
        .all()
    )

    for node, subs in rows:
        if node.max_users is not None and subs >= node.max_users:
            continue
        locked = (
            db.query(models.VPNNode)
            .filter(models.VPNNode.id == node.id)
            .with_for_update(skip_locked=True)
            .one_or_none()
        )
        if locked is None:
            continue
        if locked.max_users is not None:
            live_subs = (
                db.query(func.count(models.Subscription.id))
                .filter(
                    models.Subscription.node_id == locked.id,
                    models.Subscription.status == models.SubscriptionStatus.active,
                )
                .scalar()
                or 0
            )
            if live_subs >= locked.max_users:
                continue
        return locked

    raise RuntimeError("No healthy VPN nodes available for plan")


def choose_config(
    node: models.VPNNode, preferred_protocol: models.VPNConfigProtocol | None = None
) -> models.VPNConfig:
    configs = [cfg for cfg in node.configs if cfg.is_enabled]
    if preferred_protocol:
        filtered = [cfg for cfg in configs if cfg.protocol == preferred_protocol]
        if filtered:
            return filtered[0]
    if not configs:
        raise RuntimeError("No enabled VPN configs found for node")
    return configs[0]


# ── Credential builders ──────────────────────────────────────────────

def _build_shadowtls_credential(
    node: models.VPNNode, config: models.VPNConfig, username: str, password: str
) -> str:
    method = (config.settings or {}).get("method", "chacha20-ietf-poly1305")
    payload = f"{method}:{password}@{node.host}:{config.port}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"ss://{encoded}#shadowtls-{username}"


def _build_vless_reality_credential(
    node: models.VPNNode, config: models.VPNConfig, user_id: str
) -> str:
    settings = config.settings or {}
    sni = config.sni or settings.get("server_name", "")
    params = {
        "security": "reality",
        "sni": sni,
        "pbk": config.public_key or settings.get("public_key", ""),
        "sid": settings.get("short_id", ""),
        "flow": "xtls-rprx-vision",
        "fp": "chrome",
        "type": "tcp",
    }
    query = "&".join([f"{k}={v}" for k, v in params.items() if v])
    return f"vless://{user_id}@{node.host}:{config.port}?{query}#reality-{node.region}"


def _build_vless_ws_cdn_credential(
    node: models.VPNNode, config: models.VPNConfig, user_id: str
) -> str:
    """Build a VLESS+WebSocket+TLS connection URI routed through Cloudflare CDN.

    The ``host`` header is set to the CDN domain (from config.sni) so
    Cloudflare routes the WebSocket to the origin. The actual IP in the
    URI is the CDN edge — clients never see the real server IP.
    """
    settings = config.settings or {}
    cdn_domain = config.sni or settings.get("cdn_domain", "")
    path = settings.get("ws_path", "/ws")
    params = {
        "security": "tls",
        "sni": cdn_domain,
        "fp": "chrome",
        "type": "ws",
        "host": cdn_domain,
        "path": urlquote(path),
    }
    query = "&".join([f"{k}={v}" for k, v in params.items() if v])
    return f"vless://{user_id}@{cdn_domain}:{config.port}?{query}#ws-cdn-{node.region}"


def _build_hysteria2_credential(
    node: models.VPNNode, config: models.VPNConfig, password: str
) -> str:
    """Build a Hysteria2 URI.

    Format: ``hy2://password@host:port?sni=...&insecure=0#tag``
    """
    settings = config.settings or {}
    sni = config.sni or settings.get("sni", node.host)
    obfs = settings.get("obfs", "")
    obfs_password = settings.get("obfs_password", "")
    params = {"sni": sni}
    if obfs:
        params["obfs"] = obfs
        params["obfs-password"] = obfs_password
    query = "&".join([f"{k}={v}" for k, v in params.items() if v])
    return f"hy2://{password}@{node.host}:{config.port}?{query}#hy2-{node.region}"


# ── Extra vars collection for Ansible site.yml ────────────────────────

def _collect_site_extra_vars(node: models.VPNNode) -> dict[str, Any]:
    """Build extra_vars for a node-level site.yml run.

    Inspects all enabled VPNConfig rows on the node and surfaces the
    backend-authoritative secrets for each protocol to the installer roles.
    """
    extra: dict[str, Any] = {}
    for cfg in node.configs:
        if not cfg.is_enabled:
            continue
        settings = cfg.settings or {}

        # ── VLESS Reality ──
        if cfg.protocol == models.VPNConfigProtocol.vless_reality:
            priv_enc = settings.get("private_key_enc")
            if not priv_enc or not cfg.public_key:
                continue
            extra.update({
                "vless_reality_private_key": decrypt(priv_enc),
                "vless_reality_public_key": cfg.public_key,
                "vless_reality_short_id": settings.get("short_id", ""),
                "vless_reality_port": cfg.port,
                "vless_reality_sni": cfg.sni or "",
                "vless_reality_dest": settings.get("dest") or cfg.fallback or "",
            })

        # ── VLESS+WS+CDN ──
        elif cfg.protocol == models.VPNConfigProtocol.vless_ws_cdn:
            extra.update({
                "vless_ws_cdn_port": cfg.port,
                "vless_ws_cdn_domain": cfg.sni or "",
                "vless_ws_cdn_path": settings.get("ws_path", "/ws"),
                "vless_ws_cdn_cert_path": settings.get("cert_path", ""),
                "vless_ws_cdn_key_path": settings.get("key_path", ""),
            })

        # ── Hysteria2 ──
        elif cfg.protocol == models.VPNConfigProtocol.hysteria2:
            extra.update({
                "hysteria2_port": cfg.port,
                "hysteria2_domain": cfg.sni or node.host,
                "hysteria2_obfs": settings.get("obfs", ""),
                "hysteria2_obfs_password": settings.get("obfs_password", ""),
                "hysteria2_cert_path": settings.get("cert_path", ""),
                "hysteria2_key_path": settings.get("key_path", ""),
                "hysteria2_up_mbps": settings.get("up_mbps", 100),
                "hysteria2_down_mbps": settings.get("down_mbps", 100),
            })

    return extra


def _generate_sub_token() -> str:
    """Generate a stable 22-char URL-safe subscription token."""
    return secrets.token_urlsafe(16)


# ── Orchestrator ─────────────────────────────────────────────────────

class ProvisioningOrchestrator:
    """Coordinates provisioning tasks and Ansible execution."""

    def __init__(self, db: Session):
        self.db = db

    def create_task(
        self, target_type: str, target_id: int, action: str, payload: dict[str, Any] | None
    ) -> models.ProvisioningTask:
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

    def _mark_task(
        self,
        task: models.ProvisioningTask,
        status: models.ProvisioningTaskStatus,
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        task.status = status
        task.error_message = error
        task.result = result
        task.finished_at = utcnow()
        self.db.add(task)
        self.db.commit()
        TASK_STATUS_COUNTER.labels(status=status.value).inc()

    def run_task(
        self, task: models.ProvisioningTask, node: models.VPNNode | None = None
    ) -> models.ProvisioningTask:
        task.started_at = utcnow()
        task.status = models.ProvisioningTaskStatus.running
        self.db.commit()

        result_payload: dict[str, Any] | None = None
        try:
            result_payload = self._execute_task(task, node=node)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Provisioning task %s failed", task.id)
            self._mark_task(
                task, models.ProvisioningTaskStatus.failed,
                error=str(exc), result=result_payload,
            )
            self._handle_task_outcome(task, success=False)
            return task

        self._mark_task(task, models.ProvisioningTaskStatus.success, result=result_payload)
        self._handle_task_outcome(task, success=True)
        return task

    def run_task_async(
        self, task: models.ProvisioningTask, node: models.VPNNode | None = None
    ) -> None:
        """Dispatch a provisioning task off the request thread via RQ."""
        from ..queue import enqueue_task

        job_id = enqueue_task(task.id, node.id if node else None)
        if job_id:
            logger.info("Task %s enqueued as RQ job %s", task.id, job_id)
            return

        if os.getenv("ALLOW_INPROCESS_PROVISIONING", "").lower() not in {"1", "true", "yes"}:
            raise RuntimeError(
                "Provisioning queue is unavailable and ALLOW_INPROCESS_PROVISIONING is not set"
            )

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
                        task, models.ProvisioningTaskStatus.failed, error=str(exc),
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
                now = utcnow()
                for cred in device.credentials:
                    cred.is_active = False
                    cred.revoked_at = cred.revoked_at or now
        else:
            device.status = models.DeviceStatus.failed
        device.updated_at = utcnow()
        self.db.add(device)
        self.db.commit()

        # ── Post-provision callback: notify bot ──
        if success and task.action == "apply":
            self._notify_bot_config_ready(device)

    def _notify_bot_config_ready(self, device: models.Device) -> None:
        """Push a notification to the bot that config is ready for delivery.

        We POST to the backend's internal /api/bot/notify_config endpoint,
        which the bot polls or which triggers a direct Telegram message.
        Instead of coupling worker→bot, we write a lightweight callback
        record that the bot's polling loop picks up.
        """
        try:
            sub = device.subscription
            if not sub:
                return
            user = sub.user
            if not user or not user.telegram_id:
                return
            # Store the notification in the task result so the bot can read it
            # via the existing task polling mechanism, or via the new callback API.
            task_result = (
                self.db.query(models.ProvisioningTask)
                .filter(
                    models.ProvisioningTask.target_type == "device",
                    models.ProvisioningTask.target_id == device.id,
                    models.ProvisioningTask.action == "apply",
                    models.ProvisioningTask.status == models.ProvisioningTaskStatus.success,
                )
                .order_by(models.ProvisioningTask.id.desc())
                .first()
            )
            if task_result and task_result.result:
                result = dict(task_result.result)
                result["_notify"] = {
                    "telegram_id": user.telegram_id,
                    "subscription_id": sub.id,
                    "device_id": device.id,
                }
                task_result.result = result
                self.db.add(task_result)
                self.db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to store bot notification for device %s", device.id)

    def _execute_task(
        self, task: models.ProvisioningTask, node: models.VPNNode | None = None
    ) -> dict[str, Any]:
        payload = task.payload or {}
        _ansible_semaphore.acquire()
        inventory = None
        try:
            if task.target_type == "node":
                if not node:
                    node = self.db.get(models.VPNNode, task.target_id)
                if not node:
                    raise RuntimeError("VPN node not found for provisioning")
                inventory = build_inventory_for_node(node)
                site_vars = _collect_site_extra_vars(node)
                result = run_playbook(
                    "site.yml", inventory, limit=node.name, extra_vars=site_vars,
                )
            elif task.target_type == "device":
                if not node:
                    raise RuntimeError("Node is required to provision device")
                inventory = build_inventory_for_node(node)
                result = run_playbook(
                    "playbooks/provision_device.yml",
                    inventory, limit=node.name, extra_vars=payload,
                )
            else:
                raise RuntimeError(f"Unsupported target type {task.target_type}")
        finally:
            _ansible_semaphore.release()
            if inventory is not None:
                try:
                    inventory.unlink()
                except OSError:
                    logger.warning("Failed to remove temp inventory %s", inventory)

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
        expires_at_override: datetime | None = None,
    ) -> tuple[models.Subscription, models.ProvisioningTask]:
        node = choose_node(self.db, plan, node_id=node_id)

        # All enabled configs become credentials under one subscription so
        # the dynamic sub-link returns every protocol the node serves and
        # the client picks whichever currently works.
        enabled_configs = [cfg for cfg in node.configs if cfg.is_enabled]
        if not enabled_configs:
            raise RuntimeError("No enabled VPN configs found for node")

        active_device_count = (
            self.db.query(models.Device)
            .join(models.Subscription)
            .filter(
                models.Subscription.user_id == user.id,
                models.Subscription.plan_id == plan.id,
                models.Device.status.notin_([
                    models.DeviceStatus.revoked, models.DeviceStatus.disabled
                ]),
            )
            .count()
        )
        if active_device_count >= plan.max_devices:
            raise RuntimeError(f"Device limit reached for this plan (max {plan.max_devices})")

        subscription = models.Subscription(
            user_id=user.id,
            plan_id=plan.id,
            node_id=node.id,
            expires_at=expires_at_override or (utcnow() + timedelta(days=plan.duration_days)),
            traffic_limit_mb=plan.traffic_limit_mb,
            sub_token=_generate_sub_token(),
        )
        self.db.add(subscription)
        self.db.flush()

        device_label = device_name or "primary"
        # One identity shared across all protocols on this device — the node
        # accounts traffic by access_username, so we want a single key.
        username = f"user-{user.id}-{subscription.id}"
        password = secrets.token_urlsafe(12)
        user_uuid = uuid.uuid4()

        # Device.connection_uri now stores the public sub-link, not a raw
        # protocol URI — admin UI shows one stable URL per device.
        sub_base = os.getenv("SUB_LINK_BASE_URL", "").rstrip("/")
        if sub_base:
            device_uri = f"{sub_base}/{subscription.sub_token}"
        else:
            device_uri = f"/api/sub/{subscription.sub_token}"

        # Pick a representative config for Device.config_id (FK is NOT NULL).
        # ShadowTLS preferred, otherwise the first enabled config.
        primary_config = next(
            (c for c in enabled_configs if c.protocol == models.VPNConfigProtocol.shadowtls_ss),
            enabled_configs[0],
        )

        device = models.Device(
            user_id=user.id,
            subscription_id=subscription.id,
            config_id=primary_config.id,
            name=device_label,
            status=models.DeviceStatus.pending,
            access_username=username,
            connection_uri=encrypt(device_uri),
        )
        self.db.add(device)
        self.db.flush()

        protocols_payload: list[dict[str, Any]] = []
        for cfg in enabled_configs:
            if cfg.protocol == models.VPNConfigProtocol.shadowtls_ss:
                cred_text = _build_shadowtls_credential(node, cfg, username, password)
            elif cfg.protocol == models.VPNConfigProtocol.vless_reality:
                cred_text = _build_vless_reality_credential(node, cfg, str(user_uuid))
            elif cfg.protocol == models.VPNConfigProtocol.vless_ws_cdn:
                cred_text = _build_vless_ws_cdn_credential(node, cfg, str(user_uuid))
            elif cfg.protocol == models.VPNConfigProtocol.hysteria2:
                cred_text = _build_hysteria2_credential(node, cfg, password)
            else:
                logger.warning("Skipping unsupported protocol %s on node %s", cfg.protocol, node.id)
                continue

            self.db.add(
                models.Credential(
                    subscription_id=subscription.id,
                    device_id=device.id,
                    config_id=cfg.id,
                    proto=cfg.protocol.value,
                    config_text=encrypt(cred_text),
                )
            )

            entry: dict[str, Any] = {"proto": cfg.protocol.value, "port": cfg.port}
            if cfg.protocol == models.VPNConfigProtocol.shadowtls_ss:
                entry["method"] = (cfg.settings or {}).get("method", "chacha20-ietf-poly1305")
            protocols_payload.append(entry)

        if not protocols_payload:
            raise RuntimeError("No supported protocols among enabled configs")

        task_payload: dict[str, Any] = {
            "username": username,
            "uuid": str(user_uuid),
            "password": password,
            "protocols": protocols_payload,
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
        # Revoke every protocol the device was provisioned into.
        protocols_payload: list[dict[str, Any]] = []
        seen: set[str] = set()
        for cred in device.credentials:
            if cred.proto in seen:
                continue
            seen.add(cred.proto)
            entry: dict[str, Any] = {"proto": cred.proto}
            if cred.config is not None:
                entry["port"] = cred.config.port
            protocols_payload.append(entry)

        payload = {
            "username": device.access_username,
            "protocols": protocols_payload,
            "state": "absent",
            "reason": reason,
        }
        task = self.create_task("device", device.id, "revoke", payload)
        device.status = models.DeviceStatus.disabled
        for cred in device.credentials:
            cred.is_active = False
            cred.revoked_at = cred.revoked_at or utcnow()
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
        self.db.commit()
        return tasks
