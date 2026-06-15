"""VPN node admin endpoints: ``/api/nodes/*``.

Covers the CRUD surface for VPN nodes plus all the operations layered
on top of them: bootstrap/rerun, diagnose, resync, active toggle,
configs, spawn/destroy, health lookup and force-migrate. The probe
ingest and traffic endpoints that also live under ``/nodes/{id}/...``
are factored into ``probes.py`` and ``traffic.py`` respectively —
FastAPI stitches them back together when the master router includes
both modules.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..services.ansible_runner import (
    InvalidNodeIdentity,
    validate_node_identity_fields,
    validate_node_name,
)
from ..services.health import recompute_node_health
from ..services.node_spawner import (
    NodeSpawnError,
    destroy_node,
    reinstall_node,
    renew_node,
    spawn_node_async,
)
from ..services.provisioning import ProvisioningOrchestrator
from ._common import (
    ADMIN_ACTOR_HEADER,
    _audit,
    _resolve_admin_actor,
    get_db,
    logger,
)

router = APIRouter()


@router.post("/nodes", response_model=schemas.VPNNodeOut)
def create_node(
    payload: schemas.VPNNodeCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    # #55 — whitelist the three fields that end up in a dynamically
    # rendered ansible inventory (name/host/ssh_port). Defence-in-depth:
    # the service layer re-validates at render time, but catching it here
    # gives the operator a 400 with a clear reason instead of a bootstrap
    # task that crashes later.
    try:
        validate_node_identity_fields(payload.name, payload.host, payload.ssh_port)
    except InvalidNodeIdentity as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    node = models.VPNNode(
        name=payload.name,
        region=payload.region,
        host=payload.host,
        ssh_port=payload.ssh_port,
        pool_id=payload.pool_id,
        notes=payload.notes,
        status=models.VPNNodeStatus.registering,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "node_created", "vpn_node", node.id, actor_type=actor_type)
    orchestrator = ProvisioningOrchestrator(db)
    # Ручное создание ноды — нужна немедленная bootstrap-таска (оператор ждёт
    # провижининг + трекаемую таску), НЕ дефёрим в reconciler-debounce.
    task, _created = orchestrator.create_or_coalesce_node_bootstrap(
        node, {"pool_id": payload.pool_id}, defer_to_reconciler=False
    )
    db.commit()
    if _created:
        orchestrator.run_task_async(task, node=node)
    return node


def _ws_cdn_resolve_ip(host: str) -> str:
    """node.host may be an IP or a hostname; CF A-records need an IPv4."""
    import ipaddress
    import socket

    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return socket.gethostbyname(host)


def _provision_cf_subdomain(
    db: Session, node: models.VPNNode, config: models.VPNConfig
) -> None:
    """Mint a **DNS-only** subdomain → node IP and pin it onto the config
    (``sni`` + ``settings.cf_record_id/cf_subdomain``).

    Applies to: ws-cdn (always — its sni is always the minted subdomain) and
    xhttp when the operator left ``sni`` empty (auto-front mode; a non-empty
    sni = classic direct+LE xhttp, left untouched). The record is grey-cloud
    (proxied=False) → resolves straight to the node, which serves TLS itself
    (LE per subdomain). CF is DNS only — proxying WS/xhttp is dead (RKN; see
    project_cf_ws_cdn_dead). Fails loud (HTTPException) if CF isn't configured
    or the API errors.
    """
    from ..services import cloudflare_dns

    proto = config.protocol
    if proto == models.VPNConfigProtocol.vless_ws_cdn:
        # ws-cdn sni is always the minted subdomain.
        domain = cloudflare_dns.front_domain()
    elif proto == models.VPNConfigProtocol.vless_xhttp and not config.sni:
        # empty sni → auto-front xhttp. Single zone now (wgse.info): xhttp
        # shares the ws-cdn front; grwr.ink is retired (DNS-only removed the
        # cert-clobber that forced the per-protocol zone split).
        domain = cloudflare_dns.front_domain()
    else:
        return  # other protocols, or xhttp with an explicit (direct) domain

    if not cloudflare_dns.is_configured(domain):
        raise HTTPException(
            status_code=400,
            detail=(
                "DNS front requires CLOUDFLARE_DNS_TOKEN + WSCDN_FRONT_DOMAIN "
                "on the backend"
            ),
        )
    try:
        ip = _ws_cdn_resolve_ip(node.host)
        # proxied=False → DNS-only A → node IP directly; node serves TLS (LE).
        rec = cloudflare_dns.create_node_record(ip, domain=domain, proxied=False)
    except cloudflare_dns.CloudflareError as exc:
        raise HTTPException(
            status_code=502, detail=f"Cloudflare DNS failed: {exc}"
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=400, detail=f"Cannot resolve node host {node.host!r}: {exc}"
        ) from exc

    config.sni = rec["subdomain"]
    settings = dict(config.settings or {})
    settings["cf_record_id"] = rec["record_id"]
    settings["cf_subdomain"] = rec["subdomain"]
    settings["cf_front_domain"] = rec["front_domain"]  # zone for teardown
    config.settings = settings
    db.add(config)
    db.commit()
    db.refresh(config)


def _teardown_cf_subdomain(config: models.VPNConfig) -> None:
    """Best-effort delete the DNS-only CF record bound to a config (ws-cdn or
    auto xhttp) — idempotent. Keyed on cf_record_id (+ stored cf_front_domain
    for the zone), so it's protocol-agnostic and still tears down legacy
    proxied/grwr.ink records: a config without a record is a no-op."""
    settings = config.settings or {}
    record_id = settings.get("cf_record_id")
    if not record_id:
        return
    from ..services import cloudflare_dns

    # Delete in the zone the record was minted in (ws→wgse, xhttp→grwr).
    # Older configs predate cf_front_domain → fall back to the default front.
    cloudflare_dns.delete_record(
        record_id, domain=settings.get("cf_front_domain")
    )


def _build_config_from_payload(
    db: Session, node: models.VPNNode, payload: schemas.VPNConfigCreate
) -> models.VPNConfig:
    """Создать VPNConfig из payload с auto-keygen для REALITY/shadowtls.

    Reuse'ит логику ``create_config`` (см. ниже), но без bootstrap-task'и
    в конце — caller сам решает когда планировать bootstrap (в composite
    flow — один общий для всех configs).
    """
    try:
        protocol = models.VPNConfigProtocol(payload.protocol)
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"Unknown protocol: {payload.protocol}"
        ) from exc

    existing_same = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == protocol,
        )
        .first()
    )
    if existing_same is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Config for {protocol.value} already exists on this node "
                f"(id={existing_same.id})"
            ),
        )

    if protocol == models.VPNConfigProtocol.vless_reality and not payload.public_key:
        from ..services.node_spawner import ensure_reality_config
        return ensure_reality_config(
            db, node,
            port=payload.port or None,
            sni=payload.sni or None,
            dest=payload.fallback or None,
        )
    if protocol == models.VPNConfigProtocol.shadowtls_ss and not (
        payload.settings or {}
    ).get("ss_password_enc"):
        from ..services.node_spawner import ensure_shadowtls_config
        return ensure_shadowtls_config(
            db, node,
            port=payload.port or None,
            handshake_domain=payload.sni or None,
            name=payload.name or None,
        )

    settings = dict(payload.settings or {})
    # vless-xhttp: nginx-фронт matchит SNI с settings.domain — без
    # дублирования у админа задача «sni vs settings.domain» расходилась
    # после DR (sni был, domain пустой → fix_xhttp_sni.sh). Auto-fill
    # сохраняет explicit значение, если админ его задал.
    if (
        protocol == models.VPNConfigProtocol.vless_xhttp
        and payload.sni
        and not settings.get("domain")
    ):
        settings["domain"] = payload.sni

    config = models.VPNConfig(
        node_id=node.id,
        name=payload.name,
        protocol=protocol,
        port=payload.port,
        sni=payload.sni,
        public_key=payload.public_key,
        fallback=payload.fallback,
        settings=settings or None,
        is_enabled=payload.is_enabled,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    try:
        _provision_cf_subdomain(db, node, config)
    except HTTPException:
        db.delete(config)
        db.commit()
        raise
    return config


@router.post("/nodes/with-configs", response_model=schemas.VPNNodeOut)
def create_node_with_configs(
    payload: schemas.VPNNodeWithConfigsCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Создать ноду + N VPN-конфигов + один bootstrap одним запросом.

    Заменяет последовательность «POST /nodes → POST /configs × N», где
    каждый шаг плодил свой bootstrap-task. Здесь все INSERT'ы делаются
    в логическом блоке + один bootstrap-task в конце c ``initial=True``
    в payload — site.yml на ноде сразу видит финальный набор протоколов.

    Best-effort atomicity: при exception в середине rollback'аем ноду
    + успешно созданные configs ручным cleanup'ом (полностью atomic
    require'ло бы non-committing варианты ensure_reality/shadowtls'ов).
    Для админ-UX этого достаточно: при провале юзер видит чистое
    состояние и пробует заново.
    """
    try:
        validate_node_identity_fields(
            payload.node.name, payload.node.host, payload.node.ssh_port
        )
    except InvalidNodeIdentity as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    protocols_seen: set[str] = set()
    for cfg in payload.configs:
        if cfg.protocol in protocols_seen:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate protocol in configs: {cfg.protocol}",
            )
        protocols_seen.add(cfg.protocol)
        try:
            models.VPNConfigProtocol(cfg.protocol)
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail=f"Unknown protocol: {cfg.protocol}"
            ) from exc

    node = models.VPNNode(
        name=payload.node.name,
        region=payload.node.region,
        host=payload.node.host,
        ssh_port=payload.node.ssh_port,
        pool_id=payload.node.pool_id,
        notes=payload.node.notes,
        status=models.VPNNodeStatus.registering,
    )
    db.add(node)
    db.commit()
    db.refresh(node)

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "node_created", "vpn_node", node.id, actor_type=actor_type)

    created_configs: list[models.VPNConfig] = []
    try:
        for cfg_payload in payload.configs:
            cfg = _build_config_from_payload(db, node, cfg_payload)
            created_configs.append(cfg)
            _audit(
                db, actor, "config_created", "vpn_config", cfg.id,
                actor_type=actor_type,
            )
    except HTTPException:
        # Rollback: убираем уже созданные configs + ноду. Юзер видит
        # чистое состояние и понимает что весь composite запрос
        # провалился (без orphan'ной ноды без configs).
        for cfg in created_configs:
            _teardown_cf_subdomain(cfg)  # release CF record before dropping the row
            db.delete(cfg)
        db.delete(node)
        db.commit()
        raise

    orchestrator = ProvisioningOrchestrator(db)
    # Первичный bootstrap новой ноды — немедленно (НЕ дефёрим): оператор ждёт,
    # пока нода поднимется, и трекает таску. initial=True не коалесить.
    task, _created = orchestrator.create_or_coalesce_node_bootstrap(
        node,
        {
            "pool_id": payload.node.pool_id,
            "initial": True,
            "config_change": len(created_configs) > 0,
        },
        defer_to_reconciler=False,
    )
    db.commit()
    if _created:
        orchestrator.run_task_async(task, node=node)
    return node


@router.get("/nodes", response_model=list[schemas.VPNNodeOut])
def list_nodes(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    region: str | None = None,
    pool_id: int | None = None,
    is_active: bool | None = None,
):
    query = db.query(models.VPNNode)
    if status_filter:
        try:
            query = query.filter(models.VPNNode.status == models.VPNNodeStatus(status_filter))
        except ValueError as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid status") from exc
    if region:
        query = query.filter(models.VPNNode.region == region)
    if pool_id is not None:
        query = query.filter(models.VPNNode.pool_id == pool_id)
    if is_active is not None:
        query = query.filter(models.VPNNode.is_active.is_(is_active))
    nodes = (
        # id как вторичный ключ — детерминированный тайбрейкер. Без него
        # ноды с равным (или NULL после DR-restore) created_at возвращались
        # в физическом heap-порядке, который меняется на UPDATE строки
        # (bootstrap/любое действие) → нода «перепрыгивала» в списке админки.
        query.order_by(models.VPNNode.created_at, models.VPNNode.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    # Bulk-load relay→exit links для health-dots в строке. Грузим одной
    # JOIN-кой (вместо per-row), потом раскладываем по relay_node_id.
    relay_ids = [n.id for n in nodes if n.relay_config is not None]
    links_by_relay: dict[int, list[models.RelayExitLink]] = {}
    if relay_ids:
        links = (
            db.query(models.RelayExitLink)
            .join(
                models.WGExitNode,
                models.WGExitNode.id == models.RelayExitLink.exit_id,
            )
            .filter(models.RelayExitLink.relay_node_id.in_(relay_ids))
            .all()
        )
        for link in links:
            links_by_relay.setdefault(link.relay_node_id, []).append(link)

    # max(observed_at) на NodeTrafficSample = «когда tick последний раз
    # реально дошёл до ноды по SSH и xray отдал stats». Одной GROUP BY
    # вместо N отдельных запросов; по node_id уже есть индекс.
    node_ids = [n.id for n in nodes]
    last_ssh_by_node: dict[int, datetime] = {}
    if node_ids:
        rows = (
            db.query(
                models.NodeTrafficSample.node_id,
                func.max(models.NodeTrafficSample.observed_at).label("latest"),
            )
            .filter(models.NodeTrafficSample.node_id.in_(node_ids))
            .group_by(models.NodeTrafficSample.node_id)
            .all()
        )
        last_ssh_by_node = {node_id: latest for node_id, latest in rows}

    # active_users из ПОСЛЕДНЕГО сэмпла per-node (DISTINCT ON node_id ORDER BY
    # observed_at DESC). Нужно для idle-aware health-dots: 0 юзеров + протухший
    # WG-handshake = простой, не обрыв (см. linkHealth.tsx). Колонки
    # active_users на vpn_nodes НЕТ — она живёт только в node_traffic_samples.
    active_users_by_node: dict[int, int] = {}
    if node_ids:
        au_rows = (
            db.query(
                models.NodeTrafficSample.node_id,
                models.NodeTrafficSample.active_users,
            )
            .filter(models.NodeTrafficSample.node_id.in_(node_ids))
            .order_by(
                models.NodeTrafficSample.node_id,
                models.NodeTrafficSample.observed_at.desc(),
            )
            .distinct(models.NodeTrafficSample.node_id)
            .all()
        )
        active_users_by_node = {nid: au for nid, au in au_rows}

    def _to_out(n: models.VPNNode) -> schemas.VPNNodeOut:
        out = schemas.VPNNodeOut.from_orm(n)
        out.exit_links = [
            schemas.NodeExitLinkHealthMini(
                exit_id=link.exit_id,
                exit_name=link.exit_node.name if link.exit_node else "",
                wg_interface_name=link.wg_interface_name,
                last_handshake_at=link.last_handshake_at,
                last_observed_at=link.last_observed_at,
            )
            for link in links_by_relay.get(n.id, [])
        ]
        out.last_ssh_at = last_ssh_by_node.get(n.id)
        out.active_users = active_users_by_node.get(n.id, 0)
        # Reconciler-видимость: нода помечена dirty (desired бампнут правкой),
        # но прогон ещё отложен на тик. reconcile_due_at уже подтянут from_orm.
        out.reconcile_pending = n.desired_generation > n.reconciled_generation
        return out

    return [_to_out(n) for n in nodes]


@router.get("/nodes/carrying-fractions")
def list_carrying_fractions(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Phase C — per-node ``carrying_fraction`` (детект блокировок по поведению
    клиентов). Read-only. = (девайсов сейчас на ноде) / (девайсов, у кого нода в
    наборе). Осмыслен при ``DIVERSE_SUB_NODES``>1; устойчивый тренд вниз = нода
    широко заблокирована (клиенты ушли на 2nd-best). См.
    docs/operations/diverse_subscription_epic.md."""
    from ..services.carrying import compute_carrying_fractions

    return {"nodes": compute_carrying_fractions(db)}


@router.post("/nodes/{node_id}/resync")
def resync_node_clients(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Force re-push of all vless-family users onto a node.

    Safety net for the "empty clients after site.yml" class of bugs:
    if an operator suspects the node has drifted from the backend's
    view of who's provisioned (e.g. after a manual config edit, a
    restore-from-backup, or a half-broken bootstrap), this endpoint
    enqueues a resync task that re-adds every active credential via
    manage_vless_*_user.sh. The helper is idempotent so running this
    in any node state is safe.

    Covers vless_reality, vless_xhttp and vless_ws_cdn — anything
    that shares the "invalid request user id" failure mode — and
    also pushes warm-pool bundles that are bound to the node but
    not yet assigned to a subscription.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.resync_node_clients(node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_resync",
        "vpn_node",
        node.id,
        metadata={"task_id": task.id if task else None},
        actor_type=actor_type,
    )
    if not task:
        return {"node_id": node.id, "task_id": None, "clients": 0}
    payload = task.payload or {}
    clients_by_proto = payload.get("clients_by_proto") or {}
    total = sum(len(v) for v in clients_by_proto.values()) if clients_by_proto else len(
        payload.get("clients", [])
    )
    return {
        "node_id": node.id,
        "task_id": task.id,
        "clients": total,
        "clients_by_proto": {
            proto: len(v) for proto, v in clients_by_proto.items()
        },
    }


@router.post("/nodes/{node_id}/backfill-missing-creds")
def backfill_missing_creds(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Backfill ``Credential`` rows for every enabled config on the node.

    Fixes the historical gap where adding a protocol to a node only wrote
    credentials for subscriptions provisioned *after* the add. Iterates
    the enabled configs and hands each to
    ``ProvisioningOrchestrator.backfill_credentials_for_new_config`` —
    the helper is idempotent (skips devices that already have a
    Credential for that config_id) so re-running is safe and a no-op on
    a healthy node. Once credentials exist, the node-level auto-resync
    path picks them up and pushes the new protocol into the server's
    xray config; ``/sub/{token}`` then returns the missing protocol.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    orchestrator = ProvisioningOrchestrator(db)
    created: dict[int, int] = {}
    total_created = 0
    for cfg in node.configs:
        if not cfg.is_enabled:
            continue
        n = orchestrator.backfill_credentials_for_new_config(node, cfg)
        if n:
            created[cfg.id] = n
            total_created += n

    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "node_backfill_creds", "vpn_node", node.id,
        actor_type=actor_type,
        metadata={"created": created, "total_created": total_created},
    )
    return {
        "node_id": node.id,
        "created": created,
        "total_created": total_created,
    }


@router.post("/nodes/{node_id}/bootstrap")
def rebootstrap_node(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Re-run site.yml against an existing node from scratch.

    Use when you want to reapply the full ansible role stack to a node
    — rolling out role changes, recovering from a half-broken manual
    edit, rotating keys, etc. Creates a fresh ``bootstrap`` task (same
    action the initial node creation uses) and hands it to the
    orchestrator. The install_vless_reality role preserves existing
    clients across re-renders, and the post-site.yml auto-resync
    covers the edge case where it can't (see NODES.md § VLESS client
    resync), so running this on a node with active users is safe.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    orchestrator = ProvisioningOrchestrator(db)
    # Явная кнопка «перекатить site.yml» — оператор ждёт НЕМЕДЛЕННУЮ таску,
    # поэтому НЕ дефёрим в reconciler (иначе при RECONCILER_ENABLED=1 кнопка
    # молча возвращала (None, False) и «висела» — root-фикс под включение).
    task, _created = orchestrator.create_or_coalesce_node_bootstrap(
        node, {"pool_id": node.pool_id, "rerun": True}, defer_to_reconciler=False
    )
    db.commit()
    if _created:
        orchestrator.run_task_async(task, node=node)
    # defer_to_reconciler=False → таска создаётся всегда; None-safe на всякий.
    task_id = task.id if task else None
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_bootstrap_rerun",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={"task_id": task_id},
    )
    return {"node_id": node.id, "task_id": task_id}


@router.post("/nodes/{node_id}/diagnose")
def diagnose_node(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Run check_node_health against the node without touching configs."""
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task("node", node.id, "diagnose", {})
    db.commit()
    orchestrator.run_task_async(task, node=node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_diagnose",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={"task_id": task.id},
    )
    return {"node_id": node.id, "task_id": task.id}


@router.post("/nodes/{node_id}/auto-diagnose/disable", status_code=200)
def disable_node_auto_diagnose(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Mute smart-диагностику и Telegram-алерты для этой ноды и ВСЕХ её link'ов.

    Worker `_auto_diagnose_stale_links` skip'ает links у этой ноды.
    `run_relay_link_health_tick` фильтрует `failed_relay_names` против
    muted nodes перед `notify_admins(kind=infra_ssh)` — в Telegram алёрт
    не уходит. Ручная диагностика (`POST /nodes/{id}/diagnose`,
    `POST /exits/links/{id}/diagnose`) остаётся доступной — это только
    выключение АВТОматического trigger'а + алёртов.

    Idempotent: повторный disable не перезаписывает timestamp.
    """
    from ..time_utils import utcnow

    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if node.auto_diagnose_disabled_at is not None:
        return {
            "node_id": node.id,
            "auto_diagnose_disabled_at": node.auto_diagnose_disabled_at,
            "already_disabled": True,
        }
    node.auto_diagnose_disabled_at = utcnow()
    db.commit()
    db.refresh(node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "node_auto_diagnose_disabled", "vpn_node",
        node.id, actor_type=actor_type,
    )
    return {
        "node_id": node.id,
        "auto_diagnose_disabled_at": node.auto_diagnose_disabled_at,
    }


@router.post("/nodes/{node_id}/auto-diagnose/enable", status_code=200)
def enable_node_auto_diagnose(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Снять mute с smart-диагностики и Telegram-алертов для ноды."""
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if node.auto_diagnose_disabled_at is None:
        return {"node_id": node.id, "already_enabled": True}
    node.auto_diagnose_disabled_at = None
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "node_auto_diagnose_enabled", "vpn_node",
        node.id, actor_type=actor_type,
    )
    return {"node_id": node.id, "auto_diagnose_disabled_at": None}


@router.post("/nodes/{node_id}/active", response_model=schemas.VPNNodeOut)
def set_node_active(
    node_id: int,
    body: dict,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Toggle ``is_active``. A node with ``is_active=False`` stays up but
    is excluded from the scheduler in :func:`services.provisioning.choose_node`,
    so new subscriptions won't land on it. Existing subs keep working.
    Useful for staging a freshly-added node for manual testing before it
    starts taking real traffic.

    Side-effect on promote to ``is_active=True``: clears ``cooldown_until``,
    ``suspect_since`` and ``blocked_regions``. Rationale: these are set by
    the health monitor / traffic-drop detector after an incident and block
    ``choose_node`` from picking the node. An operator explicitly flipping
    the kill switch back on is saying "take subs again" — leaving stale
    gates would silently defeat that intent and all new subs would keep
    landing on the one node that isn't in cooldown (prod-hit 2026-04-15,
    3 of 4 nodes were ``is_active=True`` with cooldown still in the future,
    all new subs stacked on the 4th). Mirrors PATCH ``/nodes/{id}/status``.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    if "is_active" not in body:
        raise HTTPException(400, "is_active required")
    next_active = bool(body["is_active"])
    cleared: list[str] = []
    if next_active:
        if node.cooldown_until is not None:
            node.cooldown_until = None
            cleared.append("cooldown_until")
        if node.suspect_since is not None:
            node.suspect_since = None
            cleared.append("suspect_since")
        if node.blocked_regions:
            node.blocked_regions = None
            cleared.append("blocked_regions")
    node.is_active = next_active
    db.add(node)
    db.commit()
    db.refresh(node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "node_set_active",
        "vpn_node", node.id,
        actor_type=actor_type,
        metadata={"is_active": node.is_active, "cleared": cleared},
    )
    db.commit()
    return schemas.VPNNodeOut.from_orm(node)


_ALLOWED_STATUS_OVERRIDES = {"active", "error", "disabled"}


@router.patch("/nodes/{node_id}/status", response_model=schemas.VPNNodeOut)
def set_node_status(
    node_id: int,
    body: dict,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Manually override node status.

    Allowed values: ``active``, ``error``, ``disabled``.  ``registering``
    and ``draining`` are managed automatically and cannot be set by hand.

    Side-effects on promote to ``active``: clears ``cooldown_until``,
    ``suspect_since`` and ``blocked_regions`` so the node immediately
    re-enters the selection pool.  Demote to ``error``/``disabled``
    sets ``is_active=False`` to prevent new subscriptions landing here.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    raw = body.get("status")
    if not raw or raw not in _ALLOWED_STATUS_OVERRIDES:
        raise HTTPException(
            400,
            f"status must be one of {sorted(_ALLOWED_STATUS_OVERRIDES)}",
        )
    try:
        new_status = models.VPNNodeStatus(raw)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid status: {raw}") from exc

    old_status = node.status
    node.status = new_status

    if new_status == models.VPNNodeStatus.active:
        node.cooldown_until = None
        node.suspect_since = None
        node.blocked_regions = None
    elif new_status in (models.VPNNodeStatus.error, models.VPNNodeStatus.disabled):
        node.is_active = False

    db.add(node)
    db.commit()
    db.refresh(node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "node_status_changed",
        "vpn_node", node.id,
        actor_type=actor_type,
        metadata={"old_status": old_status.value, "new_status": new_status.value},
    )
    db.commit()
    return schemas.VPNNodeOut.from_orm(node)


@router.post("/nodes/{node_id}/configs", response_model=schemas.VPNConfigOut)
def create_config(
    node_id: int,
    payload: schemas.VPNConfigCreate,
    defer_bootstrap: bool = Query(
        default=False,
        description=(
            "If true, не создаём bootstrap-task в конце — caller сам "
            "вызовет POST /nodes/{id}/bootstrap после batch'а правок"
        ),
    ),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    try:
        protocol = models.VPNConfigProtocol(payload.protocol)
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Unknown protocol") from exc

    # Only one config per protocol per node — the extra_vars collector
    # and the ansible roles both assume this. Silently returning the
    # existing row (as the idempotent helpers used to do) made the
    # admin form look broken: operators hit "+ Добавить" and nothing
    # changed. Fail loud instead; to replace a config, delete first.
    existing_same_protocol = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == protocol,
        )
        .first()
    )
    if existing_same_protocol is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Config for protocol {protocol.value} already exists on this node "
                f"(id={existing_same_protocol.id}). Delete it first to replace."
            ),
        )

    # VLESS Reality: if the admin left public_key empty we generate the
    # whole keypair + short_id backend-side via the shared helper. The
    # add-config form in the admin UI relies on this behavior (see
    # admin/src/pages/Nodes.tsx comment on AddConfigForm).
    if protocol == models.VPNConfigProtocol.vless_reality and not payload.public_key:
        from ..services.node_spawner import ensure_reality_config
        config = ensure_reality_config(
            db, node,
            port=payload.port or None,
            sni=payload.sni or None,
            dest=payload.fallback or None,
        )
    # ShadowTLS+SS: backend owns both secrets (outer shadow-tls password
    # and inner ss-rust PSK). The admin form only supplies port + SNI;
    # passwords are generated here and stored encrypted.
    elif protocol == models.VPNConfigProtocol.shadowtls_ss and not (
        payload.settings or {}
    ).get("ss_password_enc"):
        from ..services.node_spawner import ensure_shadowtls_config
        config = ensure_shadowtls_config(
            db, node,
            port=payload.port or None,
            handshake_domain=payload.sni or None,
            name=payload.name or None,
        )
    else:
        config = models.VPNConfig(
            node_id=node.id,
            name=payload.name,
            protocol=protocol,
            port=payload.port,
            sni=payload.sni,
            public_key=payload.public_key,
            fallback=payload.fallback,
            settings=payload.settings,
            is_enabled=payload.is_enabled,
        )
        db.add(config)
        db.commit()
        db.refresh(config)
    # WS+CDN: provision the CF-proxied subdomain (sets config.sni) BEFORE
    # backfill so credentials are built against the CDN domain. On CF
    # failure drop the just-created row, else the 409 "already exists"
    # guard blocks the retry and the operator is stuck with a dangling
    # sni-less config.
    try:
        _provision_cf_subdomain(db, node, config)
    except HTTPException:
        db.delete(config)
        db.commit()
        raise
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "config_created", "vpn_config", config.id, actor_type=actor_type)
    # Existing warm bundles were built against the previous protocol set;
    # drop them so the warmer rebuilds with the new config included.
    from ..services import warm_pool
    warm_pool.invalidate_node_warm_pool(db, node.id, reason="config added")
    orchestrator = ProvisioningOrchestrator(db)
    # Backfill credentials for existing devices on this node — без этого
    # юзеры провижененные до новой протоколы получат обновлённый xray
    # config, но stale-credential без нового протокола. Backfill дёргаем
    # ВСЕГДА (даже при defer_bootstrap), т.к. он только пишет в БД и не
    # запускает ansible — реально дотолкает протокол на ноду только
    # bootstrap (через _handle_task_outcome → resync_node_clients).
    orchestrator.backfill_credentials_for_new_config(node, config)
    if not defer_bootstrap:
        task, _created = orchestrator.create_or_coalesce_node_bootstrap(
            node, {"pool_id": node.pool_id, "config_change": True}
        )
        db.commit()
        if _created:
            orchestrator.run_task_async(task, node=node)
    return config


@router.put(
    "/nodes/{node_id}/configs/{config_id}",
    response_model=schemas.VPNConfigOut,
)
def update_config(
    node_id: int,
    config_id: int,
    payload: schemas.VPNConfigUpdate,
    defer_bootstrap: bool = Query(
        default=False,
        description=(
            "If true, не создаём bootstrap-task в конце — caller сам "
            "вызовет POST /nodes/{id}/bootstrap после batch'а правок"
        ),
    ),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Edit an existing VPNConfig in place and re-run bootstrap.

    Protocol change is forbidden — replacing the protocol is what
    delete+create is for, and it has side effects (warm pool, live
    devices) that can't be papered over here. Every other field is
    editable; omitted fields stay as-is.
    """
    config = db.get(models.VPNConfig, config_id)
    if not config or config.node_id != node_id:
        raise HTTPException(status_code=404, detail="Config not found")

    # Guard against accidental protocol swap. The UI sends ``protocol``
    # read-only for clarity; reject the request if someone flipped it.
    if payload.protocol is not None and payload.protocol != config.protocol.value:
        raise HTTPException(
            status_code=400,
            detail=(
                "Protocol change is not supported. Delete the config and "
                "create a new one for the target protocol."
            ),
        )

    # Auto-front configs (ws-cdn, and xhttp in auto mode): sni is the minted
    # DNS-only *.wgse subdomain (= settings.cf_subdomain) and port is pinned
    # to 443. A manual edit would desync from the live A-record / nginx
    # server_name / LE cert and break the chain silently — neutralize those
    # two fields. Direct xhttp (no cf_subdomain) stays editable.
    _cf_fronted = config.protocol == models.VPNConfigProtocol.vless_ws_cdn or (
        config.protocol == models.VPNConfigProtocol.vless_xhttp
        and (config.settings or {}).get("cf_subdomain")
    )
    if _cf_fronted:
        payload.sni = None
        payload.port = None

    changed: list[str] = []

    # xhttp: явный пустой sni ("") = «переведи в auto-front (DNS-only)» —
    # чистим stale direct-домен/серт и минтим wgse.info-сабдомен (как при create с пустым
    # sni). null = поле не трогали (PATCH-семантика); ТОЛЬКО "" — явный сброс
    # из формы. Для прочих протоколов "" трактуем как «не трогать», чтобы
    # случайно не обнулить, например, reality-SNI.
    if payload.sni == "":
        if (
            config.protocol == models.VPNConfigProtocol.vless_xhttp
            and not (config.settings or {}).get("cf_subdomain")
        ):
            _s = dict(config.settings or {})
            for _k in ("domain", "cert_path", "key_path"):
                _s.pop(_k, None)
            config.sni = None
            config.settings = _s
            db.flush()
            _provision_cf_subdomain(db, config.node, config)  # sni ← DNS-only *.wgse subdomain (node serves LE)
            changed.append("sni")
        payload.sni = None  # обработали (или игнор для non-xhttp) — не применять ниже

    if payload.name is not None and payload.name != config.name:
        config.name = payload.name
        changed.append("name")
    if payload.port is not None and payload.port != config.port:
        config.port = payload.port
        changed.append("port")
    if payload.sni is not None and payload.sni != config.sni:
        config.sni = payload.sni
        changed.append("sni")
    if payload.public_key is not None and payload.public_key != config.public_key:
        config.public_key = payload.public_key
        changed.append("public_key")
    if payload.fallback is not None and payload.fallback != config.fallback:
        config.fallback = payload.fallback
        changed.append("fallback")
    if payload.is_enabled is not None and payload.is_enabled != config.is_enabled:
        config.is_enabled = payload.is_enabled
        changed.append("is_enabled")
    if payload.settings is not None:
        # Shallow-merge so the UI can update one sub-key without having
        # to resend the encrypted secrets it never saw. Passing ``{}``
        # explicitly is a no-op (nothing to merge).
        merged = dict(config.settings or {})
        merged.update(payload.settings)
        if merged != (config.settings or {}):
            config.settings = merged
            changed.append("settings")

    if not changed:
        # Nothing actually changed — skip bootstrap + warm invalidation.
        # Idempotent from the UI perspective.
        return config

    db.commit()
    db.refresh(config)

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "config_updated",
        "vpn_config",
        config.id,
        actor_type=actor_type,
        metadata={"changed_fields": changed},
    )
    # Existing warm bundles were built with the previous config values —
    # port/sni/pubkey baked into the URI. Drop them so the warmer
    # rebuilds with the updated config.
    from ..services import warm_pool
    warm_pool.invalidate_node_warm_pool(db, node_id, reason="config updated")
    if not defer_bootstrap:
        node = db.get(models.VPNNode, node_id)
        if node:
            orchestrator = ProvisioningOrchestrator(db)
            task, _created = orchestrator.create_or_coalesce_node_bootstrap(
                node, {"pool_id": node.pool_id, "config_change": True}
            )
            db.commit()
            if _created:
                orchestrator.run_task_async(task, node=node)
    return config


@router.delete("/nodes/{node_id}/configs/{config_id}", status_code=204)
def delete_config(
    node_id: int,
    config_id: int,
    defer_bootstrap: bool = Query(
        default=False,
        description=(
            "If true, не создаём bootstrap-task в конце — caller сам "
            "вызовет POST /nodes/{id}/bootstrap после batch'а правок"
        ),
    ),
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Delete a VPN protocol config from a node.

    Refuses if any Device still points at this config — those devices
    would instantly break, and the admin should rotate them to another
    node first. To force-delete, disable the config instead (clients
    stop getting fresh URIs but existing ones keep working until the
    node itself is reprovisioned).
    """
    config = db.get(models.VPNConfig, config_id)
    if not config or config.node_id != node_id:
        raise HTTPException(status_code=404, detail="Config not found")
    # Only *live* devices block the delete. A revoked/disabled device
    # still has ``config_id`` set (we never null it out on unbind) but
    # it's terminal — the user is already off the node, so keeping the
    # VPNConfig around for its sake is pointless.
    device_count = (
        db.query(models.Device)
        .filter(
            models.Device.config_id == config.id,
            models.Device.status.notin_(
                [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
            ),
        )
        .count()
    )
    if device_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{device_count} device(s) still bound to this config. "
                "Rotate them to another node first, or disable the config."
            ),
        )
    # Null out config_id on terminal devices + any Credential rows still
    # pointing here. The 409 gate above already guarantees that every
    # remaining Device is revoked/disabled, so losing the config link is
    # fine — the rows stay for sub_token aliasing (see the comment block
    # on the ``revoke`` branch in services/provisioning.py). Pre-migration
    # 0030 this had to hard-delete the Device rows because config_id was
    # NOT NULL; now the column is nullable + ON DELETE SET NULL so we
    # can just detach.
    db.query(models.Device).filter(
        models.Device.config_id == config.id
    ).update({models.Device.config_id: None}, synchronize_session=False)
    db.query(models.Credential).filter(
        models.Credential.config_id == config.id
    ).update({models.Credential.config_id: None}, synchronize_session=False)
    db.flush()

    # Tear down the CF DNS record for ws-cdn configs before dropping the row.
    _teardown_cf_subdomain(config)
    db.delete(config)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "config_deleted", "vpn_config", config_id, actor_type=actor_type)
    from ..services import warm_pool
    warm_pool.invalidate_node_warm_pool(db, node_id, reason="config removed")
    if not defer_bootstrap:
        node = db.get(models.VPNNode, node_id)
        if node:
            orchestrator = ProvisioningOrchestrator(db)
            task, _created = orchestrator.create_or_coalesce_node_bootstrap(
                node, {"pool_id": node.pool_id, "config_change": True}
            )
            db.commit()
            if _created:
                orchestrator.run_task_async(task, node=node)
    return None


@router.get("/nodes/{node_id}/configs", response_model=list[schemas.VPNConfigOut])
def list_configs(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    configs = db.query(models.VPNConfig).filter(models.VPNConfig.node_id == node_id).all()
    return [schemas.VPNConfigOut.from_orm(cfg) for cfg in configs]


@router.get("/nodes/{node_id}/links", response_model=list[schemas.NodeRelayLinkOut])
def list_node_relay_links(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Per-exit link view for a relay node (G.7 admin multi-attach).

    Returns one row per ``RelayExitLink`` where ``relay_node_id ==
    node_id`` with the interface name, the attached exit and a live
    counter of credentials pinned to this (relay, exit) pair. Non-relay
    nodes legitimately return ``[]`` (no links). Counter excludes
    revoked creds but includes warm-pool entries (those still carry
    ``exit_id`` and occupy a routing slot on the relay).
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    links = (
        db.query(models.RelayExitLink)
        .filter(models.RelayExitLink.relay_node_id == node_id)
        .order_by(models.RelayExitLink.wg_interface_name)
        .all()
    )
    if not links:
        return []

    counts: dict[int, int] = dict(
        db.query(
            models.Credential.exit_id,
            func.count(models.Credential.id),
        )
        .filter(
            models.Credential.node_id == node_id,
            models.Credential.exit_id.isnot(None),
            models.Credential.pool_state != models.CredentialPoolState.revoked,
        )
        .group_by(models.Credential.exit_id)
        .all()
    )

    # ── Auto-diagnose badge data ────────────────────────────────────
    # Latest symptom_detected audit entry per link_id. Subquery: ROW_NUMBER()
    # would be cleaner на PG, но per-link массив маленький (макс ~5-10
    # линков на одну ноду), поэтому one query + python-group дешевле и
    # переноcимее. extra→symptom + extra→action_taken вытаскиваем JSON-ом.
    link_ids = [link.id for link in links]
    latest_audit_by_link: dict[int, models.AuditLog] = {}
    if link_ids:
        audit_rows = (
            db.query(models.AuditLog)
            .filter(models.AuditLog.target_type == "relay_exit_link")
            .filter(models.AuditLog.target_id.in_(link_ids))
            .filter(models.AuditLog.action == "symptom_detected")
            .order_by(models.AuditLog.created_at.desc())
            .all()
        )
        for row in audit_rows:
            if row.target_id not in latest_audit_by_link:
                latest_audit_by_link[row.target_id] = row

    def _audit_to_diag(
        link_id: int,
    ) -> tuple[datetime | None, int | None, str | None]:
        row = latest_audit_by_link.get(link_id)
        if row is None:
            return (None, None, None)
        extra = row.extra or {}
        action_taken = str(extra.get("action_taken") or "")
        # action_taken формата "enqueued_task:<id>" — извлекаем числовой id.
        task_id: int | None = None
        if action_taken.startswith("enqueued_task:"):
            try:
                task_id = int(action_taken.split(":", 1)[1])
            except ValueError:
                task_id = None
        symptom = extra.get("symptom")
        return (row.created_at, task_id, str(symptom) if symptom else None)

    out: list[schemas.NodeRelayLinkOut] = []
    for link in links:
        diag_at, diag_task_id, diag_symptom = _audit_to_diag(link.id)
        out.append(
            schemas.NodeRelayLinkOut(
                link_id=link.id,
                exit_id=link.exit_id,
                exit_name=link.exit_node.name if link.exit_node else "",
                wg_interface_name=link.wg_interface_name,
                wg_client_address_v4=link.wg_client_address_v4,
                wg_client_public_key=link.wg_client_public_key,
                credentials_count=counts.get(link.exit_id, 0),
                created_at=link.created_at,
                last_auto_diagnose_at=diag_at,
                last_auto_diagnose_task_id=diag_task_id,
                last_auto_diagnose_symptom=diag_symptom,
            )
        )
    return out


# Freshness window for "who's on the node right now" — matches the
# default TRAFFIC_STATS_INTERVAL (5 min) plus two skipped ticks. Older
# than this means the collector hasn't written a sample recently, so
# the list is showing a historical snapshot — admin UI flips to a
# "нет свежих данных" banner.
_NODE_USERS_FRESHNESS = timedelta(minutes=15)


@router.get("/nodes/{node_id}/users", response_model=schemas.NodeActiveUsersOut)
def list_node_users(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Return the access_usernames seen on the node in the last traffic-stats tick.

    Reads the freshest ``NodeTrafficSample`` for the node, unpacks
    per-protocol user lists from ``details``, and left-joins each
    username to Device → Subscription → User/Plan so the admin UI can
    show who is currently on the node without a second round-trip.

    Tolerates legacy samples where ``details["<proto>"]["users"]`` is
    a bare int (pre-2026-04 format) — those rows just don't contribute
    usernames, count is unreliable anyway.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    sample = (
        db.query(models.NodeTrafficSample)
        .filter(models.NodeTrafficSample.node_id == node_id)
        .order_by(models.NodeTrafficSample.observed_at.desc())
        .first()
    )

    observed_at: datetime | None = None
    stale = True
    username_protos: dict[str, set[str]] = {}

    if sample is not None:
        observed_at = sample.observed_at
        # observed_at is stored as naive UTC (default=utcnow) — compare
        # against a naive now() so we don't accidentally trip over tz.
        age = datetime.utcnow() - (
            observed_at.replace(tzinfo=None) if observed_at.tzinfo else observed_at
        )
        stale = age > _NODE_USERS_FRESHNESS
        details = sample.details or {}
        for proto, payload in details.items():
            if proto == "_errors" or not isinstance(payload, dict):
                continue
            users_raw = payload.get("users")
            if not isinstance(users_raw, list):
                # Legacy int-count format; nothing we can do.
                continue
            for uname in users_raw:
                if not isinstance(uname, str) or not uname:
                    continue
                username_protos.setdefault(uname, set()).add(proto)

    if not username_protos:
        return schemas.NodeActiveUsersOut(
            node_id=node_id,
            observed_at=observed_at,
            stale=stale,
            users=[],
        )

    # Резолвим юзера через CREDENTIAL.access_username (а НЕ Device) — xray на ноде
    # видит access_username КРЕДОВ, а у диверс/warm-кредов он "warm-<node>-<hash>"
    # и НЕ совпадает с Device.access_username → раньше такие показывались как
    # «orphan» с голым warm-именем без телеги. Cred несёт device_id → дотягиваем
    # Device→Sub→User. Фильтр node_id — креды ЭТОЙ ноды ИЛИ легаси/детачнутые с
    # node_id=NULL (детач при удалении соседней ноды обнуляет node_id, но
    # access_username warm-бандла уникален по ноде → ложных совпадений нет).
    # ORDER BY ниже ставит node-specific строку первой, чтобы при дубле она и
    # выиграла дедуп.
    rows = (
        db.query(
            models.Device.id.label("device_id"),
            models.Credential.access_username,
            models.Device.name.label("device_name"),
            models.Subscription.id.label("subscription_id"),
            models.Subscription.expires_at,
            models.User.id.label("user_id"),
            models.User.telegram_id,
            models.Plan.id.label("plan_id"),
            models.Plan.name.label("plan_name"),
        )
        .select_from(models.Credential)
        .outerjoin(models.Device, models.Device.id == models.Credential.device_id)
        .outerjoin(
            models.Subscription,
            models.Subscription.id == models.Device.subscription_id,
        )
        .outerjoin(models.User, models.User.id == models.Device.user_id)
        .outerjoin(models.Plan, models.Plan.id == models.Subscription.plan_id)
        .filter(models.Credential.access_username.in_(list(username_protos.keys())))
        .filter(
            or_(
                models.Credential.node_id == node_id,
                models.Credential.node_id.is_(None),
            )
        )
        # node-specific (node_id IS NULL → False/0) сортируется раньше легаси.
        .order_by(models.Credential.node_id.is_(None))
        .all()
    )

    by_username: dict[str, dict] = {}
    for r in rows:
        if r.access_username in by_username:
            # Один access_username может встретиться дважды (node-specific +
            # легаси/детачнутый кред с node_id=NULL). ORDER BY выше гарантирует,
            # что node-specific строка пришла первой и уже в словаре — она и
            # выигрывает (UI держит один слот на username).
            continue
        by_username[r.access_username] = {
            "device_id": r.device_id,
            "device_name": r.device_name,
            "subscription_id": r.subscription_id,
            "user_id": r.user_id,
            "user_telegram_id": r.telegram_id,
            "plan_id": r.plan_id,
            "plan_name": r.plan_name,
            "subscription_expires_at": r.expires_at,
        }

    out_users: list[schemas.NodeActiveUserOut] = []
    for uname, protos in sorted(username_protos.items()):
        meta = by_username.get(uname, {})
        out_users.append(
            schemas.NodeActiveUserOut(
                access_username=uname,
                device_id=meta.get("device_id"),
                device_name=meta.get("device_name"),
                subscription_id=meta.get("subscription_id"),
                user_id=meta.get("user_id"),
                user_telegram_id=meta.get("user_telegram_id"),
                plan_id=meta.get("plan_id"),
                plan_name=meta.get("plan_name"),
                protocols=sorted(protos),
                subscription_expires_at=meta.get("subscription_expires_at"),
            )
        )

    return schemas.NodeActiveUsersOut(
        node_id=node_id,
        observed_at=observed_at,
        stale=stale,
        users=out_users,
    )


@router.get(
    "/nodes/{node_id}/traffic-history",
    response_model=schemas.NodeTrafficHistoryOut,
)
def get_node_traffic_history(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    hours: int = Query(default=24, ge=1, le=168),
):
    """Return NodeTrafficSample series for the last ``hours`` hours.

    Hard-bounded to [1, 168] so a misclick in the UI can't drag the
    whole month of samples (~8600 rows per node). Default 24h lines up
    with the admin sparkline; operators who want a longer window pass
    ``?hours=72`` etc.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    to_ts = datetime.utcnow()
    from_ts = to_ts - timedelta(hours=hours)
    samples = (
        db.query(models.NodeTrafficSample)
        .filter(
            models.NodeTrafficSample.node_id == node_id,
            models.NodeTrafficSample.observed_at >= from_ts,
        )
        .order_by(models.NodeTrafficSample.observed_at.asc())
        .all()
    )

    return schemas.NodeTrafficHistoryOut(
        node_id=node_id,
        from_ts=from_ts,
        to_ts=to_ts,
        samples=[
            schemas.NodeTrafficSamplePoint(
                observed_at=s.observed_at,
                active_users=s.active_users,
                uplink_bytes=s.uplink_bytes,
                downlink_bytes=s.downlink_bytes,
            )
            for s in samples
        ],
    )


# ---------------------------------------------------------------------------
# Cloud-spawned nodes
# ---------------------------------------------------------------------------


@router.post("/nodes/spawn", response_model=schemas.VPNNodeOut)
def spawn_node_route(
    payload: schemas.NodeSpawnRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    # #55 — same DNS-safe whitelist as /nodes POST. Only ``name`` can be
    # checked at this point; the host is resolved later by the cloud driver
    # and re-validated inside ``_finalize_spawn`` before the real host is
    # written onto the row.
    try:
        validate_node_name(payload.name)
    except InvalidNodeIdentity as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Неблокирующий спавн: синхронно делаем только быстрый заказ (order_server)
    # + фиксируем VPNNode-строку, долгий поллинг IP + bootstrap — в фоне. Иначе
    # 600s-поллинг create_server упирался в nginx proxy_read_timeout (60s) → CF
    # 502, а оплаченный VPS оставался осиротевшим. См. spawn_node_async.
    try:
        node = spawn_node_async(
            db,
            provider_id=payload.provider_id,
            name=payload.name,
            region=payload.region,
            plan=payload.plan,
            image=payload.image,
            ssh_key_ids=payload.ssh_key_ids,
            pool_id=payload.pool_id,
            user_data=payload.user_data,
            notes=payload.notes,
        )
    # 400, не 502: заказ падает на этапе обращения к провайдеру (неверный
    # tarif/ostempl, нет баланса, верификация). Cloudflare подменяет 5xx своей
    # HTML-страницей и прячет detail — на 4xx он проходит, и админ видит
    # реальную причину от 4vps в форме. Реальные gateway-сбои тут не при чём.
    except NodeSpawnError as exc:
        raise HTTPException(status_code=400, detail=f"spawn failed: {exc}") from exc

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_spawned",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={
            "provider_id": payload.provider_id,
            "region": payload.region,
            "plan": payload.plan,
        },
    )
    return schemas.VPNNodeOut.from_orm(node)


@router.post("/nodes/{node_id}/destroy")
def destroy_node_route(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        destroy_node(db, node)
    except NodeSpawnError as exc:
        raise HTTPException(status_code=502, detail=f"destroy failed: {exc}") from exc
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "node_destroyed", "vpn_node", node.id, actor_type=actor_type)
    return {"node_id": node.id, "status": node.status.value}


@router.post("/nodes/{node_id}/reinstall", response_model=schemas.VPNNodeOut)
def reinstall_node_route(
    node_id: int,
    payload: schemas.NodeReinstallRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Переустановить ОС на cloud-ноде через API провайдера и заново прокатить
    site.yml. IP сохраняется → reality-ключи и sub-токены остаются валидными."""
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        node, _task = reinstall_node(db, node, image=payload.image)
    except NodeSpawnError as exc:
        raise HTTPException(status_code=502, detail=f"reinstall failed: {exc}") from exc
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_reinstalled",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={"image": payload.image},
    )
    return schemas.VPNNodeOut.from_orm(node)


@router.post("/nodes/{node_id}/renew")
def renew_node_route(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Принудительно продлить аренду cloud-ноды у провайдера (списывает с
    баланса). Обычно продление автоматическое (autoprolong, включается при
    заказе) — это ручной путь на случай выключенного autoprolong."""
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        renew_node(db, node)
    except NodeSpawnError as exc:
        raise HTTPException(status_code=502, detail=f"renew failed: {exc}") from exc
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "node_renewed", "vpn_node", node.id, actor_type=actor_type)
    return {"node_id": node.id, "renewed": True}


@router.get("/pools", response_model=list[schemas.ServerPoolMini])
def list_server_pools(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Список пулов (id+name) — для дропдауна правки ноды."""
    return [
        schemas.ServerPoolMini(id=p.id, name=p.name)
        for p in db.query(models.ServerPool).order_by(models.ServerPool.name).all()
    ]


@router.patch("/nodes/{node_id}", response_model=schemas.VPNNodeOut)
def update_node(
    node_id: int,
    payload: schemas.VPNNodeUpdate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Править дисплейные/маршрутные поля ноды: name / region / pool_id / notes.

    ``name`` валидируется как inventory-хост (``[a-z0-9][a-z0-9-]{0,62}``) и
    проверяется на уникальность. Переименование БЕЗОПАСНО без re-bootstrap:
    ``build_inventory_for_node`` рендерит name как alias, а ansible коннектится
    по ``ansible_host=host`` — IP не меняется, развёрнутые клиенты не рвутся.
    ``region``/``pool_id`` влияют только на будущий ``choose_node``; ``notes`` —
    текст. ``host``/``ssh_port`` тут нельзя (identity у провайдера →
    reinstall/renew). ``model_fields_set`` различает «не передано» и «=null».
    """
    from ..time_utils import utcnow

    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    fields = payload.model_fields_set
    changed: list[str] = []

    if "name" in fields and payload.name != node.name:
        new_name = (payload.name or "").strip()
        try:
            validate_node_name(new_name)
        except InvalidNodeIdentity as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        clash = (
            db.query(models.VPNNode)
            .filter(models.VPNNode.name == new_name, models.VPNNode.id != node.id)
            .first()
        )
        if clash:
            raise HTTPException(
                status_code=409,
                detail=f"Нода с именем '{new_name}' уже есть (#{clash.id})",
            )
        node.name = new_name
        changed.append("name")

    if "region" in fields and payload.region != node.region:
        new_region = (payload.region or "").strip()
        if not new_region:
            raise HTTPException(status_code=400, detail="region не может быть пустым")
        node.region = new_region
        changed.append("region")

    if "pool_id" in fields and payload.pool_id != node.pool_id:
        if payload.pool_id is not None and not db.get(
            models.ServerPool, payload.pool_id
        ):
            raise HTTPException(
                status_code=400, detail=f"Пул #{payload.pool_id} не найден"
            )
        node.pool_id = payload.pool_id
        changed.append("pool_id")

    if "notes" in fields and payload.notes != node.notes:
        node.notes = payload.notes
        changed.append("notes")

    if changed:
        node.updated_at = utcnow()
        db.add(node)
        actor, actor_type = _resolve_admin_actor(admin_actor)
        # _audit коммитит сам → флашит и правки ноды в той же транзакции.
        _audit(
            db, actor, "node_updated", "vpn_node", node.id,
            actor_type=actor_type, metadata={"changed_fields": changed},
        )
        db.refresh(node)
    return schemas.VPNNodeOut.from_orm(node)


@router.delete("/nodes/{node_id}", status_code=200)
def delete_node(
    node_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Удалить ЗАПИСЬ ноды из админ-панели (БД). НЕ трогает хостер —
    уничтожение VPS у провайдера это отдельное действие
    (``POST /nodes/{id}/destroy``: запускает teardown-плейбук и снимает VPS).

    Гейты (оба обходятся ``?force=true``, фронт это знает):

    * ``active_subs`` (409) — на ноде ещё есть АКТИВНЫЕ подписки (node_id ==
      этой ноды). Фронт предлагает «переселить и удалить»: ``/migrate`` →
      retry. Замороженные подписки НЕ блокируют: их node_id обнуляется через
      SET NULL, а при разморозке подписка заново выбирает ноду (``choose_node``);
      живых кредов на этой ноде у них нет, стрэнда не будет. (Раньше frozen тоже
      считались — но ``migrate_subscriptions_off`` их не двигает, и нода
      становилась неудаляемой: dead-end.)
    * ``live_vm`` (409) — у ноды живой VPS у хостера (provider_external_id +
      статус не disabled/error). Удалять запись = осиротить платный сервер;
      сначала «уничтожить у хостера» (/destroy) или force.

    Warm-кред'ы, привязанные к ноде, удаляются (без ноды они неназначаемы).
    Кред'ы привязанных подписок ДЕТАЧАТСЯ (node_id → NULL, is_active → False):
    исторический sub/cred-линк выживает, но мёртвая нода уходит из sub-link'а
    (per-device alias пересаживается на живого соседа — diverse-саба продолжает
    отдавать оставшиеся ноды).
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    if not force:
        active_subs = (
            db.query(models.Subscription)
            .filter(
                models.Subscription.node_id == node.id,
                models.Subscription.status == models.SubscriptionStatus.active,
            )
            .count()
        )
        if active_subs > 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "active_subs",
                    "active_subs": active_subs,
                    "message": (
                        f"На ноде ещё {active_subs} активных подписок — "
                        "сперва перенеси их на другую ноду."
                    ),
                },
            )
        if node.provider_external_id and node.status not in (
            models.VPNNodeStatus.disabled,
            models.VPNNodeStatus.error,
        ):
            # Живой VPS у хостера → удаление записи осиротит платный сервер.
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "live_vm",
                    "message": (
                        "У ноды живой VPS у хостера — сначала «уничтожить у "
                        "хостера» (/destroy), либо force."
                    ),
                },
            )

    # Warm-pool credentials (no subscription) can't survive a missing
    # node — they'd never be assignable. Delete them.
    warm_deleted = (
        db.query(models.Credential)
        .filter(
            models.Credential.node_id == node.id,
            models.Credential.subscription_id.is_(None),
        )
        .delete(synchronize_session=False)
    )

    # Bound credentials (any sub: terminated/expired *or* a live diverse sub
    # losing one of its N nodes): detach (NULL node_id) so the FK stops
    # pinning the node, AND deactivate (is_active → False) so the dead node
    # drops out of the served sub-link. The sub-link's per-device alias then
    # re-points the now-credless device at a live sibling (api_extensions.py
    # source_device branch) — a diverse sub keeps serving its other nodes.
    bound_detached = (
        db.query(models.Credential)
        .filter(
            models.Credential.node_id == node.id,
            models.Credential.subscription_id.isnot(None),
        )
        .update(
            {models.Credential.node_id: None, models.Credential.is_active: False},
            synchronize_session=False,
        )
    )

    # Devices point at configs on this node via config_id. The DB-level
    # ON DELETE SET NULL (migration 0030) would handle it during the
    # vpn_nodes → vpn_configs → devices cascade, but doing it explicitly
    # here pre-empties SQLAlchemy's orphan-nullify at flush time (which
    # would otherwise race with the CASCADE) and gives the admin an
    # accurate ``devices_detached`` count in the audit row. Those rows
    # live on for sub_token aliasing (see revoke-branch comment in
    # services/provisioning.py); only their config link is dropped.
    devices_detached = (
        db.query(models.Device)
        .filter(
            models.Device.config_id.in_(
                db.query(models.VPNConfig.id).filter(
                    models.VPNConfig.node_id == node.id
                )
            ),
        )
        .update({models.Device.config_id: None}, synchronize_session=False)
    )

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_deleted",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={
            "warm_credentials_deleted": warm_deleted,
            "bound_credentials_detached": bound_detached,
            "devices_detached": devices_detached,
        },
    )
    # WS+CDN: release each ws-cdn config's CF DNS record before the ORM
    # cascade drops the rows — else the proxied A-record (origin IP in the
    # public CF zone) leaks forever (resource leak + deanon of a burned IP).
    for _cfg in list(node.configs):
        _teardown_cf_subdomain(_cfg)
    db.delete(node)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Something still points here — surface the DB-level detail so
        # the admin can fix it instead of staring at "Не удалось удалить".
        raise HTTPException(
            status_code=409,
            detail={
                "error": "fk_blocked",
                "message": (
                    "БД отказала в удалении: на ноду ещё что-то ссылается. "
                    f"Detail: {exc.orig}"
                ),
            },
        ) from exc
    return {
        "node_id": node_id,
        "deleted": True,
        "warm_credentials_deleted": warm_deleted,
        "bound_credentials_detached": bound_detached,
        "devices_detached": devices_detached,
    }


# ---------------------------------------------------------------------------
# Health + migration
# ---------------------------------------------------------------------------


@router.get("/nodes/{node_id}/health", response_model=schemas.NodeHealthOut)
def get_node_health(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    summary = recompute_node_health(db, node, auto_migrate=False)
    return schemas.NodeHealthOut(**summary)


@router.post("/nodes/{node_id}/migrate")
def migrate_node_route(
    node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Force migration of all active subscriptions off this node."""
    from ..services.health import migrate_subscriptions_off

    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    result = migrate_subscriptions_off(db, node, reason="manual migration")
    migrated = result["subscription_ids"]
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_migrated",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={
            "migrated_count": len(migrated),
            "considered_count": result["considered_count"],
            "no_target_count": result["no_target_count"],
            "device_task_ids": result["device_task_ids"],
            "resync_task_ids": result["resync_task_ids"],
        },
    )
    # Combined task id list — admin UI polls /api/provisioning/tasks
    # and filters by these to render a grouped progress banner.
    # Revokes go first (old node, best-effort), then device applies
    # (new node), then resync mops up any drift.
    task_ids = (
        result["revoke_task_ids"]
        + result["device_task_ids"]
        + result["resync_task_ids"]
    )
    return {
        "node_id": node.id,
        "migrated_subscriptions": migrated,
        "task_ids": task_ids,
        "revoke_task_ids": result["revoke_task_ids"],
        "device_task_ids": result["device_task_ids"],
        "resync_task_ids": result["resync_task_ids"],
        # Lets the UI tell "0 active subs on this node" apart from
        # "had subs but every other node was in cooldown / unhealthy".
        "considered_count": result["considered_count"],
        "no_target_count": result["no_target_count"],
    }


@router.post(
    "/nodes/{from_id}/migrate-to/{to_id}",
    response_model=schemas.NodeBulkMigrateOut,
)
def migrate_node_to_target_route(
    from_id: int,
    to_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Move every active subscription from ``from_id`` to a specific ``to_id``.

    Bulk counterpart to ``POST /subscriptions/{id}/migrate`` — walks active
    subs on the source node and funnels them into one explicit target via
    ``migrate_subscription_to_new_node(target_node_id=to_id)``. Unlike
    ``POST /nodes/{id}/migrate`` which auto-picks targets per-sub via
    ``choose_node``, this endpoint pins all subs to a single chosen node
    (needed for the relay cut-over in ``RELAY_ROADMAP.md`` D.3: the admin
    points an old foreign direct-node's users at a freshly built RU relay).

    ``sub_token`` is preserved per sub. Revokes on the old node and applies
    on the new node run in background. One ``resync_node_clients`` is
    issued on the target at the end to repair any drift.

    Failures are per-sub: a sub that can't migrate (e.g. target not active)
    is reported in ``failed`` and the loop continues with the next sub.
    """
    if from_id == to_id:
        raise HTTPException(
            status_code=400, detail="from_id and to_id must differ"
        )
    from_node = db.get(models.VPNNode, from_id)
    if not from_node:
        raise HTTPException(status_code=404, detail="Source node not found")
    to_node = db.get(models.VPNNode, to_id)
    if not to_node:
        raise HTTPException(status_code=404, detail="Target node not found")
    if not to_node.is_active:
        raise HTTPException(
            status_code=400, detail="Target node is not active"
        )

    subs: list[models.Subscription] = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.node_id == from_node.id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .all()
    )
    considered = len(subs)
    migrated_ids: list[int] = []
    failed: list[schemas.NodeBulkMigrateFailure] = []
    device_task_ids: list[int] = []
    orchestrator = ProvisioningOrchestrator(db)

    for sub in subs:
        try:
            _target, _device, task = orchestrator.migrate_subscription_to_new_node(
                sub, target_node_id=to_node.id
            )
        except RuntimeError as exc:
            failed.append(
                schemas.NodeBulkMigrateFailure(
                    subscription_id=sub.id, error=str(exc)
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001
            failed.append(
                schemas.NodeBulkMigrateFailure(
                    subscription_id=sub.id, error=f"internal: {exc}"
                )
            )
            continue
        migrated_ids.append(sub.id)
        if task is not None:
            device_task_ids.append(task.id)

    # One resync on the target after the batch — same pattern as
    # migrate_subscriptions_off. The per-sub apply adds each user, but
    # if the target config is mid-drift (race with warm-pool, partial
    # render) the resync re-pushes the full authoritative client list.
    # Cheap (one extra ansible run), and catches the "invalid request
    # user id" class of post-migrate bugs.
    from ..services.provisioning import _node_has_vless_family

    resync_task_ids: list[int] = []
    if migrated_ids and _node_has_vless_family(to_node):
        try:
            resync_task = orchestrator.resync_node_clients(to_node)
            if resync_task is not None:
                resync_task_ids.append(resync_task.id)
        except Exception:  # noqa: BLE001
            # Logged by resync_node_clients; don't fail the whole batch
            # — migrated subs are already on the new node.
            pass

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_bulk_migrated",
        "vpn_node",
        from_node.id,
        actor_type=actor_type,
        metadata={
            "from_node_id": from_node.id,
            "from_node_name": from_node.name,
            "to_node_id": to_node.id,
            "to_node_name": to_node.name,
            "considered_count": considered,
            "migrated_count": len(migrated_ids),
            "failed_count": len(failed),
            "migrated_subscription_ids": migrated_ids,
            "failed_subscription_ids": [f.subscription_id for f in failed],
            "device_task_ids": device_task_ids,
            "resync_task_ids": resync_task_ids,
        },
    )
    db.commit()

    task_ids = device_task_ids + resync_task_ids
    return schemas.NodeBulkMigrateOut(
        from_node_id=from_node.id,
        to_node_id=to_node.id,
        considered_count=considered,
        migrated=migrated_ids,
        failed=failed,
        task_ids=task_ids,
        revoke_task_ids=[],
        device_task_ids=device_task_ids,
        resync_task_ids=resync_task_ids,
    )


@router.post(
    "/nodes/{node_id}/refresh-reality-dest",
    response_model=schemas.NodeRefreshDestOut,
)
def refresh_reality_dest(
    node_id: int,
    payload: schemas.NodeRefreshDestIn,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Сменить Reality SNI/dest для ноды и пере-провижинить её активных
    Device'ов под новый SNI.

    ``payload.sni`` — явный домен (должен быть реальный TLS 1.3 host, НЕ
    заблокированный в target market). ``None`` → ``pick_reality_sni``
    выберет наименее используемый домен из ``REALITY_DEST_POOL``.

    Flow:
        1. Обновляем ``VPNConfig.sni`` + ``fallback`` + ``settings.dest``
           в БД (source of truth для ansible extra_vars и для URI
           в ``_build_vless_reality_credential``).
        2. Для каждой активной Subscription на ноде: revoke_device
           по каждому Device + reprovision_subscription → свежий Device
           с новым UUID и cred_text, отражающим новый SNI. Sub-status
           остаётся active (revoke_device не трогает sub.status).
        3. Ansible apply внутри reprovision перерендерит xray config
           с новым ``vless_reality_sni``/``vless_reality_dest``.

    Клиентский flow: Hiddify/v2rayN пуллят /sub/{token} → получают
    новые URI с новым SNI → handshake идёт с новым fallback cert'ом.
    Старый URI → handshake reject на xray → клиент пуллит sub-link
    раньше (force refresh), и восстанавливается.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from ..services.node_spawner import REALITY_DEST_POOL, pick_reality_sni

    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    cfg = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node_id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality,
        )
        .one_or_none()
    )
    if cfg is None:
        raise HTTPException(
            status_code=400,
            detail="Node has no vless_reality config to refresh",
        )

    old_sni = cfg.sni or ""
    new_sni = payload.sni or pick_reality_sni(db)
    # Допускаем и sni вне пула — callsite может захотеть форсить
    # конкретный fallback для ноды (edge-case, whitelist RKN). Но
    # warn'им если domen явно подозрительный — в MVP только проверяем
    # что не пустой.
    if not new_sni:
        raise HTTPException(status_code=400, detail="Resolved SNI is empty")
    if new_sni == old_sni:
        raise HTTPException(
            status_code=400,
            detail=f"SNI already equals {new_sni}; noop",
        )

    new_dest = f"{new_sni}:443"
    cfg.sni = new_sni
    cfg.fallback = new_dest
    cfg.settings = {**(cfg.settings or {}), "dest": new_dest}
    flag_modified(cfg, "settings")
    db.commit()
    db.refresh(cfg)

    # Warm-pool bundles уже содержат ``cred.config_text`` с ОЛД sni
    # (они построены через ``_build_vless_reality_credential`` в
    # момент warm_pool refill'а). Если не выбросить — первый юзер на
    # ноде получит bundle с старым URI и клиент будет handshake'ить
    # с прежним fallback cert'ом. Invalidate чистит flag'и в БД +
    # next apply tick с state=absent уберёт их c ноды.
    from ..services.warm_pool import invalidate_node_warm_pool

    invalidate_node_warm_pool(db, node_id, reason="reality-dest refresh")

    orchestrator = ProvisioningOrchestrator(db)

    # Xray на ноде всё ещё держит старый sni в realitySettings.serverNames
    # и старый dest в fallback cert'е — без re-render'а config.json клиент
    # с новым URI получит handshake reject. Триггерим bootstrap ПЕРЕД
    # reprovision'ом девайсов, install_vless_reality рендерит новый
    # config.json с обновлённым sni/dest (extra_vars подтянет их из
    # cfg.sni/cfg.settings.dest). Device-apply таски встанут в очередь
    # ПОСЛЕ bootstrap'а ноды — xray к тому моменту уже рестартнёт с
    # новым конфигом, новые UUID'ы добавятся штатно через API.
    # Ordering-critical: bootstrap ОБЯЗАН пройти ПЕРЕД device-apply тасками
    # (xray должен рестартнуть с новым sni/dest до добавления UUID'ов). Дефёр
    # в reconciler сломал бы порядок (device-apply ушли бы раньше реконсайл-
    # тика) → defer_to_reconciler=False, таска немедленно.
    bootstrap_task, _created = orchestrator.create_or_coalesce_node_bootstrap(
        node,
        {"pool_id": node.pool_id, "rerun": True, "reason": "reality-dest refresh"},
        defer_to_reconciler=False,
    )
    db.commit()
    if _created:
        orchestrator.run_task_async(bootstrap_task, node=node)
    bootstrap_task_id = bootstrap_task.id if bootstrap_task else None

    subs = (
        db.query(models.Subscription)
        .filter(
            models.Subscription.node_id == node_id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .all()
    )
    failed: list[int] = []
    task_ids: list[int] = []
    if bootstrap_task_id is not None:
        task_ids.append(bootstrap_task_id)
    for sub in subs:
        try:
            # Снимок имён активных девайсов ДО revoke — чтобы не
            # схлопнуть N девайсов в один "primary" (см. комментарий
            # в migrate_subscription_to_new_node).
            live_names = [
                d.name or "primary"
                for d in list(sub.devices)
                if d.status
                not in (models.DeviceStatus.disabled, models.DeviceStatus.revoked)
            ]
            for device in list(sub.devices):
                if device.status in (
                    models.DeviceStatus.disabled,
                    models.DeviceStatus.revoked,
                ):
                    continue
                orchestrator.revoke_device(
                    device, reason="reality-dest refresh", background=True
                )
            if not live_names:
                live_names = ["primary"]
            for name in live_names:
                _device, task = orchestrator.reprovision_subscription(
                    sub, device_name=name
                )
                if task is not None:
                    task_ids.append(task.id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "refresh-reality-dest: reprovision failed sub=%s", sub.id
            )
            failed.append(sub.id)

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "node_reality_dest_refreshed",
        "vpn_node",
        node.id,
        actor_type=actor_type,
        metadata={
            "old_sni": old_sni,
            "new_sni": new_sni,
            "from_pool": new_sni in REALITY_DEST_POOL,
            "sub_count": len(subs),
            "failed_subs": failed,
            "bootstrap_task_id": bootstrap_task_id,
            "task_ids": task_ids,
        },
    )

    return schemas.NodeRefreshDestOut(
        node_id=node.id,
        old_sni=old_sni,
        new_sni=new_sni,
        sub_count=len(subs),
        failed_subs=failed,
        task_ids=task_ids,
    )


@router.post("/nodes/ssh/ping/refresh", status_code=200)
def refresh_traffic_stats_tick(
    admin_token: str = Depends(require_admin),
):
    """Форс-прогон traffic_stats тика прямо сейчас.

    Симметрично ``POST /exits/links/health/refresh`` для relay-link
    health: админка зовёт эту ручку если в таблице нод колонка
    ``SSH · обновлено`` показывает 10+ минут у кого-то и не понятно,
    тик ли умер или конкретная нода недоступна. Один клик — tick
    сразу ставится в очередь, воркер SSH'ит на все active/draining
    ноды, каждая успешная читается как строка в
    ``node_traffic_samples`` (а ``last_ssh_at`` = max(observed_at)
    в ответе list_nodes). Если после 20 сек у ноды всё ещё старый
    ``observed_at`` — значит SSH или Xray API именно у неё сломаны,
    а не воркер.

    SSH-ключ смонтирован только в worker-контейнер, поэтому здесь
    inline не собираем — только enqueue через RQ на тот же
    deterministic ``tick-traffic-stats`` job_id, что и периодический
    scheduler из worker.main. Каскада копий не будет: если тик уже
    крутится — вернём его id без нового enqueue; если в scheduled —
    снимаем и кладём без delay, чтобы воркер подхватил сразу.

    Response identical to ``refresh_relay_link_health``:
      * ``enqueued=true`` + ``job_id`` — tick в очереди, обновляй
        список нод через 15–30 сек;
      * ``enqueued=false`` — Redis недоступен, индикаторы не
        обновятся пока очередь не поднимется.
    """
    from ..queue import RESULT_TTL, TICK_IDS, get_queue
    from rq.exceptions import NoSuchJobError
    from rq.job import Job
    from rq.registry import StartedJobRegistry

    queue = get_queue()
    if queue is None:
        return {"enqueued": False, "reason": "queue unavailable"}

    tick_id = TICK_IDS["app.worker.run_traffic_stats_tick"]

    try:
        StartedJobRegistry(queue=queue).cleanup()
    except Exception:  # noqa: BLE001
        pass

    try:
        existing = Job.fetch(tick_id, connection=queue.connection)
        if existing.get_status(refresh=True) == "started":
            return {"enqueued": True, "job_id": existing.id, "note": "already running"}
        existing.delete()
    except NoSuchJobError:
        pass

    job = queue.enqueue(
        "app.worker.run_traffic_stats_tick",
        job_id=tick_id,
        result_ttl=RESULT_TTL,
    )
    return {"enqueued": True, "job_id": job.id}
