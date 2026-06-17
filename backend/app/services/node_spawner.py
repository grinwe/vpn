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

import json
import logging
import os
import secrets
import socket
import threading
import time

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..db import SessionLocal
from ..security import decrypt, encrypt
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
from .vless import (
    generate_reality_keypair,
    generate_short_id,
    generate_wireguard_keypair,
)

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


# Заглушка host для ноды, заказанной через spawn_node_async, пока реальный IP
# не выдан хостером. Валидный IPv4 (проходит inventory-regex), SSH к нему падает
# мгновенно. Нода держится is_active=False (вне choose_node) до проставления
# настоящего IP — placeholder в credentials не попадает.
SPAWN_PLACEHOLDER_HOST = "0.0.0.0"

# Алфавит без спецсимволов — некоторые хостеры (4vps) валидируют рут-пароль и
# отклоняют спецсимволы; длина с запасом.
_PW_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_root_password(length: int = 20) -> str:
    """Рут-пароль для reinstall (провайдер сбрасывает его; нам нужно знать
    новый, чтобы зайти по паролю и переустановить provisioning-ключ)."""
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


def _display_region(driver, region_id: str) -> str:
    """Человекочитаемое имя региона по id датацентра хостера — для
    ``VPNNode.region`` (сырой id остаётся в ``provider_region``). Без этого в
    админке светилась голая «3» (id ДЦ VDSina/UFO) вместо «Russia». Резолвим
    через ``list_datacenters`` (id→country/name); драйверы без каталога ДЦ
    (hetzner и т.п.) или офлайн-сбой → фолбэк на сам id, как было раньше."""
    lister = getattr(driver, "list_datacenters", None)
    if not callable(lister):
        return str(region_id)
    try:
        for dc in lister():
            if str(dc.get("id")) == str(region_id):
                return str(dc.get("country") or dc.get("name") or region_id)
    except Exception:  # noqa: BLE001 — оффлайн-резолв имени не должен ронять заказ
        pass
    return str(region_id)


# Короткие префиксы хостеров для авто-имени ноды. billmgr — общий kind на
# UFO/AdminVPS/DataCheap → префикс берём из хоста base_url (см. _hoster_prefix).
_KIND_PREFIX = {
    "vdsina": "vdsina", "vdsina_ru": "vdsina", "4vps": "4vps", "aeza": "aeza",
    "hetzner": "hz", "vultr": "vultr", "digitalocean": "do",
}
# Страна (из _display_region) → 2-буквенный код для имени. Фолбэк — первые 2
# буквы региона (непокрытые страны).
_COUNTRY_CC = {
    "russia": "ru", "россия": "ru", "netherlands": "nl", "нидерланды": "nl",
    "denmark": "dk", "дания": "dk", "germany": "de", "германия": "de",
    "finland": "fi", "финляндия": "fi", "india": "in", "индия": "in",
    "usa": "us", "united states": "us", "сша": "us", "france": "fr",
    "poland": "pl", "sweden": "se", "kazakhstan": "kz", "казахстан": "kz",
    "turkey": "tr", "турция": "tr",
}


def _slug(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _hoster_prefix(provider) -> str:
    """Короткий префикс хостера для имени ноды. По ``kind``; для billmgr — из
    хоста ``base_url`` (``bill.ufo.hosting`` → ``ufo``); фолбэк — слаг имени
    провайдера."""
    kind = getattr(provider.kind, "value", str(provider.kind))
    if kind in _KIND_PREFIX:
        return _KIND_PREFIX[kind]
    if kind == "billmgr":
        try:
            tok = json.loads(decrypt(provider.api_token_enc) or "{}")
            host = (tok.get("base_url") or "").split("//")[-1].split("/")[0]
            parts = [
                p for p in host.split(".")
                if p not in ("bill", "www", "my", "cp", "panel", "billing")
            ]
            if parts:
                return _slug(parts[0]) or "vds"
        except Exception:  # noqa: BLE001 — не смогли распарсить → слаг имени
            pass
    return _slug(provider.name)[:8] or "node"


def _country_cc(region: str) -> str:
    r = (region or "").strip().lower()
    if r in _COUNTRY_CC:
        return _COUNTRY_CC[r]
    letters = "".join(ch for ch in r if ch.isalpha())
    return letters[:2] or "xx"


def _auto_node_name(db: Session, provider, region_display: str) -> str:
    """``<хостер>-<cc>-<NN>`` по конвенции. NN — следующий номер по ОБЕИМ
    таблицам (relay+exit), чтобы имена нод не сталкивались."""
    base = f"{_hoster_prefix(provider)}-{_country_cc(region_display)}-"
    nums: list[int] = []
    for model in (models.VPNNode, models.WGExitNode):
        for (nm,) in db.query(model.name).filter(model.name.like(base + "%")).all():
            tail = (nm or "")[len(base):]
            if tail.isdigit():
                nums.append(int(tail))
    nn = (max(nums) + 1) if nums else 1
    return f"{base}{nn:02d}"


def resolve_spawn_name(
    db: Session, provider_id: int, region: str, name: str | None = None
) -> str:
    """Имя ноды для заказа: явное от админа, иначе авто ``<хостер>-<cc>-<NN>``.
    Зовётся спавн-роутами перед заказом (autoscale-тик передаёт имя сам)."""
    if name and name.strip():
        return name.strip()
    provider = db.get(models.CloudProvider, provider_id)
    if not provider:
        raise NodeSpawnError(f"CloudProvider {provider_id} not found")
    return _auto_node_name(
        db, provider, _display_region(get_driver(provider), region)
    )


def _wait_for_ssh(
    host: str, port: int = 22, *, timeout_s: int | None = None, interval: int = 10
) -> bool:
    """Поллим TCP-доступность ``host:port``, пока не поднимется sshd.

    Свежий VPS (после заказа) и нода после reinstall грузятся несколько минут —
    bootstrap, стартовавший раньше, падает на ``No route to host`` /
    ``Connection refused``. Ждём ЗДЕСЬ, в фоновом daemon-потоке backend'а (нет
    RQ-таймаута), ПЕРЕД постановкой bootstrap-таски — тогда воркер запускает
    site.yml уже по доступной ноде и укладывается в RQ job_timeout (900s).

    Возвращает True, если порт открылся; False по таймауту (bootstrap всё равно
    ставим — key-inject/ansible-ретраи получат последний шанс).
    Окно настраивается через ``NODE_SSH_WAIT_TIMEOUT`` (сек, дефолт 480 = 8 мин).
    """
    if timeout_s is None:
        timeout_s = int(os.getenv("NODE_SSH_WAIT_TIMEOUT", "480"))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except OSError:
            time.sleep(interval)
    return False


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
        region=_display_region(driver, region),
        host=server.ipv4,
        status=models.VPNNodeStatus.registering,
        is_active=True,
        pool_id=pool_id,
        provider_id=provider.id,
        provider_external_id=server.external_id,
        provider_region=server.region,
        provider_plan=server.plan,
        monthly_cost=server.monthly_cost,
        # Хостеры без инъекции SSH-ключа (4vps) отдают рут-пароль при заказе —
        # храним зашифрованным для SSH-bootstrap'а (см. эпик, Фаза 1.5).
        provider_root_password_enc=(
            encrypt(server.root_password) if server.root_password else None
        ),
        notes=notes,
        health_score=100,
        last_health_check_at=utcnow(),
    )
    db.add(node)
    db.commit()
    db.refresh(node)

    # Авто-продление на стороне провайдера: чтобы заказанный VPS не удалился в
    # конце оплаченного периода (флот живёт без ручного continueServer).
    # Best-effort — не все провайдеры умеют, сбой не должен валить спавн.
    if hasattr(driver, "set_autoprolong"):
        try:
            driver.set_autoprolong(server.external_id, True)
            logger.info("autoprolong enabled for node %s (%s)", node.id, node.name)
        except Exception:  # noqa: BLE001
            logger.warning(
                "autoprolong enable failed for node %s (ignored)", node.id
            )

    # Auto-provision a VLESS+Reality VPNConfig for this node via the shared
    # helper so manual and automated registration go through the same path.
    ensure_reality_config(db, node, sni=reality_sni, dest=reality_dest)

    orchestrator = ProvisioningOrchestrator(db)
    # Свежеподнятая spawn-нода — bootstrap нужен немедленно (как в _finalize_spawn
    # ниже), НЕ дефёрим в reconciler-debounce.
    task, _created = orchestrator.create_or_coalesce_node_bootstrap(
        node, {"pool_id": pool_id, "auto_spawn": True}, defer_to_reconciler=False
    )
    db.commit()
    if _created:
        orchestrator.run_task_async(task, node=node)
    return node, task


def spawn_node_async(
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
) -> models.VPNNode:
    """Неблокирующий спавн для HTTP-роута ``POST /nodes/spawn``.

    ``create_server`` у всех драйверов БЛОКИРУЕТ до выдачи IP (4vps — поллинг
    /myservers до 600s). Прямой вызов в запросе убивал uvicorn-воркер по nginx
    ``proxy_read_timeout`` (60s) → CF 502; хуже того, ``buyServer`` оплачивал
    VPS ДО создания строки ``VPNNode``, и убитый запрос оставлял осиротевший,
    неотслеживаемый сервер (каждый ретрай — ещё один заказ).

    Здесь СИНХРОННО выполняем только быстрый ``order_server`` (buyServer,
    ~секунды) и сразу фиксируем ``VPNNode`` (placeholder host,
    ``is_active=False``, ``provider_external_id``) — сервер привязан к строке с
    момента заказа, сирот нет. Долгий поллинг IP + bootstrap уходят в фоновый
    daemon-поток (:func:`_finalize_spawn`). Нода становится ``is_active=True``
    (видимой для ``choose_node``) только когда проставлен реальный IP — до этого
    юзеры на неё не назначаются (placeholder не попадает в credentials).

    Драйверы без ``order_server`` (hetzner/vultr/…): здесь строка создаётся без
    ``external_id``, а полный (блокирующий) ``create_server`` уходит целиком в
    тот же фоновый поток. (Их основной путь — autoscale-тик в RQ-воркере, без
    HTTP-таймаута, через :func:`spawn_node`.)
    """
    provider = db.get(models.CloudProvider, provider_id)
    if not provider or not provider.is_active:
        raise NodeSpawnError(f"CloudProvider {provider_id} not found or inactive")

    try:
        validate_node_name(name)
    except InvalidNodeIdentity as exc:
        raise NodeSpawnError(str(exc)) from exc

    driver = get_driver(provider)
    image = image or provider.default_image or "ubuntu-22.04"
    ssh_key_ids = ssh_key_ids if ssh_key_ids is not None else (provider.ssh_key_ids or [])

    external_id: str | None = None
    root_password: str | None = None
    # Быстрый заказ — фиксируем external_id ДО создания строки, чтобы оплаченный
    # сервер сразу был привязан. Capability-проверка: 4vps умеет order_server.
    if hasattr(driver, "order_server"):
        logger.info("Ordering node %s via %s in %s", name, provider.name, region)
        try:
            external_id, root_password = driver.order_server(
                name=name,
                region=region,
                plan=plan,
                image=image,
                ssh_key_ids=[str(x) for x in ssh_key_ids] or None,
                user_data=user_data,
            )
        except DriverError as exc:
            logger.exception("Failed to order node via %s", provider.name)
            raise NodeSpawnError(str(exc)) from exc

    node = models.VPNNode(
        name=name,
        region=_display_region(driver, region),
        host=SPAWN_PLACEHOLDER_HOST,  # реальный IP проставит _finalize_spawn
        status=models.VPNNodeStatus.registering,
        is_active=False,  # вне choose_node, пока нет настоящего IP
        pool_id=pool_id,
        provider_id=provider.id,
        provider_external_id=external_id,
        provider_region=region,
        provider_plan=plan,
        provider_root_password_enc=(
            encrypt(root_password) if root_password else None
        ),
        notes=notes,
        health_score=100,
        last_health_check_at=utcnow(),
    )
    db.add(node)
    db.commit()
    db.refresh(node)

    # Reality-config можно создать сразу — он node-scoped и не требует host
    # (host вшивается в credentials позже, при выдаче). Нода уже несёт целевой
    # протокол в админ-UI.
    ensure_reality_config(db, node, sni=reality_sni, dest=reality_dest)

    threading.Thread(
        target=_finalize_spawn,
        args=(node.id,),
        kwargs={
            "name": name,
            "region": region,
            "plan": plan,
            "image": image,
            "ssh_key_ids": list(ssh_key_ids) if ssh_key_ids else None,
            "user_data": user_data,
            "reality_sni": reality_sni,
            "reality_dest": reality_dest,
        },
        daemon=True,
    ).start()
    return node


def _mark_spawn_error(session: Session, node: models.VPNNode) -> None:
    """Пометить ноду error+inactive (заказ не достроился). external_id уже в
    строке → оператор может снести/переустановить, сирот не остаётся."""
    node.status = models.VPNNodeStatus.error
    node.is_active = False
    node.updated_at = utcnow()
    session.add(node)
    session.commit()


def _finalize_spawn(
    node_id: int,
    *,
    name: str,
    region: str,
    plan: str,
    image: str,
    ssh_key_ids: list[str] | None,
    user_data: str | None,
    reality_sni: str | None,
    reality_dest: str | None,
) -> None:
    """Фоновая достройка ноды, заказанной через :func:`spawn_node_async`.

    Дожидается IP (``wait_for_ipv4`` — драйверы с ``order_server``) либо
    выполняет полный блокирующий ``create_server`` (драйверы без него),
    проставляет host + ``is_active=True`` и запускает bootstrap (site.yml).
    Крутится в daemon-потоке backend'а со своей сессией. При провале помечает
    ноду error (см. :func:`_mark_spawn_error`)."""
    session = SessionLocal()
    try:
        node = session.get(models.VPNNode, node_id)
        if not node:
            logger.error("spawn finalize: node %s vanished", node_id)
            return
        provider = session.get(models.CloudProvider, node.provider_id)
        if not provider:
            logger.error("spawn finalize: provider for node %s missing", node_id)
            _mark_spawn_error(session, node)
            return
        driver = get_driver(provider)

        try:
            if node.provider_external_id and hasattr(driver, "wait_for_ipv4"):
                ipv4, monthly_cost, _raw = driver.wait_for_ipv4(node.provider_external_id)
            else:
                # Драйвер без order_server — полный (блокирующий) заказ тут, в фоне.
                server = driver.create_server(
                    name=name,
                    region=region,
                    plan=plan,
                    image=image,
                    ssh_key_ids=[str(x) for x in (ssh_key_ids or [])] or None,
                    user_data=user_data,
                )
                ipv4 = server.ipv4
                monthly_cost = server.monthly_cost
                node.provider_external_id = server.external_id
                if server.root_password:
                    node.provider_root_password_enc = encrypt(server.root_password)
        except DriverError:
            logger.exception("spawn finalize: driver failed for node %s", node_id)
            _mark_spawn_error(session, node)
            return

        try:
            validate_node_identity_fields(node.name, ipv4, 22)
        except InvalidNodeIdentity:
            logger.exception("spawn finalize: invalid IPv4 %r for node %s", ipv4, node_id)
            _mark_spawn_error(session, node)
            return

        node.host = ipv4
        node.is_active = True  # реальный IP есть → нода доступна для choose_node
        if monthly_cost is not None:
            node.monthly_cost = monthly_cost
        node.updated_at = utcnow()
        session.add(node)
        session.commit()
        session.refresh(node)

        # Авто-продление на стороне провайдера (best-effort) — теперь точно есть
        # external_id. Сбой не должен валить достройку.
        if node.provider_external_id and hasattr(driver, "set_autoprolong"):
            try:
                driver.set_autoprolong(node.provider_external_id, True)
                logger.info("autoprolong enabled for node %s (%s)", node.id, node.name)
            except Exception:  # noqa: BLE001
                logger.warning("autoprolong enable failed for node %s (ignored)", node.id)

        # Ждём, пока на свежем VPS поднимется SSH, ПЕРЕД bootstrap'ом — иначе
        # site.yml стартует слишком рано и падает на 'No route to host'. Ждём
        # тут (фоновый поток, без RQ-таймаута), не в воркере.
        if _wait_for_ssh(node.host, node.ssh_port or 22):
            logger.info("spawn finalize: SSH up on %s", node.host)
        else:
            logger.warning(
                "spawn finalize: SSH on %s not up within wait window — enqueuing "
                "bootstrap anyway (key-inject/ansible will retry)", node.host,
            )

        orchestrator = ProvisioningOrchestrator(session)
        # defer_to_reconciler=False — нода только что поднялась, bootstrap нужен
        # сразу (как в reinstall_node), не ждём reconcile-тик.
        task, created = orchestrator.create_or_coalesce_node_bootstrap(
            node, {"pool_id": node.pool_id, "auto_spawn": True},
            defer_to_reconciler=False,
        )
        session.commit()
        if created:
            orchestrator.run_task_async(task, node=node)
        logger.info(
            "spawn finalize: node %s got IP %s, bootstrap enqueued", node_id, ipv4
        )
    except Exception:  # noqa: BLE001
        logger.exception("spawn finalize crashed for node %s", node_id)
        if session.is_active:
            session.rollback()
        try:
            node = session.get(models.VPNNode, node_id)
            if node and node.status == models.VPNNodeStatus.registering:
                _mark_spawn_error(session, node)
        except Exception:  # noqa: BLE001
            session.rollback()
    finally:
        session.close()


def destroy_node(db: Session, node: models.VPNNode) -> None:
    # Снести сервер у провайдера МОЖНО только если есть и провайдер, и его
    # external_id. У упавших заказов (нода ушла в error до выдачи external_id),
    # ручных нод и нод без провайдера сносить на стороне провайдера нечего —
    # просто гасим локальную строку (раньше тут был raise → такие ноды
    # вообще не удалялись из админки, см. баг с error-нодами).
    provider = (
        db.get(models.CloudProvider, node.provider_id) if node.provider_id else None
    )
    if node.provider_external_id and provider:
        driver = get_driver(provider)
        try:
            driver.destroy_server(node.provider_external_id)
        except DriverError as exc:
            raise NodeSpawnError(str(exc)) from exc
    else:
        logger.info(
            "destroy_node %s: нет provider_external_id/провайдера — гашу локально "
            "(ничего сносить у хостера)", node.id,
        )

    node.is_active = False
    node.status = models.VPNNodeStatus.disabled
    node.updated_at = utcnow()
    db.add(node)
    db.commit()


def _reboot_target(
    db: Session, provider_id: int | None, external_id: str | None,
    host: str, ssh_port: int | None,
) -> str:
    """Перезагрузка cloud/ручной ноды без захода в панель хостера: сначала
    hard-reboot через API провайдера (работает даже когда нода зависла), при
    отсутствии cloud-API или его сбое — graceful по SSH через provisioning-ключ.
    Возвращает использованный метод (``api``|``ssh``). NodeSpawnError, если оба
    пути недоступны (нет API и SSH не отвечает)."""
    from .ssh_bootstrap import reboot_via_ssh

    if provider_id and external_id:
        provider = db.get(models.CloudProvider, provider_id)
        if provider:
            driver = get_driver(provider)
            if hasattr(driver, "reboot_server"):
                try:
                    driver.reboot_server(external_id)
                    return "api"
                except DriverError as exc:
                    logger.warning(
                        "reboot: API reboot failed for %s (%s) — SSH fallback",
                        host, exc,
                    )
    if reboot_via_ssh(host, port=ssh_port or 22):
        return "ssh"
    raise NodeSpawnError(
        "reboot failed: no cloud-API reboot available and node SSH unreachable"
    )


def reboot_node(db: Session, node: models.VPNNode) -> str:
    """Перезагрузить relay-ноду (API hard-reboot → SSH-фолбэк)."""
    return _reboot_target(
        db, node.provider_id, node.provider_external_id,
        node.host, getattr(node, "ssh_port", None),
    )


def reboot_exit(db: Session, exit_node: models.WGExitNode) -> str:
    """Перезагрузить exit-ноду (API hard-reboot → SSH-фолбэк)."""
    return _reboot_target(
        db, exit_node.provider_id, exit_node.provider_external_id,
        exit_node.host, getattr(exit_node, "ssh_port", None),
    )


def reinstall_node(
    db: Session, node: models.VPNNode, *, image: str | None = None,
    password: str | None = None,
) -> tuple[models.VPNNode, models.ProvisioningTask | None]:
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
    # Reinstall СБРАСЫВАЕТ root-пароль. Генерим его сами (а не отдаём драйверу
    # на самогенерацию) и СОХРАНЯЕМ — иначе после переустановки мы не сможем
    # зайти по паролю и переустановить provisioning-ключ (4vps ключ не
    # инжектит), и bootstrap снова упадёт на Permission denied.
    if password is None:
        password = _gen_root_password()
    try:
        driver.reinstall_server(node.provider_external_id, img, password=password)
    except DriverError as exc:
        raise NodeSpawnError(str(exc)) from exc

    # Свежая ОС → нода ещё не настроена. Сохраняем новый пароль и возвращаем в
    # registering. Bootstrap НЕ пинаем сразу — нода уходит в ребут на несколько
    # минут, и ранний site.yml падает на 'No route to host'. Ждём SSH в фоне
    # (_reinstall_finalize), потом ставим bootstrap (по паролю положит ключ).
    node.provider_root_password_enc = encrypt(password)
    node.status = models.VPNNodeStatus.registering
    node.updated_at = utcnow()
    db.add(node)
    db.commit()
    db.refresh(node)

    threading.Thread(
        target=_reinstall_finalize, args=(node.id, img), daemon=True
    ).start()
    return node, None


def _reinstall_finalize(node_id: int, img: str) -> None:
    """Фоновая достройка после reinstall: дождаться, пока нода переедет в ребут
    и поднимет SSH, затем поставить bootstrap. Своя сессия (daemon-поток)."""
    session = SessionLocal()
    try:
        node = session.get(models.VPNNode, node_id)
        if not node:
            logger.error("reinstall finalize: node %s vanished", node_id)
            return
        if _wait_for_ssh(node.host, node.ssh_port or 22):
            logger.info("reinstall finalize: SSH up on %s", node.host)
        else:
            logger.warning(
                "reinstall finalize: SSH on %s not up within wait window — "
                "enqueuing bootstrap anyway", node.host,
            )
        orchestrator = ProvisioningOrchestrator(session)
        task, created = orchestrator.create_or_coalesce_node_bootstrap(
            node, {"reinstall": True, "image": img}, defer_to_reconciler=False
        )
        session.commit()
        if created:
            orchestrator.run_task_async(task, node=node)
    except Exception:  # noqa: BLE001
        logger.exception("reinstall finalize crashed for node %s", node_id)
        if session.is_active:
            session.rollback()
    finally:
        session.close()


def renew_node(db: Session, node: models.VPNNode) -> None:
    """Продлить аренду cloud-ноды через API провайдера (continueServer у 4vps).
    Списывает с баланса. Авто-продление обычно включается при заказе
    (set_autoprolong), это — ручной/принудительный путь."""
    if not node.provider_id or not node.provider_external_id:
        raise NodeSpawnError("Node has no attached cloud provider; cannot renew")
    provider = db.get(models.CloudProvider, node.provider_id)
    if not provider:
        raise NodeSpawnError("CloudProvider record missing")
    driver = get_driver(provider)
    if not hasattr(driver, "renew_server"):
        raise NodeSpawnError(
            f"Provider {provider.kind} driver does not support renewal"
        )
    try:
        driver.renew_server(node.provider_external_id)
    except DriverError as exc:
        raise NodeSpawnError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Cloud-spawned WG EXIT nodes (зеркало spawn_node_async для зарубежных exit'ов)
# ---------------------------------------------------------------------------


def spawn_exit_async(
    db: Session,
    *,
    provider_id: int,
    name: str,
    region: str,
    plan: str,
    image: str | None = None,
    ssh_key_ids: list[str] | None = None,
    user_data: str | None = None,
    notes: str | None = None,
) -> models.WGExitNode:
    """Неблокирующий заказ облачной EXIT-ноды — зеркало :func:`spawn_node_async`.

    Зарубежный VPS заводится как WG-exit ЗА РУ-relay: клиент его IP не видит,
    обходя DPI-троттлинг прямого зарубежного endpoint'а (см. диагноз в эпике).
    Синхронно — быстрый ``order_server`` + сразу фиксируем ``WGExitNode``
    (is_active=False, placeholder host, external_id, root-пароль, сгенерённый
    WG-keypair); поллинг IP + ``bootstrap_exit`` уходят в фоновый поток
    (:func:`_finalize_exit_spawn`). Драйверы без ``order_server`` — заказ
    целиком в фоне.
    """
    provider = db.get(models.CloudProvider, provider_id)
    if not provider or not provider.is_active:
        raise NodeSpawnError(f"CloudProvider {provider_id} not found or inactive")
    try:
        validate_node_name(name)
    except InvalidNodeIdentity as exc:
        raise NodeSpawnError(str(exc)) from exc
    if db.query(models.WGExitNode).filter(models.WGExitNode.name == name).first():
        raise NodeSpawnError(f"Exit node {name!r} already exists")

    driver = get_driver(provider)
    image = image or provider.default_image or "ubuntu-22.04"
    ssh_key_ids = ssh_key_ids if ssh_key_ids is not None else (provider.ssh_key_ids or [])

    external_id: str | None = None
    root_password: str | None = None
    if hasattr(driver, "order_server"):
        logger.info("Ordering exit %s via %s in %s", name, provider.name, region)
        try:
            external_id, root_password = driver.order_server(
                name=name,
                region=region,
                plan=plan,
                image=image,
                ssh_key_ids=[str(x) for x in ssh_key_ids] or None,
                user_data=user_data,
            )
        except DriverError as exc:
            logger.exception("Failed to order exit via %s", provider.name)
            raise NodeSpawnError(str(exc)) from exc

    public_key, private_key = generate_wireguard_keypair()
    exit_node = models.WGExitNode(
        name=name,
        region=_display_region(driver, region),
        host=SPAWN_PLACEHOLDER_HOST,  # реальный IP проставит _finalize_exit_spawn
        status=models.WGExitNodeStatus.registering,
        is_active=False,  # вне привязки, пока нет реального IP
        provider_id=provider.id,
        provider_external_id=external_id,
        provider_region=region,
        provider_root_password_enc=(
            encrypt(root_password) if root_password else None
        ),
        wg_public_key=public_key,
        wg_private_key_enc=encrypt(private_key),
        notes=notes,
    )
    db.add(exit_node)
    db.commit()
    db.refresh(exit_node)

    threading.Thread(
        target=_finalize_exit_spawn,
        args=(exit_node.id,),
        kwargs={
            "name": name,
            "region": region,
            "plan": plan,
            "image": image,
            "ssh_key_ids": list(ssh_key_ids) if ssh_key_ids else None,
            "user_data": user_data,
        },
        daemon=True,
    ).start()
    return exit_node


def _mark_exit_error(session: Session, exit_node: models.WGExitNode) -> None:
    """Пометить exit error+inactive (заказ не достроился). external_id уже в
    строке → оператор может снести/переустановить, сирот нет."""
    exit_node.status = models.WGExitNodeStatus.error
    exit_node.is_active = False
    exit_node.updated_at = utcnow()
    session.add(exit_node)
    session.commit()


def _finalize_exit_spawn(
    exit_id: int,
    *,
    name: str,
    region: str,
    plan: str,
    image: str,
    ssh_key_ids: list[str] | None,
    user_data: str | None,
) -> None:
    """Фоновая достройка облачной exit-ноды: дождаться IP, проставить host +
    is_active, дождаться SSH и запустить ``bootstrap_exit.yml``. Зеркало
    :func:`_finalize_spawn`."""
    session = SessionLocal()
    try:
        exit_node = session.get(models.WGExitNode, exit_id)
        if not exit_node:
            logger.error("exit spawn finalize: exit %s vanished", exit_id)
            return
        provider = session.get(models.CloudProvider, exit_node.provider_id)
        if not provider:
            logger.error("exit spawn finalize: provider for exit %s missing", exit_id)
            _mark_exit_error(session, exit_node)
            return
        driver = get_driver(provider)

        try:
            if exit_node.provider_external_id and hasattr(driver, "wait_for_ipv4"):
                ipv4, _cost, _raw = driver.wait_for_ipv4(exit_node.provider_external_id)
            else:
                server = driver.create_server(
                    name=name,
                    region=region,
                    plan=plan,
                    image=image,
                    ssh_key_ids=[str(x) for x in (ssh_key_ids or [])] or None,
                    user_data=user_data,
                )
                ipv4 = server.ipv4
                exit_node.provider_external_id = server.external_id
                if server.root_password:
                    exit_node.provider_root_password_enc = encrypt(server.root_password)
        except DriverError:
            logger.exception("exit spawn finalize: driver failed for exit %s", exit_id)
            _mark_exit_error(session, exit_node)
            return

        try:
            validate_node_identity_fields(exit_node.name, ipv4, 22)
        except InvalidNodeIdentity:
            logger.exception(
                "exit spawn finalize: invalid IPv4 %r for exit %s", ipv4, exit_id
            )
            _mark_exit_error(session, exit_node)
            return

        exit_node.host = ipv4
        exit_node.is_active = True
        exit_node.updated_at = utcnow()
        session.add(exit_node)
        session.commit()
        session.refresh(exit_node)

        if exit_node.provider_external_id and hasattr(driver, "set_autoprolong"):
            try:
                driver.set_autoprolong(exit_node.provider_external_id, True)
                logger.info("autoprolong enabled for exit %s (%s)", exit_node.id, name)
            except Exception:  # noqa: BLE001
                logger.warning("autoprolong enable failed for exit %s (ignored)", exit_id)

        if _wait_for_ssh(exit_node.host, exit_node.ssh_port or 22):
            logger.info("exit spawn finalize: SSH up on %s", exit_node.host)
        else:
            logger.warning(
                "exit spawn finalize: SSH on %s not up within wait window — "
                "enqueuing bootstrap anyway", exit_node.host,
            )

        orchestrator = ProvisioningOrchestrator(session)
        task = orchestrator.create_task("exit", exit_node.id, "bootstrap", {})
        session.commit()
        orchestrator.run_task_async(task)
        logger.info(
            "exit spawn finalize: exit %s got IP %s, bootstrap enqueued", exit_id, ipv4
        )
    except Exception:  # noqa: BLE001
        logger.exception("exit spawn finalize crashed for exit %s", exit_id)
        if session.is_active:
            session.rollback()
        try:
            exit_node = session.get(models.WGExitNode, exit_id)
            if exit_node and exit_node.status == models.WGExitNodeStatus.registering:
                _mark_exit_error(session, exit_node)
        except Exception:  # noqa: BLE001
            session.rollback()
    finally:
        session.close()
