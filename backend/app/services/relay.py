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
