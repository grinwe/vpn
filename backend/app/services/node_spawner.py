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

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..security import encrypt
from ..time_utils import utcnow
from .ansible_runner import (
    InvalidNodeIdentity,
    validate_node_identity_fields,
    validate_node_name,
)
from .cloud import DriverError, get_driver
from .provisioning import ProvisioningOrchestrator
from .shadowtls import (
    DEFAULT_HANDSHAKE_DOMAIN as SHADOWTLS_DEFAULT_SNI,
    DEFAULT_PORT as SHADOWTLS_DEFAULT_PORT,
    generate_shadowtls_password,
    generate_ss_password,
)
from .vless import generate_reality_keypair, generate_short_id

# Reality's "borrowed" SNI. Must be real TLS 1.3 host, НЕ заблокированный
# в целевом рынке — иначе Reality handshake перестаёт выглядеть легитимным.
# Пул RU-популярных доменов, распределяется по нодам через
# ``_pick_reality_sni`` — per-node рандомизация снижает blast radius
# RKN-события по одному SNI (июнь 2025: RKN душит connections с SNI вне
# whitelist после ~15-20KB). ``REALITY_SNI`` env-override форсит один
# SNI для всех новых нод (dev/test). ``REALITY_DEST`` нацеливается
# на ``<sni>:443`` — меняйте только вместе с sni.
REALITY_DEST_POOL: tuple[str, ...] = (
    "www.yandex.ru",
    "vk.ru",
    "mail.ru",
    "rutube.ru",
    "lenta.ru",
)
DEFAULT_REALITY_SNI = os.getenv("REALITY_SNI") or REALITY_DEST_POOL[0]
DEFAULT_REALITY_DEST = os.getenv("REALITY_DEST", f"{DEFAULT_REALITY_SNI}:443")
DEFAULT_REALITY_PORT = int(os.getenv("REALITY_PORT", "443"))
_REALITY_SNI_ENV_OVERRIDE: str | None = os.getenv("REALITY_SNI") or None

logger = logging.getLogger(__name__)


class NodeSpawnError(RuntimeError):
    pass


def pick_reality_sni(db: Session) -> str:
    """Выбор SNI из пула: наименее используемый среди уже сконфигурированных
    vless-reality нод. Разносим ноды по разным SNI чтобы RKN-событие по
    одному домену не клало весь флот. Env ``REALITY_SNI`` форсит один SNI
    для всех новых нод (dev/test override)."""
    if _REALITY_SNI_ENV_OVERRIDE:
        return _REALITY_SNI_ENV_OVERRIDE
    used: dict[str, int] = dict(
        db.query(models.VPNConfig.sni, func.count(models.VPNConfig.id))
        .filter(models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality)
        .group_by(models.VPNConfig.sni)
        .all()
    )
    return min(REALITY_DEST_POOL, key=lambda s: used.get(s, 0))


def ensure_reality_config(
    db: Session,
    node: models.VPNNode,
    *,
    port: int | None = None,
    sni: str | None = None,
    dest: str | None = None,
) -> models.VPNConfig:
    """Create a VLESS Reality VPNConfig for ``node`` if it has none yet.

    Used both by the auto-spawner and by the admin endpoint that bootstraps
    Reality on manually-registered nodes. Keys are generated here so every
    caller goes through the same authoritative path (see services/vless.py).
    Idempotent: if the node already has a Reality config, returns it as-is.

    When ``sni`` не передан — выбираем из ``REALITY_DEST_POOL`` наименее
    используемый домен (per-node рандомизация). Явный ``sni`` имеет
    приоритет.
    """
    existing = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality,
        )
        .first()
    )
    if existing is not None:
        return existing

    public_key, private_key = generate_reality_keypair()
    short_id = generate_short_id()
    sni_value = sni or pick_reality_sni(db)
    dest_value = dest or f"{sni_value}:443"
    cfg = models.VPNConfig(
        node_id=node.id,
        name=f"{node.name}-vless-reality",
        protocol=models.VPNConfigProtocol.vless_reality,
        port=port or DEFAULT_REALITY_PORT,
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
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def ensure_shadowtls_config(
    db: Session,
    node: models.VPNNode,
    *,
    port: int | None = None,
    handshake_domain: str | None = None,
    name: str | None = None,
) -> models.VPNConfig:
    """Create a ShadowTLS+SS VPNConfig for ``node`` if it has none yet.

    Mirrors :func:`ensure_reality_config`: generates both the outer
    shadow-tls password and the inner ss-rust PSK, stores them
    encrypted in ``settings``, and exposes only the handshake domain
    + port via the plain columns. The ansible role decrypts them via
    ``_collect_site_extra_vars`` on every site.yml run.
    """
    existing = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.shadowtls_ss,
        )
        .first()
    )
    if existing is not None:
        return existing

    ss_password = generate_ss_password()
    stls_password = generate_shadowtls_password()
    cfg = models.VPNConfig(
        node_id=node.id,
        name=name or f"{node.name}-shadowtls",
        protocol=models.VPNConfigProtocol.shadowtls_ss,
        port=port or SHADOWTLS_DEFAULT_PORT,
        sni=handshake_domain or SHADOWTLS_DEFAULT_SNI,
        public_key=None,
        fallback=None,
        settings={
            "ss_password_enc": encrypt(ss_password),
            "shadowtls_password_enc": encrypt(stls_password),
        },
        is_enabled=True,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


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

    # #55 — name validation runs *before* we pay for the cloud VM. If
    # the caller (autoscale, /nodes/spawn, admin script) constructed a
    # name that wouldn't be safe in an ansible inventory, fail fast —
    # renting a machine we can't provision is strictly worse than a
    # clear rejection.
    try:
        validate_node_name(name)
    except InvalidNodeIdentity as exc:
        raise NodeSpawnError(str(exc)) from exc

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

    # #55 — host+port re-validation once the cloud driver returns.
    # ``server.ipv4`` should always be a real IPv4 string, but any
    # future driver that returns a malformed value (or we add IPv6
    # support and forget to update one path) will still be caught
    # before the row is committed or the inventory is rendered.
    try:
        validate_node_identity_fields(name, server.ipv4, 22)
    except InvalidNodeIdentity as exc:
        raise NodeSpawnError(
            f"cloud driver {provider.name} returned invalid host: {exc}"
        ) from exc

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

    # Auto-provision a VLESS+Reality VPNConfig for this node via the shared
    # helper so manual and automated registration go through the same path.
    ensure_reality_config(db, node, sni=reality_sni, dest=reality_dest)

    orchestrator = ProvisioningOrchestrator(db)
    task, _created = orchestrator.create_or_coalesce_node_bootstrap(
        node, {"pool_id": pool_id, "auto_spawn": True}
    )
    db.commit()
    if _created:
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


def reinstall_node(
    db: Session, node: models.VPNNode, *, image: str | None = None,
    password: str | None = None,
) -> tuple[models.VPNNode, models.ProvisioningTask]:
    """Переустановить ОС на ноде через API провайдера, затем заново
    прокатить site.yml (reinstall стирает диск — клиентов на ноде не остаётся,
    бэк их пере-провижинит через bootstrap). IP сохраняется, поэтому VPNConfig
    (reality-ключи, sub-токены) и так валидны — нода вернётся той же.

    Провайдер обязан уметь ``reinstall_server`` (capability-проверка через
    hasattr; напр. 4vps умеет, manual — нет)."""
    if not node.provider_id or not node.provider_external_id:
        raise NodeSpawnError("Node has no attached cloud provider; cannot reinstall")
    provider = db.get(models.CloudProvider, node.provider_id)
    if not provider:
        raise NodeSpawnError("CloudProvider record missing")
    driver = get_driver(provider)
    if not hasattr(driver, "reinstall_server"):
        raise NodeSpawnError(
            f"Provider {provider.kind} driver does not support OS reinstall"
        )
    img = image or provider.default_image or "ubuntu-22.04"
    try:
        driver.reinstall_server(node.provider_external_id, img, password=password)
    except DriverError as exc:
        raise NodeSpawnError(str(exc)) from exc

    # Свежая ОС → нода ещё не настроена. Возвращаем в registering и заново
    # катим bootstrap (site.yml) — ansible-ретраи дождутся, пока SSH поднимется.
    node.status = models.VPNNodeStatus.registering
    node.updated_at = utcnow()
    db.add(node)
    db.commit()
    db.refresh(node)

    orchestrator = ProvisioningOrchestrator(db)
    task, _created = orchestrator.create_or_coalesce_node_bootstrap(
        node, {"reinstall": True, "image": img}, defer_to_reconciler=False
    )
    db.commit()
    if _created:
        orchestrator.run_task_async(task, node=node)
    return node, task
