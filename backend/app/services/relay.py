"""Helpers for the relay↔exit attachment flow.

Central spot for IP allocation inside an exit's WireGuard subnet and
the ``VPNNode.relay_config`` projection. Kept out of ``api.exits`` so
the bootstrap worker (stage D) can reuse the same building blocks
without importing FastAPI.
"""
from __future__ import annotations

import ipaddress
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..security import encrypt as _encrypt


class RelayAllocationError(Exception):
    """Raised when no free client address remains in an exit's subnet."""


def _parse_subnet(wg_address_v4: str) -> ipaddress.IPv4Network:
    """Return the /24 (or whatever prefix) the exit's address sits in."""
    iface = ipaddress.IPv4Interface(wg_address_v4)
    return iface.network


def _taken_hosts(db: Session, exit_id: int) -> set[int]:
    """Host octets (int) already claimed by links for this exit."""
    rows = (
        db.query(models.RelayExitLink.wg_client_address_v4)
        .filter(models.RelayExitLink.exit_id == exit_id)
        .all()
    )
    taken: set[int] = set()
    for (addr,) in rows:
        try:
            taken.add(int(ipaddress.IPv4Interface(addr).ip))
        except ValueError:
            continue
    return taken


def allocate_client_address(
    db: Session, exit_node: models.WGExitNode
) -> str:
    """Pick the next free ``/32`` address for a new peer on this exit.

    Walks hosts in the exit's subnet excluding the network, broadcast
    and the server's own address, and returns the first one not yet in
    ``relay_exit_links``. Raises :class:`RelayAllocationError` when the
    subnet is full.
    """
    server_iface = ipaddress.IPv4Interface(exit_node.wg_address_v4)
    subnet = server_iface.network
    server_ip = int(server_iface.ip)
    taken = _taken_hosts(db, exit_node.id)
    for host in subnet.hosts():
        host_int = int(host)
        if host_int == server_ip or host_int in taken:
            continue
        return f"{host}/32"
    raise RelayAllocationError(
        f"Exit {exit_node.name} WG subnet {subnet} is full"
    )


def validate_requested_address(
    db: Session, exit_node: models.WGExitNode, requested: str
) -> str:
    """Validate an admin-supplied client address belongs to this exit.

    Must be ``/32`` (single host), must sit inside the exit's subnet,
    must not collide with the server address or any existing link.
    """
    try:
        iface = ipaddress.IPv4Interface(requested)
    except ValueError as exc:
        raise RelayAllocationError(f"Invalid CIDR: {requested}") from exc
    if iface.network.prefixlen != 32:
        raise RelayAllocationError("Address must be /32 (single host)")
    server_iface = ipaddress.IPv4Interface(exit_node.wg_address_v4)
    subnet = server_iface.network
    if iface.ip not in subnet:
        raise RelayAllocationError(
            f"Address {iface.ip} not in exit subnet {subnet}"
        )
    if iface.ip == server_iface.ip:
        raise RelayAllocationError("Address collides with exit's server address")
    if int(iface.ip) in _taken_hosts(db, exit_node.id):
        raise RelayAllocationError(f"Address {iface.ip} already taken")
    return f"{iface.ip}/32"


def build_relay_config(
    *,
    exit_node: models.WGExitNode,
    client_private_key: str,
    client_address_v4: str,
) -> dict[str, Any]:
    """Build the ``VPNNode.relay_config`` JSONB dict for the worker.

    ``wg_private_key`` is stored encrypted (Fernet) — see
    ``provisioning._collect_site_extra_vars`` for the decrypt path.
    """
    if not exit_node.wg_public_key:
        raise RelayAllocationError(
            f"Exit {exit_node.name} has no wg_public_key — run keygen first"
        )
    return {
        "wg_private_key_enc": _encrypt(client_private_key),
        "wg_address_v4": client_address_v4,
        "wg_endpoint": f"{exit_node.host}:{exit_node.wg_port}",
        "wg_exit_public_key": exit_node.wg_public_key,
    }


# ── G.4 multi-exit helpers ────────────────────────────────────────────
# Picked up by the credential creation paths (cold + warm-pool) so that
# every cred already carries the exit it should egress through. In the
# 1:1 case (legacy) this is just "the single attached exit"; once G.5
# lifts the attach guard it becomes real least-loaded balancing.

def choose_exit_for_relay(
    db: Session, relay: "models.VPNNode"
) -> int | None:
    """Pick the least-loaded ``exit_id`` among this relay's active links.

    Counts live credentials per exit among credentials bound to this
    relay's node_id (``pool_state != revoked`` + ``is_active = True``)
    and returns the exit with the lowest count. Ties break by exit_id
    ascending so the choice is deterministic under equal load.

    Returns ``None`` when the relay has no links — the caller treats
    this as "don't set exit_id" (legacy path, relay's own egress).
    """
    links = (
        db.query(models.RelayExitLink)
        .filter(models.RelayExitLink.relay_node_id == relay.id)
        .all()
    )
    if not links:
        return None
    if len(links) == 1:
        return links[0].exit_id

    from sqlalchemy import func as _func

    counts: dict[int, int] = {link.exit_id: 0 for link in links}
    rows = (
        db.query(models.Credential.exit_id, _func.count(models.Credential.id))
        .filter(
            models.Credential.node_id == relay.id,
            models.Credential.exit_id.isnot(None),
            models.Credential.is_active.is_(True),
            models.Credential.pool_state != models.CredentialPoolState.revoked,
        )
        .group_by(models.Credential.exit_id)
        .all()
    )
    for exit_id, cnt in rows:
        if exit_id in counts:
            counts[exit_id] = cnt

    return min(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def next_wg_interface_name(db: Session, relay_node_id: int) -> str:
    """Smallest free ``wgN`` interface name for a new link on this relay.

    Walks the relay's existing ``RelayExitLink`` rows, collects their
    ``wg_interface_name``, and returns the first ``wg{N}`` that isn't
    taken starting from ``wg0``. Used by G.5+ attach flow once the
    1:1 guard lifts; in the interim the attach endpoint still short-
    circuits on the first "already attached" check and this helper is
    exercised only by unit tests.
    """
    taken = {
        name
        for (name,) in (
            db.query(models.RelayExitLink.wg_interface_name)
            .filter(models.RelayExitLink.relay_node_id == relay_node_id)
            .all()
        )
    }
    for n in range(128):
        candidate = f"wg{n}"
        if candidate not in taken:
            return candidate
    raise RelayAllocationError(
        f"Relay {relay_node_id} has exhausted wg0..wg127 interface names"
    )


# ── G.6 xray fan-out helpers ──────────────────────────────────────────
# Multi-link relays need one freedom outbound per attached exit (each
# with its own ``sockopt.interface: wgN``) plus routing rules matching
# user emails to the right outbound tag. The single-link and non-relay
# cases return empty lists — the template and role degrade to the
# legacy (pre-G.6) shape transparently.

def resolve_exit_interface(
    db: Session, relay_node_id: int, exit_id: int | None
) -> str | None:
    """Return ``wg_interface_name`` for ``(relay, exit)`` — or ``None``.

    Used by the per-device provisioning path so ``provision_device.yml``
    can tell the manage_vless_*_user.sh scripts which exit a newly
    added user should route through. Returns ``None`` when:

      * ``exit_id`` is ``None`` (cred is not pinned to an exit — warm-
        pool cold entries, legacy pre-G.4 creds, direct nodes);
      * the relay has no link to that exit (stale exit_id after
        detach — caller should treat as "default outbound").
    """
    if exit_id is None:
        return None
    row = (
        db.query(models.RelayExitLink.wg_interface_name)
        .filter(
            models.RelayExitLink.relay_node_id == relay_node_id,
            models.RelayExitLink.exit_id == exit_id,
        )
        .first()
    )
    return row[0] if row else None


def build_xray_relay_outbounds(
    db: Session, relay: "models.VPNNode"
) -> list[dict[str, Any]]:
    """Build the ``xray_relay_outbounds`` fan-out list for this relay.

    Each entry drives both a freedom outbound (tag ``direct-wgN``,
    ``sockopt.interface: wgN``) and a routing rule
    (``user: [emails…]  outboundTag: direct-wgN``) in the three
    xray config templates.

    Only multi-link relays return a non-empty list — single-link
    relays keep the legacy "direct + sockopt patch on the only wgN"
    flow, and direct nodes return ``[]``. Credentials with
    ``exit_id is NULL`` (cold warm-pool, legacy) are excluded so they
    fall through to the default ``direct`` outbound; the role picks a
    primary interface for ``direct`` so even unclassified traffic
    still egresses via tunnel.
    """
    links = (
        db.query(models.RelayExitLink)
        .filter(models.RelayExitLink.relay_node_id == relay.id)
        .order_by(models.RelayExitLink.wg_interface_name)
        .all()
    )
    if len(links) <= 1:
        return []
    iface_by_exit = {link.exit_id: link.wg_interface_name for link in links}

    # Include warm bundles (``pool_state=warm``, ``is_active=False``
    # pre-assignment) alongside assigned creds. Warm emails are already
    # in the xray clients[] (added by warm_one_bundle's ansible run),
    # so they must also appear in the routing rule — otherwise a
    # site.yml re-render would drop the rule and route the user
    # through ``direct`` (fallback) instead of their pinned exit.
    from sqlalchemy import or_ as _or
    rows = (
        db.query(
            models.Credential.exit_id,
            models.Credential.access_username,
        )
        .filter(
            models.Credential.node_id == relay.id,
            models.Credential.exit_id.isnot(None),
            _or(
                models.Credential.is_active.is_(True),
                models.Credential.pool_state == models.CredentialPoolState.warm,
            ),
            models.Credential.pool_state != models.CredentialPoolState.revoked,
        )
        .all()
    )
    emails_by_iface: dict[str, set[str]] = {
        iface: set() for iface in iface_by_exit.values()
    }
    for exit_id, username in rows:
        iface = iface_by_exit.get(exit_id)
        if iface is None or not username:
            continue
        emails_by_iface[iface].add(username)

    return [
        {"interface": iface, "emails": sorted(emails_by_iface[iface])}
        for iface in sorted(emails_by_iface.keys())
    ]


def primary_wg_interface(
    db: Session, relay_node_id: int
) -> str | None:
    """Return the interface used as the default ``direct`` sockopt.

    Picks the smallest ``wgN`` among this relay's links — deterministic
    across runs. ``None`` for non-relay nodes (direct egress — no
    sockopt on the outbound). In the single-link case the caller can
    skip emitting ``xray_relay_outbounds`` entirely; the template
    still needs this value to render the ``direct`` outbound's
    sockopt so unclassified UUIDs tunnel out too.
    """
    row = (
        db.query(models.RelayExitLink.wg_interface_name)
        .filter(models.RelayExitLink.relay_node_id == relay_node_id)
        .order_by(models.RelayExitLink.wg_interface_name)
        .first()
    )
    return row[0] if row else None
