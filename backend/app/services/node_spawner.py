"""Orchestration for automated VPN node creation via cloud providers.

Flow:
    1. Read CloudProvider record, instantiate driver.
    2. Ask driver to create a server (blocks until running).
    3. Record VPNNode with provider metadata, status=registering.
    4. Enqueue a provisioning task that runs the Ansible ``site.yml`` on the
       new host. Once that succeeds the health checker will flip it to active.

This is intentionally synchronous with respect to cloud-API polling: creating
a single VM is a 30-90s operation and an upstream ARQ/Celery worker is the
right place to call this from so we don't block HTTP requests. For now the
API route calls it in a background thread, mirroring the existing
provisioning pattern.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

from .. import models
from ..security import encrypt
from ..time_utils import utcnow
from .cloud import DriverError, get_driver
from .provisioning import ProvisioningOrchestrator
from .vless import generate_reality_keypair, generate_short_id

# Reality's "borrowed" SNI. Must be a real TLS 1.3 host that is NOT
# censored in the target market — if it is, the whole Reality handshake
# stops looking legitimate. The default is Microsoft (stays up in RU as
# of 2026-04); override per deployment via env, or per spawn via the
# ``reality_sni`` argument below (e.g. autoscaler picking region-specific
# camouflage). ``REALITY_DEST`` can be set to point somewhere else than
# ``<sni>:443`` but 99% of the time you want them aligned.
DEFAULT_REALITY_SNI = os.getenv("REALITY_SNI", "www.microsoft.com")
DEFAULT_REALITY_DEST = os.getenv("REALITY_DEST", f"{DEFAULT_REALITY_SNI}:443")
DEFAULT_REALITY_PORT = int(os.getenv("REALITY_PORT", "443"))

logger = logging.getLogger(__name__)


class NodeSpawnError(RuntimeError):
    pass


def spawn_node(
    db: Session,
    *,
    provider_id: int,
    name: str,
    region: str,
    plan: str,
    image: str | None = None,
    ssh_key_ids: list[str] | None = None,
    pool_id: int | None = None,
    user_data: str | None = None,
    notes: str | None = None,
    reality_sni: str | None = None,
    reality_dest: str | None = None,
) -> tuple[models.VPNNode, models.ProvisioningTask]:
    provider = db.get(models.CloudProvider, provider_id)
    if not provider or not provider.is_active:
        raise NodeSpawnError(f"CloudProvider {provider_id} not found or inactive")

    driver = get_driver(provider)
    image = image or provider.default_image or "ubuntu-22.04"
    ssh_key_ids = ssh_key_ids if ssh_key_ids is not None else (provider.ssh_key_ids or [])

    logger.info("Spawning node %s via %s in %s", name, provider.name, region)
    try:
        server = driver.create_server(
            name=name,
            region=region,
            plan=plan,
            image=image,
            ssh_key_ids=[str(x) for x in ssh_key_ids] or None,
            user_data=user_data,
        )
    except DriverError as exc:
        logger.exception("Failed to spawn node via %s", provider.name)
        raise NodeSpawnError(str(exc)) from exc

    node = models.VPNNode(
        name=name,
        region=region,
        host=server.ipv4,
        status=models.VPNNodeStatus.registering,
        is_active=True,
        pool_id=pool_id,
        provider_id=provider.id,
        provider_external_id=server.external_id,
        provider_region=server.region,
        provider_plan=server.plan,
        monthly_cost=server.monthly_cost,
        notes=notes,
        health_score=100,
        last_health_check_at=utcnow(),
    )
    db.add(node)
    db.commit()
    db.refresh(node)

    # Auto-provision a VLESS+Reality VPNConfig for this node. Keys are
    # authoritative in the backend — see services/vless.py for the
    # rationale. The private key is stored encrypted in ``settings``
    # (never exposed over the API), public key and shortId live in
    # dedicated columns since they are client-visible anyway.
    public_key, private_key = generate_reality_keypair()
    short_id = generate_short_id()
    sni_value = reality_sni or DEFAULT_REALITY_SNI
    dest_value = reality_dest or (f"{sni_value}:443" if reality_sni else DEFAULT_REALITY_DEST)
    vless_config = models.VPNConfig(
        node_id=node.id,
        name=f"{name}-vless-reality",
        protocol=models.VPNConfigProtocol.vless_reality,
        port=DEFAULT_REALITY_PORT,
        sni=sni_value,
        public_key=public_key,
        fallback=dest_value,
        settings={
            "private_key_enc": encrypt(private_key),
            "short_id": short_id,
            "dest": dest_value,
        },
        is_enabled=True,
    )
    db.add(vless_config)
    db.commit()

    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task(
        "node", node.id, "bootstrap", {"pool_id": pool_id, "auto_spawn": True}
    )
    db.commit()
    orchestrator.run_task_async(task, node=node)
    return node, task


def destroy_node(db: Session, node: models.VPNNode) -> None:
    if not node.provider_id or not node.provider_external_id:
        raise NodeSpawnError("Node has no attached cloud provider; cannot destroy automatically")
    provider = db.get(models.CloudProvider, node.provider_id)
    if not provider:
        raise NodeSpawnError("CloudProvider record missing")
    driver = get_driver(provider)
    try:
        driver.destroy_server(node.provider_external_id)
    except DriverError as exc:
        raise NodeSpawnError(str(exc)) from exc

    node.is_active = False
    node.status = models.VPNNodeStatus.disabled
    node.updated_at = utcnow()
    db.add(node)
    db.commit()
