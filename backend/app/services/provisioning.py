"""Provisioning orchestration and helpers."""
from __future__ import annotations

import base64
import logging
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta

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


def choose_node(
    db: Session,
    plan: models.Plan,
    node_id: int | None = None,
    *,
    exclude_node_ids: list[int] | None = None,
) -> models.VPNNode:
    """Pick a VPN node, respecting plan pools, capacity, health and cooldown.

    Selection criteria (applied in order):
      - explicit ``node_id`` wins if given (but must still be active);
      - node must be ``is_active`` and have a status that permits traffic;
      - node must not be in cooldown (``cooldown_until`` in the future);
      - ``health_score`` must be >= ``MIN_HEALTHY_SCORE``;
      - node must not be at capacity (active subs < ``max_users``);
      - among the remaining, pick the one with the fewest active subs —
        this gives us an even, load-aware spread without needing live metrics.
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
        # Re-acquire the row with a row-level lock so concurrent provisioners
        # can't both pick the same node right at its capacity limit. We also
        # re-check the live subscription count under the lock; if the node
        # filled up between the aggregate query and the lock, skip it.
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


def _collect_site_extra_vars(node: models.VPNNode) -> dict[str, Any]:
    """Build extra_vars for a node-level site.yml run.

    Currently only Reality is variable; ShadowTLS has no secrets the
    playbook needs to know (the manage script generates credentials at
    device-apply time). If we add more protocols this is the single place
    to extend.
    """
    extra: dict[str, Any] = {}
    for cfg in node.configs:
        if cfg.protocol != models.VPNConfigProtocol.vless_reality:
            continue
        if not cfg.is_enabled:
            continue
        settings = cfg.settings or {}
        priv_enc = settings.get("private_key_enc")
        if not priv_enc or not cfg.public_key:
            # Config is half-formed; let the role's own assert fail loudly.
            continue
        extra.update(
            {
                "vless_reality_private_key": decrypt(priv_enc),
                "vless_reality_public_key": cfg.public_key,
                "vless_reality_short_id": settings.get("short_id", ""),
                "vless_reality_port": cfg.port,
                "vless_reality_sni": cfg.sni or "",
                "vless_reality_dest": settings.get("dest") or cfg.fallback or "",
            }
        )
        break  # one Reality inbound per node
    return extra


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
        task.finished_at = utcnow()
        self.db.add(task)
        self.db.commit()
        TASK_STATUS_COUNTER.labels(status=status.value).inc()

    def run_task(self, task: models.ProvisioningTask, node: models.VPNNode | None = None) -> models.ProvisioningTask:
        task.started_at = utcnow()
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
        """Dispatch a provisioning task off the request thread.

        Preferred path is the RQ queue (``QUEUE_BACKEND=rq`` + ``REDIS_URL``):
        the job is picked up by ``app.worker`` in a separate container, which
        means a crashing API replica does not lose in-flight work and the API
        image does not have to carry ``ansible`` or SSH keys at all.

        If the queue is unavailable we fall back to the legacy in-process
        thread runner so that ``docker-compose up`` without Redis (and unit
        tests) keep working. The request-scoped ``self.db`` session is never
        shared across threads — the worker opens its own ``SessionLocal``.
        """
        from ..queue import enqueue_task

        job_id = enqueue_task(task.id, node.id if node else None)
        if job_id:
            logger.info("Task %s enqueued as RQ job %s", task.id, job_id)
            return

        # Queue unavailable. Allowing the API process to execute the playbook
        # inline would undo the image split (API doesn't ship ansible or SSH
        # keys) and hide broken infra behind "it works in dev". Require an
        # explicit opt-in env var for the dev/test fallback.
        if os.getenv("ALLOW_INPROCESS_PROVISIONING", "").lower() not in {"1", "true", "yes"}:
            raise RuntimeError(
                "Provisioning queue is unavailable and ALLOW_INPROCESS_PROVISIONING is not set; "
                "refusing to run ansible inside the API process."
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
                now = utcnow()
                for cred in device.credentials:
                    cred.is_active = False
                    cred.revoked_at = cred.revoked_at or now
        else:
            device.status = models.DeviceStatus.failed
        device.updated_at = utcnow()
        self.db.add(device)
        self.db.commit()

    def _execute_task(self, task: models.ProvisioningTask, node: models.VPNNode | None = None) -> dict[str, Any]:
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
                # Surface backend-authoritative Reality parameters to the
                # install_vless_reality role. We decrypt the private key
                # only for the duration of the ansible invocation — it is
                # never persisted in cleartext on the backend disk.
                site_vars = _collect_site_extra_vars(node)
                result = run_playbook(
                    "site.yml",
                    inventory,
                    limit=node.name,
                    extra_vars=site_vars,
                )
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
            # Always remove the temp inventory — ansible_runner creates it
            # with delete=False so it survives the subprocess, but it has
            # no owner after that.
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
            expires_at=expires_at_override or (utcnow() + timedelta(days=plan.duration_days)),
            traffic_limit_mb=plan.traffic_limit_mb,
        )
        self.db.add(subscription)
        self.db.flush()

        device_label = device_name or "primary"
        username = f"user-{user.id}-{subscription.id}"
        password = secrets.token_urlsafe(12)
        user_uuid: uuid.UUID | None = None
        credential_text: str
        if config.protocol == models.VPNConfigProtocol.shadowtls_ss:
            credential_text = _build_shadowtls_credential(node, config, username, password)
        else:
            user_uuid = uuid.uuid4()
            credential_text = _build_vless_reality_credential(node, config, str(user_uuid))

        # Encrypt connection URI at rest. We still keep the cleartext in memory
        # so that the HTTP response returned to the caller (e.g. the bot) can
        # render it to the user. Readers must pass DB values through
        # `security.decrypt` before using them.
        credential_enc = encrypt(credential_text)

        device = models.Device(
            user_id=user.id,
            subscription_id=subscription.id,
            config_id=config.id,
            name=device_label,
            status=models.DeviceStatus.pending,
            access_username=username,
            connection_uri=credential_enc,
        )
        self.db.add(device)
        self.db.flush()

        credential = models.Credential(
            subscription_id=subscription.id,
            device_id=device.id,
            config_id=config.id,
            proto=config.protocol.value,
            config_text=credential_enc,
        )
        self.db.add(credential)

        task_payload: dict[str, Any] = {
            "username": username,
            "port": config.port,
            "config_proto": config.protocol.value,
            "state": "present",
        }
        if config.protocol == models.VPNConfigProtocol.shadowtls_ss:
            task_payload["password"] = password
            task_payload["method"] = (config.settings or {}).get(
                "method", "chacha20-ietf-poly1305"
            )
        elif config.protocol == models.VPNConfigProtocol.vless_reality:
            # ``username`` doubles as the xray ``email`` (and therefore
            # the stats key in the traffic collector) — keep them
            # identical so Device.access_username remains the single
            # lookup key across probes, traffic, and audit.
            task_payload["uuid"] = str(user_uuid)
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
        # Preserve original expiry — the user still owns the time they paid
        # for. Status=blocked is what gates access; clobbering expires_at
        # would lose accounting data and break any "remaining time" logic
        # (e.g. migration between nodes).
        self.db.commit()
        return tasks
