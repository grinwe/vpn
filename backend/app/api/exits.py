"""WireGuard exit-node admin endpoints: ``/api/exits/*``.

CRUD for foreign exit nodes that RU relay jump nodes tunnel to. See
``docs/RELAY_ROADMAP.md`` stage B for the architecture.

Private keys are stored encrypted via ``security.encrypt`` (same Fernet
scheme as cloud tokens). Responses expose ``wg_public_key`` but never
the private half — only ``has_private_key: bool`` to let the UI tell
whether keygen is needed.
"""
from __future__ import annotations

import heapq
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from .. import models, schemas
from ..auth import require_admin
from ..security import decrypt, encrypt as _encrypt
from ..services.provisioning import ProvisioningOrchestrator
from ..services.relay import (
    RelayAllocationError,
    allocate_client_address,
    build_relay_config,
    next_wg_interface_name,
    validate_requested_address,
)
from ..services.vless import generate_wireguard_keypair
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


def _link_to_out(
    link: models.RelayExitLink, *, active_subs: int = 0
) -> schemas.RelayExitLinkOut:
    return schemas.RelayExitLinkOut(
        id=link.id,
        relay_node_id=link.relay_node_id,
        relay_node_name=link.relay_node.name if link.relay_node else "",
        exit_id=link.exit_id,
        exit_name=link.exit_node.name if link.exit_node else "",
        wg_interface_name=link.wg_interface_name,
        wg_client_public_key=link.wg_client_public_key,
        wg_client_address_v4=link.wg_client_address_v4,
        created_at=link.created_at,
        last_handshake_at=link.last_handshake_at,
        last_rx_bytes=link.last_rx_bytes,
        last_tx_bytes=link.last_tx_bytes,
        last_observed_at=link.last_observed_at,
        active_subs=active_subs,
    )


def _active_subs_by_exit(
    db: Session, exit_ids: list[int]
) -> dict[int, int]:
    """Distinct active subscriptions pinned to each exit via Credential.exit_id.

    Источник правды — Credential.exit_id (G.4+ заполняется provisioning'ом при
    assign-е). Если relay линкуется к нескольким exit'ам, одна и та же подписка
    попадёт ровно в один бакет — тот, к которому её cred'ы реально прибиты.
    COUNT(DISTINCT subscription_id) на случай нескольких cred'ов (разные
    протоколы) у одной sub'ы.

    Legacy fallback: если cred.exit_id IS NULL (1:1 relay до G.4 миграции или
    warm-bundle до assign'а), а relay подписки имеет ровно один RelayExitLink
    — атрибутим такой sub к этому единственному exit'у. Без этого legacy
    подписки светят 0 на всех exit'ах и UI бесполезен.
    """
    if not exit_ids:
        return {}
    out: dict[int, int] = {}
    explicit = (
        db.query(
            models.Credential.exit_id,
            func.count(func.distinct(models.Credential.subscription_id)).label("cnt"),
        )
        .join(
            models.Subscription,
            models.Subscription.id == models.Credential.subscription_id,
        )
        .filter(models.Subscription.status == models.SubscriptionStatus.active)
        .filter(models.Credential.is_active.is_(True))
        .filter(models.Credential.exit_id.in_(exit_ids))
        .group_by(models.Credential.exit_id)
        .all()
    )
    for eid, cnt in explicit:
        out[eid] = cnt

    single_link_rows = (
        db.query(
            models.RelayExitLink.relay_node_id,
            func.max(models.RelayExitLink.exit_id).label("eid"),
        )
        .group_by(models.RelayExitLink.relay_node_id)
        .having(func.count(models.RelayExitLink.id) == 1)
        .all()
    )
    relay_to_sole_exit = {
        rid: eid for rid, eid in single_link_rows if eid in exit_ids
    }
    if not relay_to_sole_exit:
        return out

    legacy = (
        db.query(
            models.Subscription.node_id,
            func.count(func.distinct(models.Subscription.id)).label("cnt"),
        )
        .filter(models.Subscription.status == models.SubscriptionStatus.active)
        .filter(models.Subscription.node_id.in_(list(relay_to_sole_exit.keys())))
        .filter(
            ~models.Subscription.id.in_(
                db.query(models.Credential.subscription_id)
                .filter(models.Credential.is_active.is_(True))
                .filter(models.Credential.exit_id.isnot(None))
            )
        )
        .group_by(models.Subscription.node_id)
        .all()
    )
    for relay_id, cnt in legacy:
        eid = relay_to_sole_exit.get(relay_id)
        if eid is not None:
            out[eid] = out.get(eid, 0) + cnt
    return out


def _active_subs_by_relay_for_exit(
    db: Session, exit_id: int
) -> dict[int, int]:
    """Для заданного exit — сколько подписок ходит через каждую relay-ноду.

    Группируем по Subscription.node_id, но жёстко фильтруем Credential.exit_id
    = exit_id — чтобы link (relay→exit) показывал только тех юзеров, кого
    этот link реально обслуживает, а не всех subs релея. Legacy fallback для
    single-link relay с NULL cred.exit_id — симметрично _active_subs_by_exit.
    """
    out: dict[int, int] = {}
    explicit = (
        db.query(
            models.Subscription.node_id,
            func.count(func.distinct(models.Subscription.id)).label("cnt"),
        )
        .join(
            models.Credential,
            models.Credential.subscription_id == models.Subscription.id,
        )
        .filter(models.Subscription.status == models.SubscriptionStatus.active)
        .filter(models.Credential.is_active.is_(True))
        .filter(models.Credential.exit_id == exit_id)
        .group_by(models.Subscription.node_id)
        .all()
    )
    for relay_id, cnt in explicit:
        out[relay_id] = cnt

    single_link_rows = (
        db.query(
            models.RelayExitLink.relay_node_id,
            func.max(models.RelayExitLink.exit_id).label("eid"),
        )
        .group_by(models.RelayExitLink.relay_node_id)
        .having(func.count(models.RelayExitLink.id) == 1)
        .all()
    )
    single_link_relays = [rid for rid, eid in single_link_rows if eid == exit_id]
    if not single_link_relays:
        return out

    legacy = (
        db.query(
            models.Subscription.node_id,
            func.count(func.distinct(models.Subscription.id)).label("cnt"),
        )
        .filter(models.Subscription.status == models.SubscriptionStatus.active)
        .filter(models.Subscription.node_id.in_(single_link_relays))
        .filter(
            ~models.Subscription.id.in_(
                db.query(models.Credential.subscription_id)
                .filter(models.Credential.is_active.is_(True))
                .filter(models.Credential.exit_id.isnot(None))
            )
        )
        .group_by(models.Subscription.node_id)
        .all()
    )
    for relay_id, cnt in legacy:
        out[relay_id] = out.get(relay_id, 0) + cnt
    return out


def _links_mini(links: list[models.RelayExitLink]) -> list[schemas.ExitLinkHealthMini]:
    return [
        schemas.ExitLinkHealthMini(
            relay_node_id=link.relay_node_id,
            relay_node_name=link.relay_node.name if link.relay_node else "",
            wg_interface_name=link.wg_interface_name,
            last_handshake_at=link.last_handshake_at,
            last_observed_at=link.last_observed_at,
        )
        for link in links
    ]


def _to_out(
    exit_node: models.WGExitNode,
    *,
    peers_count: int = 0,
    links: list[models.RelayExitLink] | None = None,
    active_subs_total: int = 0,
) -> schemas.WGExitNodeOut:
    return schemas.WGExitNodeOut(
        id=exit_node.id,
        name=exit_node.name,
        region=exit_node.region,
        host=exit_node.host,
        ssh_port=exit_node.ssh_port,
        wg_port=exit_node.wg_port,
        wg_address_v4=exit_node.wg_address_v4,
        wg_public_key=exit_node.wg_public_key,
        has_private_key=exit_node.wg_private_key_enc is not None,
        provider_id=exit_node.provider_id,
        provider_external_id=exit_node.provider_external_id,
        provider_region=exit_node.provider_region,
        status=exit_node.status.value,
        is_active=exit_node.is_active,
        notes=exit_node.notes,
        peers_count=peers_count,
        links=_links_mini(links or []),
        active_subs_total=active_subs_total,
        created_at=exit_node.created_at,
        updated_at=exit_node.updated_at,
    )


def _peers_count(db: Session, exit_id: int) -> int:
    return (
        db.query(models.RelayExitLink)
        .filter(models.RelayExitLink.exit_id == exit_id)
        .count()
    )


@router.get("/exits", response_model=list[schemas.WGExitNodeOut])
def list_exits(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    rows = db.query(models.WGExitNode).order_by(models.WGExitNode.id).all()
    # Один запрос на все линки — группируем в питоне по exit_id. Дешевле
    # чем per-row select + даёт peers_count "бесплатно" (len группы).
    all_links = (
        db.query(models.RelayExitLink)
        .join(models.VPNNode, models.VPNNode.id == models.RelayExitLink.relay_node_id)
        .all()
    )
    links_by_exit: dict[int, list[models.RelayExitLink]] = {}
    for link in all_links:
        links_by_exit.setdefault(link.exit_id, []).append(link)
    subs_by_exit = _active_subs_by_exit(db, [r.id for r in rows])
    return [
        _to_out(
            r,
            peers_count=len(links_by_exit.get(r.id, [])),
            links=links_by_exit.get(r.id, []),
            active_subs_total=subs_by_exit.get(r.id, 0),
        )
        for r in rows
    ]


@router.post("/exits", response_model=schemas.WGExitNodeOut)
def create_exit(
    payload: schemas.WGExitNodeCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    if db.query(models.WGExitNode).filter(models.WGExitNode.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Exit node with this name already exists")

    # Stage E — an exit without a private key can't be bootstrapped, and
    # the admin UI shouldn't need two clicks (create → keygen) to get a
    # usable node. Auto-generate on the server side when the admin hasn't
    # provided the half themselves (e.g. bringing over an existing server).
    public_key = payload.wg_public_key
    private_key = payload.wg_private_key
    if not private_key:
        public_key, private_key = generate_wireguard_keypair()

    exit_node = models.WGExitNode(
        name=payload.name,
        region=payload.region,
        host=payload.host,
        ssh_port=payload.ssh_port,
        wg_port=payload.wg_port,
        wg_address_v4=payload.wg_address_v4,
        wg_public_key=public_key,
        wg_private_key_enc=_encrypt(private_key),
        provider_id=payload.provider_id,
        provider_external_id=payload.provider_external_id,
        provider_region=payload.provider_region,
        is_active=payload.is_active,
        notes=payload.notes,
    )
    db.add(exit_node)
    db.commit()
    db.refresh(exit_node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "wg_exit_created", "wg_exit_node", exit_node.id, actor_type=actor_type)

    # Stage E — schedule the Ansible bootstrap on the worker. The role
    # is idempotent, and this first run carries an empty peer list (the
    # exit is unattached). Attaching a relay later re-runs bootstrap_exit
    # as part of the relay_tunnel task so the peer list stays in sync.
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task("exit", exit_node.id, "bootstrap", {})
    db.commit()
    orchestrator.run_task_async(task)

    return _to_out(exit_node, peers_count=0)


@router.get("/exits/{exit_id}", response_model=schemas.WGExitNodeOut)
def get_exit(
    exit_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    return _to_out(exit_node, peers_count=_peers_count(db, exit_id))


@router.patch("/exits/{exit_id}", response_model=schemas.WGExitNodeOut)
def update_exit(
    exit_id: int,
    payload: schemas.WGExitNodePatch,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")

    if payload.name is not None:
        if (
            db.query(models.WGExitNode)
            .filter(models.WGExitNode.name == payload.name, models.WGExitNode.id != exit_id)
            .first()
        ):
            raise HTTPException(status_code=409, detail="Another exit node with this name exists")
        exit_node.name = payload.name
    if payload.region is not None:
        exit_node.region = payload.region
    if payload.host is not None:
        exit_node.host = payload.host
    if payload.ssh_port is not None:
        exit_node.ssh_port = payload.ssh_port
    if payload.wg_port is not None:
        exit_node.wg_port = payload.wg_port
    if payload.wg_address_v4 is not None:
        exit_node.wg_address_v4 = payload.wg_address_v4
    if payload.wg_public_key is not None:
        exit_node.wg_public_key = payload.wg_public_key
    if payload.wg_private_key is not None:
        exit_node.wg_private_key_enc = _encrypt(payload.wg_private_key)
    if payload.provider_id is not None:
        exit_node.provider_id = payload.provider_id
    if payload.provider_external_id is not None:
        exit_node.provider_external_id = payload.provider_external_id
    if payload.provider_region is not None:
        exit_node.provider_region = payload.provider_region
    if payload.status is not None:
        try:
            exit_node.status = models.WGExitNodeStatus(payload.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Unknown status") from exc
    if payload.is_active is not None:
        exit_node.is_active = payload.is_active
    if payload.notes is not None:
        exit_node.notes = payload.notes

    db.commit()
    db.refresh(exit_node)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "wg_exit_updated", "wg_exit_node", exit_node.id, actor_type=actor_type)
    return _to_out(exit_node, peers_count=_peers_count(db, exit_node.id))


@router.delete("/exits/{exit_id}", status_code=200)
def delete_exit(
    exit_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    peers = _peers_count(db, exit_id)
    if peers > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Exit has {peers} attached relay(s); detach them first",
        )
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "wg_exit_deleted", "wg_exit_node", exit_node.id, actor_type=actor_type)
    db.delete(exit_node)
    db.commit()
    return {"exit_id": exit_id, "deleted": True}


@router.post("/exits/_keygen")
def preview_keygen(
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    """Generate a fresh WG keypair without persisting — form helper.

    The ``ExitForm`` in the admin UI calls this so the operator can
    fill the create/edit form with a valid keypair in one click
    (browsers sometimes autofill ``type=password`` fields with unrelated
    credentials, producing a urlsafe-base64 string that wg-quick
    rejects with "Key is not the correct length or format"). No DB
    row is created — the generated pair is only stored if the admin
    submits the form.
    """
    pub, priv = generate_wireguard_keypair()
    return {"wg_public_key": pub, "wg_private_key": priv}


@router.post("/exits/{exit_id}/keygen", response_model=schemas.WGExitKeygenOut)
def keygen_exit(
    exit_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Generate a fresh X25519 WireGuard keypair for this exit.

    Overwrites any existing keys on the row. Returns only the public key
    — the private half is stored encrypted and never exposed via API.
    """
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    pub, priv = generate_wireguard_keypair()
    exit_node.wg_public_key = pub
    exit_node.wg_private_key_enc = _encrypt(priv)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(db, actor, "wg_exit_keygen", "wg_exit_node", exit_node.id, actor_type=actor_type)
    return schemas.WGExitKeygenOut(id=exit_node.id, wg_public_key=pub)


@router.post("/exits/{exit_id}/bootstrap")
def rebootstrap_exit(
    exit_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Re-run ``bootstrap_exit.yml`` against an existing exit.

    Mirror of :func:`~backend.app.api.nodes.rebootstrap_node` for WG
    exit nodes. Use when the ``wg_exit_node`` role changed, manual
    edits on the server need to be reconciled, or keys were rotated.
    The role is idempotent — the peer list is re-rendered from the
    current ``relay_exit_links`` rows, so attached relays keep their
    tunnels. Creates a fresh ``bootstrap`` task (same action
    :func:`create_exit` schedules on first registration) and hands it
    to the orchestrator.
    """
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task(
        "exit", exit_node.id, "bootstrap", {"rerun": True}
    )
    db.commit()
    orchestrator.run_task_async(task)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "wg_exit_bootstrap_rerun",
        "wg_exit_node",
        exit_node.id,
        actor_type=actor_type,
        metadata={"task_id": task.id},
    )
    return {"exit_id": exit_node.id, "task_id": task.id}


@router.post("/exits/{exit_id}/diagnose")
def diagnose_exit(
    exit_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Run ``diagnose_exit.yml`` (read-only health probe) on the exit.

    Mirror of :func:`~backend.app.api.nodes.diagnose_node`. Collects
    ``wg show wg0``, ``wg-quick@wg0`` systemd state, listening UDP
    sockets and routing/NAT — none of which mutate the exit. The
    task stdout is surfaced in the admin UI so the operator can see
    per-peer handshake freshness at a glance. The exit's status is
    NOT flipped on result (see the ``action == "diagnose"`` guard in
    ``_handle_task_outcome``) — diagnosing a working exit must never
    knock it into ``error``.
    """
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task("exit", exit_node.id, "diagnose", {})
    db.commit()
    orchestrator.run_task_async(task)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "wg_exit_diagnose",
        "wg_exit_node",
        exit_node.id,
        actor_type=actor_type,
        metadata={"task_id": task.id},
    )
    return {"exit_id": exit_node.id, "task_id": task.id}


@router.get("/exits/{exit_id}/links", response_model=list[schemas.RelayExitLinkOut])
def list_exit_links(
    exit_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
):
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    rows = (
        db.query(models.RelayExitLink)
        .filter(models.RelayExitLink.exit_id == exit_id)
        .order_by(models.RelayExitLink.id)
        .all()
    )
    subs_by_relay = _active_subs_by_relay_for_exit(db, exit_id)
    return [
        _link_to_out(r, active_subs=subs_by_relay.get(r.relay_node_id, 0))
        for r in rows
    ]


@router.post("/exits/{exit_id}/links", response_model=schemas.RelayExitLinkOut)
def attach_relay(
    exit_id: int,
    payload: schemas.RelayExitLinkCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Attach a relay VPN node to this exit (G.5: multi-exit per relay).

    Generates a fresh WG client keypair, allocates a free /32 in the
    exit's subnet, allocates the next free ``wgN`` kernel interface
    name for this relay, and writes the link row — all in one
    transaction. The legacy ``relay_config`` JSONB is kept in sync as
    a "relay mode" flag (first link's snapshot) so UI badges still
    work; extra_vars for ansible are now sourced from
    ``relay_exit_links`` directly. Then schedules a ``relay_tunnel``
    task on the worker that re-renders the exit's peer list and
    brings wg-quick@wgN up on the relay.
    """
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    if not exit_node.is_active:
        raise HTTPException(status_code=400, detail="Exit is not active")

    relay = db.get(models.VPNNode, payload.relay_node_id)
    if not relay:
        raise HTTPException(status_code=404, detail="Relay node not found")

    # Idempotency guard — a (relay, exit) pair is still unique, so a
    # double-attach to the same exit returns 409 instead of producing
    # two interfaces that tunnel to the same peer. The previous guard
    # (relay attached to *any* exit) was removed in G.5 to allow
    # multi-exit.
    dup = (
        db.query(models.RelayExitLink)
        .filter(
            models.RelayExitLink.relay_node_id == relay.id,
            models.RelayExitLink.exit_id == exit_node.id,
        )
        .first()
    )
    if dup:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Relay {relay.name} is already attached to exit "
                f"{exit_node.name} on interface {dup.wg_interface_name}"
            ),
        )

    try:
        if payload.wg_client_address_v4:
            client_address = validate_requested_address(
                db, exit_node, payload.wg_client_address_v4
            )
        else:
            client_address = allocate_client_address(db, exit_node)
        iface_name = next_wg_interface_name(db, relay.id)
        pub, priv = generate_wireguard_keypair()
        relay_config = build_relay_config(
            exit_node=exit_node,
            client_private_key=priv,
            client_address_v4=client_address,
        )
    except RelayAllocationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    link = models.RelayExitLink(
        relay_node_id=relay.id,
        exit_id=exit_node.id,
        wg_interface_name=iface_name,
        wg_client_private_key_enc=_encrypt(priv),
        wg_client_public_key=pub,
        wg_client_address_v4=client_address,
    )
    db.add(link)
    # Keep relay_config populated — it's still the "is this a relay?"
    # flag for the admin schema (``has_relay_config``). Ansible no
    # longer reads it; the multi-link case wins last-writer here,
    # which is fine because the field is only used as a boolean.
    relay.relay_config = relay_config
    try:
        db.commit()
    except IntegrityError as exc:
        # Read-then-insert guard выше не race-safe: параллельный
        # POST для той же (relay, exit) пары мог проскочить. БД
        # ловит такое через UNIQUE (uq_relay_exit_links_relay_exit,
        # миграция 0031) — конвертим в 409 Conflict, чтобы клиент
        # увидел привычный код вместо 500.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"Relay {relay.name} is already attached to exit "
                f"{exit_node.name} (race на параллельном attach)"
            ),
        ) from exc
    db.refresh(link)

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "relay_exit_attached", "relay_exit_link", link.id,
        actor_type=actor_type,
    )

    # Stage E — worker task runs bootstrap_exit.yml (new peer added to
    # the server's wg0.conf) then relay_tunnel_apply.yml (wg0 up + Xray
    # sockopt patched) on the relay. exit_id is carried on the payload
    # so a future detach task can still find the right exit after
    # relay_config is cleared.
    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task(
        "relay_tunnel",
        relay.id,
        "apply",
        {"exit_id": exit_node.id, "link_id": link.id},
    )
    db.commit()
    orchestrator.run_task_async(task)

    return _link_to_out(link)


@router.post(
    "/exits/batch-attach",
    response_model=schemas.BatchAttachRelayResponse,
)
def batch_attach_relay(
    payload: schemas.BatchAttachRelayRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Ensure: один relay прицеплен ко всем выбранным exit'ам одним запросом.

    Семантика «ensure», не «strict attach»:
      - exit_id, к которому relay ещё НЕ прицеплен → INSERT link + new
        ``relay_tunnel apply`` task. ``mode="attached"`` в ответе.
      - exit_id, к которому relay уже прицеплен → re-apply: НЕ
        INSERT'им новый link (keypair и /32 сохраняются), просто
        создаём свежий ``relay_tunnel apply`` task на существующем
        link'е. ``mode="reapplied"`` в ответе.

    Зачем так: оператор открывает модалку на /exits и хочет «прогнать
    relay-tunnel для этих exit'ов» — независимо от того, новые они
    или уже прицеплены. Strict-режим с 409 на дубликаты делал re-apply
    невозможным через тот же UI (см. issue 2026-05-27).

    Все task'и (attach'и и re-apply'и) идут под общим ``batch_id`` —
    UI рендерит их одной таблицей с прогрессом N/M, retry отдельных
    upal'нувших через обычную /tasks-кнопку.

    Защита от race в ``allocate_client_address`` для НОВЫХ link'ов:
    каждый link добавляем через ``db.add()`` + ``db.flush()`` ДО
    следующего allocate в той же транзакции — следующий
    ``_taken_hosts`` уже видит свежий address как pending-INSERT и
    пропустит его. То же для ``next_wg_interface_name``.
    """
    if not payload.exit_ids:
        raise HTTPException(
            status_code=400, detail="exit_ids must be non-empty"
        )

    if len(set(payload.exit_ids)) != len(payload.exit_ids):
        raise HTTPException(
            status_code=400,
            detail="exit_ids содержит дубликаты — почистите список",
        )

    relay = db.get(models.VPNNode, payload.relay_node_id)
    if not relay:
        raise HTTPException(status_code=404, detail="Relay node not found")

    exits_by_id: dict[int, models.WGExitNode] = {}
    for eid in payload.exit_ids:
        exit_node = db.get(models.WGExitNode, eid)
        if not exit_node:
            raise HTTPException(
                status_code=404, detail=f"Exit node {eid} not found"
            )
        if not exit_node.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"Exit {exit_node.name} is not active",
            )
        exits_by_id[eid] = exit_node

    existing_links_by_exit: dict[int, models.RelayExitLink] = {
        row.exit_id: row
        for row in (
            db.query(models.RelayExitLink)
            .filter(
                models.RelayExitLink.relay_node_id == relay.id,
                models.RelayExitLink.exit_id.in_(payload.exit_ids),
            )
            .all()
        )
    }

    batch_id = uuid.uuid4()
    orchestrator = ProvisioningOrchestrator(db)
    # mode → (link, exit_node, task). Сохраняем порядок payload.exit_ids
    # чтобы в ответе links шли в том же порядке, что выбрал юзер.
    created_entries: list[
        tuple[str, models.RelayExitLink, models.WGExitNode, models.ProvisioningTask]
    ] = []
    relay_config_snapshot: dict[str, Any] | None = None

    try:
        for exit_id in payload.exit_ids:
            exit_node = exits_by_id[exit_id]
            existing = existing_links_by_exit.get(exit_id)
            if existing is not None:
                # Re-apply на существующем link'е: новый task с тем же
                # link_id, никаких INSERT'ов. keypair и /32 сохранены —
                # клиент-сторона WG-конфига и так корректна, ansible
                # должен лишь привести iface/peer-state на нодах.
                task = orchestrator.create_task(
                    "relay_tunnel",
                    relay.id,
                    "apply",
                    {"exit_id": exit_node.id, "link_id": existing.id},
                    batch_id=batch_id,
                )
                created_entries.append(("reapplied", existing, exit_node, task))
                continue

            # Новый attach: alloc + keygen + INSERT + task.
            client_address = allocate_client_address(db, exit_node)
            iface_name = next_wg_interface_name(db, relay.id)
            pub, priv = generate_wireguard_keypair()
            relay_config_snapshot = build_relay_config(
                exit_node=exit_node,
                client_private_key=priv,
                client_address_v4=client_address,
            )
            link = models.RelayExitLink(
                relay_node_id=relay.id,
                exit_id=exit_node.id,
                wg_interface_name=iface_name,
                wg_client_private_key_enc=_encrypt(priv),
                wg_client_public_key=pub,
                wg_client_address_v4=client_address,
            )
            db.add(link)
            db.flush()
            task = orchestrator.create_task(
                "relay_tunnel",
                relay.id,
                "apply",
                {"exit_id": exit_node.id, "link_id": link.id},
                batch_id=batch_id,
            )
            created_entries.append(("attached", link, exit_node, task))
    except RelayAllocationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        # UNIQUE на (relay_id, exit_id) — race с single attach из другого
        # запроса между нашим existing-check'ом и flush'ем. uq_relay_exit_pair
        # migration 0031.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Batch attach race на уникальном индексе: {exc.orig}",
        ) from exc

    if relay_config_snapshot is not None:
        # relay_config — флажок «это relay» для admin badge'а; ansible
        # его не читает (см. attach_relay комментарий). Обновляем
        # только если был хоть один новый attach — для чистого re-apply
        # batch'а флаг уже стоит.
        relay.relay_config = relay_config_snapshot

    db.commit()

    actor, actor_type = _resolve_admin_actor(admin_actor)
    for mode, link, _exit, _task in created_entries:
        if mode == "attached":
            _audit(
                db, actor, "relay_exit_attached", "relay_exit_link", link.id,
                actor_type=actor_type,
            )
    _audit(
        db, actor, "relay_batch_attach", "relay_node", relay.id,
        actor_type=actor_type,
        metadata={
            "batch_id": str(batch_id),
            "exit_ids": payload.exit_ids,
            "attached": [
                e.id for m, _l, e, _t in created_entries if m == "attached"
            ],
            "reapplied": [
                e.id for m, _l, e, _t in created_entries if m == "reapplied"
            ],
        },
    )

    # Enqueue после commit'а — worker по job_id подбирает row из БД;
    # если задача ещё не committed — `_execute_task` не найдёт её и
    # запишет fail. При недоступности Redis enqueue падает, но task'и
    # остаются в pending — pending-rescue-tick подберёт через
    # PENDING_RESCUE_INTERVAL секунд.
    for _mode, _link, _exit, task in created_entries:
        try:
            orchestrator.run_task_async(task)
        except Exception:  # noqa: BLE001
            logger.exception(
                "run_task_async failed for batch %s task %s — "
                "pending-rescue-tick should pick it up",
                batch_id, task.id,
            )

    out_links = [
        schemas.BatchAttachLinkOut(
            exit_id=exit_node.id,
            exit_name=exit_node.name,
            link_id=link.id,
            task_id=task.id,
            wg_interface_name=link.wg_interface_name,
            wg_client_address_v4=link.wg_client_address_v4,
            mode=mode,
        )
        for mode, link, exit_node, task in created_entries
    ]
    return schemas.BatchAttachRelayResponse(
        batch_id=batch_id,
        relay_node_id=relay.id,
        relay_node_name=relay.name,
        links=out_links,
    )


def _detach_link_core(
    db: Session,
    link: models.RelayExitLink,
) -> tuple[models.VPNNode | None, int, dict[str, Any]]:
    """Ядро отцепления одного relay↔exit link'а — БЕЗ commit'а и task'а.

    Перепинивает осиротевшие creds (live с ``node_id == relay`` и
    ``exit_id == этот exit``): если у релея остаются другие линки —
    раскладывает их least-loaded по оставшимся exit'ам (в памяти, чтобы
    распределить пачку равномерно, а не свалить всех на наименее
    загруженный); если линков больше нет — обнуляет ``exit_id`` в NULL
    (релей становится direct-нодой), иначе ``build_xray_relay_outbounds``
    видел бы stale exit_id и выкидывал email'ы из routing rules → юзер
    ушёл бы на default outbound без sockopt (прямой egress из РФ). Затем
    удаляет link и пересобирает ``relay_config``-флаг (NULL если это был
    последний линк, иначе snapshot с любого оставшегося).

    Возвращает ``(relay, link_id, migration_summary)``. Вынесено из
    ``detach_relay``, чтобы ``batch_detach_relay`` повторял ту же логику
    per-relay без дублирования. Commit, audit и создание ``relay_tunnel``
    teardown-task'а — на стороне вызывающего.
    """
    exit_id = link.exit_id
    relay_node_id = link.relay_node_id
    link_id = link.id
    relay = db.get(models.VPNNode, relay_node_id)

    migration_summary: dict[str, Any] = {"migrated": 0, "cleared": 0}
    orphans = (
        db.query(models.Credential)
        .filter(
            models.Credential.node_id == relay_node_id,
            models.Credential.exit_id == exit_id,
            models.Credential.pool_state != models.CredentialPoolState.revoked,
        )
        .all()
    )
    if orphans:
        remaining_links = (
            db.query(models.RelayExitLink)
            .filter(
                models.RelayExitLink.relay_node_id == relay_node_id,
                models.RelayExitLink.id != link.id,
            )
            .all()
        )
        if remaining_links:
            orphan_ids = {c.id for c in orphans}
            counts: dict[int, int] = {rl.exit_id: 0 for rl in remaining_links}
            rows = (
                db.query(
                    models.Credential.exit_id,
                    func.count(models.Credential.id),
                )
                .filter(
                    models.Credential.node_id == relay_node_id,
                    models.Credential.exit_id.isnot(None),
                    models.Credential.pool_state
                    != models.CredentialPoolState.revoked,
                    ~models.Credential.id.in_(orphan_ids),
                )
                .group_by(models.Credential.exit_id)
                .all()
            )
            for ex_id, cnt in rows:
                if ex_id in counts:
                    counts[ex_id] = cnt
            # Round-robin по heap: (count, exit_id) — при равенстве
            # exit_id с меньшим id выигрывает (детерминированно между
            # запусками).
            heap: list[tuple[int, int]] = [
                (cnt, ex) for ex, cnt in counts.items()
            ]
            heapq.heapify(heap)
            buckets: dict[int, list[int]] = {}
            for cred in orphans:
                cnt, ex = heapq.heappop(heap)
                buckets.setdefault(ex, []).append(cred.id)
                heapq.heappush(heap, (cnt + 1, ex))
            for new_ex, cred_ids in buckets.items():
                db.query(models.Credential).filter(
                    models.Credential.id.in_(cred_ids)
                ).update(
                    {models.Credential.exit_id: new_ex},
                    synchronize_session=False,
                )
            migration_summary = {
                "migrated": len(orphans),
                "distribution": {
                    str(ex): len(ids) for ex, ids in buckets.items()
                },
            }
        else:
            db.query(models.Credential).filter(
                models.Credential.id.in_([c.id for c in orphans])
            ).update(
                {models.Credential.exit_id: None},
                synchronize_session=False,
            )
            migration_summary = {"cleared": len(orphans)}

    db.delete(link)
    db.flush()
    if relay:
        # G.5 — ``relay_config`` is now just the "is this a relay?"
        # signal for the admin schema. Clear it only when the last
        # link is gone; otherwise keep any remaining link's data as
        # the snapshot so ``has_relay_config`` still reports True.
        remaining = (
            db.query(models.RelayExitLink)
            .filter(models.RelayExitLink.relay_node_id == relay.id)
            .order_by(models.RelayExitLink.id)
            .first()
        )
        if remaining is None:
            relay.relay_config = None
        else:
            remaining_exit = db.get(models.WGExitNode, remaining.exit_id)
            if remaining_exit is not None:
                relay.relay_config = build_relay_config(
                    exit_node=remaining_exit,
                    client_private_key=decrypt(
                        remaining.wg_client_private_key_enc
                    ),
                    client_address_v4=remaining.wg_client_address_v4,
                )
    return relay, link_id, migration_summary


@router.delete("/exits/{exit_id}/links/{relay_node_id}", status_code=200)
def detach_relay(
    exit_id: int,
    relay_node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Detach a relay from this exit (G.5: multi-exit aware).

    Drops the single matching link row. If the relay has other
    remaining links, ``relay_config`` is re-snapshotted from one of
    them so the admin schema still flags this node as a relay; if
    this was the last link, ``relay_config`` is cleared. Either way
    a ``relay_tunnel`` task is scheduled — ansible re-renders the
    exit's peer list without this client (``bootstrap_exit.yml`` +
    ``wg syncconf``) and re-applies the relay's WG tunnel set from
    ``relay_wg_links``: the removed ``wgN`` gets torn down by the
    drift-reconciliation step in the ``relay_jump_node`` role, any
    remaining ``wgN`` stays up.

    Перед удалением линка все live creds с ``exit_id == exit_id`` и
    ``node_id == relay.id`` перепиниваются: если остаются другие
    линки — распределяются least-loaded по ним; если линков больше
    нет — ``exit_id`` обнуляется в NULL. Без этого шага
    ``build_xray_relay_outbounds`` выкидывает emails со stale
    ``exit_id`` из routing rules и юзер идёт по default outbound
    (direct), а не через оставшийся туннель.

    Returns ``task_id`` so the admin UI can surface progress in
    /tasks, plus ``credentials`` summary (migrated count +
    распределение по exit'ам). ``task_id`` is ``None`` only if the
    relay row was already gone (rare — FK cascade order drops the
    link first).
    """
    link = (
        db.query(models.RelayExitLink)
        .filter(
            models.RelayExitLink.exit_id == exit_id,
            models.RelayExitLink.relay_node_id == relay_node_id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")

    relay, link_id, migration_summary = _detach_link_core(db, link)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "relay_exit_detached", "relay_exit_link", link_id,
        actor_type=actor_type,
        metadata={
            "exit_id": exit_id,
            "relay_node_id": relay_node_id,
            "credentials": migration_summary,
        },
    )

    # Stage E — teardown task. Runs bootstrap_exit.yml on the exit
    # (peer list no longer includes this relay) and relay_tunnel_apply.yml
    # on the relay; the role sees empty relay_wg_* vars and tears wg0
    # down + unpatches Xray. If the relay row is gone (rare — FK cascades
    # drop the link first), skip — there's no target to reconfigure.
    task_id: int | None = None
    if relay is not None:
        orchestrator = ProvisioningOrchestrator(db)
        task = orchestrator.create_task(
            "relay_tunnel",
            relay.id,
            "apply",
            {"exit_id": exit_id, "link_id": link_id, "detach": True},
        )
        db.commit()
        task_id = task.id
        orchestrator.run_task_async(task)

    return {
        "exit_id": exit_id,
        "relay_node_id": relay_node_id,
        "deleted": True,
        # task_id lets the admin UI link to /tasks?id=N so the admin
        # sees the ansible run instead of wondering if anything happened.
        "task_id": task_id,
        # Сколько осиротевших creds переписали и как распределили —
        # админка показывает это в alert'е после detach'а.
        "credentials": migration_summary,
    }


@router.post(
    "/exits/{exit_id}/batch-detach",
    response_model=schemas.BatchDetachRelayResponse,
)
def batch_detach_relay(
    exit_id: int,
    payload: schemas.BatchDetachRelayRequest,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Отцепить несколько relay-нод от одного exit'а одним запросом.

    Обратная операция к batch-attach (там один relay → N exit'ов, тут
    один exit → N relay'ев). Для каждого relay повторяет логику
    ``detach_relay`` через общий хелпер ``_detach_link_core`` (перепин
    осиротевших creds → удаление link'а → пересборка relay_config-флага)
    и ставит ``relay_tunnel`` teardown-task. Все task'и идут под общим
    ``batch_id`` — UI рендерит прогресс N/M тем же drawer'ом, что и
    batch-attach, retry отдельных upal'нувших через /tasks.

    Best-effort по составу: relay_node_id без линка к этому exit'у — не
    ошибка, попадает в ``not_found`` ответа (UI мог показывать stale
    строку). Каждый relay независим (его линки/creds scoped по
    ``node_id``), поэтому порядок обработки на результат не влияет.
    """
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")

    if not payload.relay_node_ids:
        raise HTTPException(
            status_code=400, detail="relay_node_ids must be non-empty"
        )
    if len(set(payload.relay_node_ids)) != len(payload.relay_node_ids):
        raise HTTPException(
            status_code=400,
            detail="relay_node_ids содержит дубликаты — почистите список",
        )

    links_by_relay: dict[int, models.RelayExitLink] = {
        row.relay_node_id: row
        for row in (
            db.query(models.RelayExitLink)
            .filter(
                models.RelayExitLink.exit_id == exit_id,
                models.RelayExitLink.relay_node_id.in_(payload.relay_node_ids),
            )
            .all()
        )
    }
    not_found = [
        rid for rid in payload.relay_node_ids if rid not in links_by_relay
    ]
    if not links_by_relay:
        raise HTTPException(
            status_code=404,
            detail="Ни один из выбранных relay не прицеплен к этому exit'у",
        )

    batch_id = uuid.uuid4()
    orchestrator = ProvisioningOrchestrator(db)
    # (relay_id, relay_name, link_id, migration_summary, task|None).
    # Сохраняем порядок payload.relay_node_ids для стабильного ответа.
    created_entries: list[
        tuple[int, str, int, dict[str, Any], models.ProvisioningTask | None]
    ] = []

    try:
        for relay_id in payload.relay_node_ids:
            link = links_by_relay.get(relay_id)
            if link is None:
                continue  # уже учтён в not_found
            relay, link_id, migration_summary = _detach_link_core(db, link)
            relay_name = relay.name if relay is not None else f"#{relay_id}"
            task = None
            if relay is not None:
                task = orchestrator.create_task(
                    "relay_tunnel",
                    relay.id,
                    "apply",
                    {"exit_id": exit_id, "link_id": link_id, "detach": True},
                    batch_id=batch_id,
                )
            created_entries.append(
                (relay_id, relay_name, link_id, migration_summary, task)
            )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Batch detach race на FK/уникальном индексе: {exc.orig}",
        ) from exc

    db.commit()

    actor, actor_type = _resolve_admin_actor(admin_actor)
    for relay_id, _name, link_id, summary, _task in created_entries:
        _audit(
            db, actor, "relay_exit_detached", "relay_exit_link", link_id,
            actor_type=actor_type,
            metadata={
                "exit_id": exit_id,
                "relay_node_id": relay_id,
                # Паритет с одиночным detach: пишем summary перепина creds.
                "credentials": summary,
            },
        )
    _audit(
        db, actor, "relay_batch_detach", "wg_exit_node", exit_id,
        actor_type=actor_type,
        metadata={
            "batch_id": str(batch_id),
            "relay_node_ids": payload.relay_node_ids,
            "detached": [rid for rid, *_ in created_entries],
            "not_found": not_found,
        },
    )

    # Enqueue после commit'а — worker по job_id подбирает row из БД; см.
    # развёрнутый комментарий в batch_attach_relay. Redis-fail → task'и
    # в pending, pending-rescue-tick их подберёт.
    for _rid, _name, _lid, _summary, task in created_entries:
        if task is None:
            continue
        try:
            orchestrator.run_task_async(task)
        except Exception:  # noqa: BLE001
            logger.exception(
                "run_task_async failed for batch %s task %s — "
                "pending-rescue-tick should pick it up",
                batch_id, task.id,
            )

    out_links = [
        schemas.BatchDetachLinkOut(
            relay_node_id=relay_id,
            relay_node_name=relay_name,
            link_id=link_id,
            task_id=task.id if task is not None else None,
            credentials=migration_summary,
        )
        for relay_id, relay_name, link_id, migration_summary, task
        in created_entries
    ]
    return schemas.BatchDetachRelayResponse(
        batch_id=batch_id,
        exit_id=exit_id,
        exit_name=exit_node.name,
        links=out_links,
        not_found=not_found,
    )


@router.post("/exits/{exit_id}/links/{relay_node_id}/reconnect", status_code=200)
def reconnect_relay_link(
    exit_id: int,
    relay_node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Перезапустить relay_tunnel ансибл для существующего (relay, exit).

    Link в БД не трогаем — только создаём новую таску с тем же
    payload, что и attach: bootstrap_exit.yml на exit + relay_tunnel_apply.yml
    на relay. Полезно, когда исходный attach упал на handshake'е
    (curl rc=28) и нужно просто прогнать ансибл заново без
    detach/attach-цикла.
    """
    link = (
        db.query(models.RelayExitLink)
        .filter(
            models.RelayExitLink.exit_id == exit_id,
            models.RelayExitLink.relay_node_id == relay_node_id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")

    relay = db.get(models.VPNNode, relay_node_id)
    if relay is None:
        raise HTTPException(status_code=404, detail="Relay node not found")

    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task(
        "relay_tunnel",
        relay.id,
        "apply",
        {"exit_id": exit_id, "link_id": link.id},
    )
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "relay_exit_reconnect", "relay_exit_link", link.id,
        actor_type=actor_type,
    )
    db.commit()
    orchestrator.run_task_async(task)
    return {
        "exit_id": exit_id,
        "relay_node_id": relay_node_id,
        "task_id": task.id,
    }


class DiagnoseLinkRequest(BaseModel):
    """Body для POST /exits/links/{link_id}/diagnose.

    `check_types` опционален — если пуст/отсутствует, оркестратор
    подставит `DEFAULT_DIAGNOSE_CHECKS` (все 6 jump-side checks).
    `xray_port` тоже опционален; по умолчанию 9443 (Reality).
    """

    check_types: list[str] | None = None
    xray_port: int | None = None


@router.post("/exits/links/{link_id}/diagnose", status_code=200)
def diagnose_relay_link(
    link_id: int,
    body: DiagnoseLinkRequest | None = None,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Read-only диагностика конкретного relay→exit WG-линка.

    Создаёт ProvisioningTask с target_type='relay_tunnel', target_id=
    relay.id, action='diagnose'. На worker'е оркестратор зовёт
    `playbooks/diagnose_relay_link.yml`, парсит структурированный JSON
    и кладёт `checks: [{name,status,latency_ms,message,details}, ...]`
    в `task.result` рядом со stdout/stderr/rc. UI рендерит `checks`
    карточками — raw stdout уходит в collapsible details.

    Безопасно: `_handle_task_outcome` для relay_tunnel — no-op, поэтому
    диагностика никогда не флипает статусы и не дёргает credentials.
    """
    link = db.get(models.RelayExitLink, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    relay = db.get(models.VPNNode, link.relay_node_id)
    if relay is None:
        raise HTTPException(status_code=404, detail="Relay node not found")

    task_payload: dict[str, Any] = {
        "link_id": link.id,
        "exit_id": link.exit_id,
    }
    if body is not None:
        if body.check_types:
            task_payload["check_types"] = list(body.check_types)
        if body.xray_port is not None:
            task_payload["xray_port"] = int(body.xray_port)

    orchestrator = ProvisioningOrchestrator(db)
    task = orchestrator.create_task(
        "relay_tunnel", relay.id, "diagnose", task_payload,
    )
    db.commit()
    orchestrator.run_task_async(task)
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "relay_link_diagnose", "relay_exit_link", link.id,
        actor_type=actor_type,
        metadata={
            "task_id": task.id,
            "check_types": task_payload.get("check_types"),
        },
    )
    return {
        "link_id": link.id,
        "relay_node_id": relay.id,
        "exit_id": link.exit_id,
        "task_id": task.id,
    }


@router.post("/exits/links/health/refresh", status_code=200)
def refresh_relay_link_health(
    admin_token: str = Depends(require_admin),
):
    """Форс-прогон relay_link_health тика прямо сейчас.

    Если после деплоя колонка ``last_observed_at`` на всех линках NULL
    («тик ещё не прошёл»), админка зовёт эту ручку вместо того, чтобы
    ждать до 5 минут. Enqueue'ится тот же ``run_relay_link_health_tick``,
    который воркер обычно гоняет периодически: он сам SSH'ит на все
    relay, читает ``wg show all dump`` и апдейтит health-колонки,
    а в конце перепланирует себя через RELAY_LINK_HEALTH_INTERVAL —
    то есть одним нажатием восстанавливается и периодичность, если
    бутстрап-шаг воркера почему-то не сработал при старте.

    SSH-ключ смонтирован только в worker-контейнер (см. docker-compose
    volume для /run/secrets/provisioning_key), поэтому тут мы не
    запускаем коллектор inline — только enqueue через RQ.

    Реюзаем deterministic ``tick-relay-link-health`` job_id (тот же, что
    ставит периодический self-reschedule), чтобы серии кликов админки не
    порождали каскад копий в очереди. Если тик уже крутится — возвращаем
    его id без нового enqueue; если стоит в ``scheduled`` или ``queued``
    — снимаем и кладём заново без delay, чтобы воркер подхватил прямо
    сейчас, а не ждал следующего RELAY_LINK_HEALTH_INTERVAL.

    Response:
      * ``enqueued=true`` + ``job_id`` — jobs передан воркеру, смотри
        результат обновлением списка links через 10–20 секунд;
      * ``enqueued=false`` — очередь недоступна (Redis down), тогда
        индикаторы не обновятся пока не поднимется очередь.
    """
    from ..queue import RESULT_TTL, TICK_IDS, get_queue
    from rq.exceptions import NoSuchJobError
    from rq.job import Job
    from rq.registry import StartedJobRegistry

    queue = get_queue()
    if queue is None:
        return {"enqueued": False, "reason": "queue unavailable"}

    tick_id = TICK_IDS["app.worker.run_relay_link_health_tick"]

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
        "app.worker.run_relay_link_health_tick",
        job_id=tick_id,
        result_ttl=RESULT_TTL,
    )
    return {"enqueued": True, "job_id": job.id}


@router.post(
    "/exits/{from_id}/evacuate-to/{to_id}",
    response_model=schemas.ExitEvacuateOut,
)
def evacuate_exit_to(
    from_id: int,
    to_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Переселить все активные подписки с exit A на exit B.

    Аналог ``POST /nodes/{from}/migrate-to/{to}``, но на уровне exit:
    сами сабы остаются на своих relay-нодах, меняется только outbound —
    ``Credential.exit_id`` с ``from_id`` на ``to_id`` и relay_tunnel_apply
    на затронутых relay перегенерирует routing rules в xray. Используется
    когда foreign exit A залочили / он умер / уезжаем на другого провайдера:
    один клик вместо ручного per-sub switch-exit на десятки подписок.

    **Prerequisite**: target exit должен быть уже прикреплён к relay'ям,
    на которых живут evacuated-сабы (``RelayExitLink`` exists). Для
    relay'ев без линка на ``to_id`` все их сабы попадают в ``failed`` с
    причиной «target not attached» — админ сам решает прикрепить или
    скипнуть. Релей попадает в ``failed_relays``.

    **Батчинг**: все creds сабов на одном relay обновляются одним UPDATE,
    и генерируется ОДИН ``relay_tunnel apply`` task per relay — reconcile_xray
    в роли перечитывает authoritative emails_by_iface из БД и регенерит
    каждый ``config*.json``, так что разницы между «перещёлкнули 1 саб» и
    «перещёлкнули 50» для него нет. Это заметно дешевле N вызовов
    ``switch_subscription_exit`` подряд (N apply-тасков через семафор).

    Non-goals: creds на других exit'ах у сабов НЕ трогаются. Если у саба
    была cred на from_id — ок, переедет; если на другом exit — остаётся
    как было. Смешанных sub'ов быть не должно (switch_subscription_exit
    выше переключает все creds разом), но на всякий случай UPDATE фильтрует
    по ``exit_id = from_id`` явно.
    """
    if from_id == to_id:
        raise HTTPException(status_code=400, detail="from_id and to_id must differ")
    from_exit = db.get(models.WGExitNode, from_id)
    if not from_exit:
        raise HTTPException(status_code=404, detail="Source exit not found")
    to_exit = db.get(models.WGExitNode, to_id)
    if not to_exit:
        raise HTTPException(status_code=404, detail="Target exit not found")
    if not to_exit.is_active:
        raise HTTPException(status_code=400, detail="Target exit is not active")

    subs: list[models.Subscription] = (
        db.query(models.Subscription)
        .join(
            models.Credential,
            models.Credential.subscription_id == models.Subscription.id,
        )
        .filter(
            models.Subscription.status == models.SubscriptionStatus.active,
            models.Credential.exit_id == from_id,
            models.Credential.is_active.is_(True),
        )
        .distinct()
        .all()
    )
    considered = len(subs)

    # Group by relay — one apply per relay regardless of sub count.
    subs_by_relay: dict[int, list[models.Subscription]] = {}
    subs_without_relay: list[models.Subscription] = []
    for sub in subs:
        if sub.node_id is None:
            subs_without_relay.append(sub)
            continue
        subs_by_relay.setdefault(sub.node_id, []).append(sub)

    migrated: list[int] = []
    failed: list[schemas.NodeBulkMigrateFailure] = []
    task_ids: list[int] = []
    failed_relays: list[int] = []

    for sub in subs_without_relay:
        failed.append(
            schemas.NodeBulkMigrateFailure(
                subscription_id=sub.id, error="subscription has no relay node"
            )
        )

    orchestrator = ProvisioningOrchestrator(db)
    for relay_id, relay_subs in subs_by_relay.items():
        link = (
            db.query(models.RelayExitLink)
            .filter(
                models.RelayExitLink.relay_node_id == relay_id,
                models.RelayExitLink.exit_id == to_id,
            )
            .first()
        )
        if link is None:
            failed_relays.append(relay_id)
            for sub in relay_subs:
                failed.append(
                    schemas.NodeBulkMigrateFailure(
                        subscription_id=sub.id,
                        error=f"target exit {to_id} not attached to relay {relay_id}",
                    )
                )
            continue

        sub_ids = [s.id for s in relay_subs]
        # Обновляем только creds указывающие на from-exit — не трогаем
        # соседние exit'ы у смешанных сабов (защита, хотя обычно их нет).
        db.query(models.Credential).filter(
            models.Credential.subscription_id.in_(sub_ids),
            models.Credential.exit_id == from_id,
        ).update(
            {models.Credential.exit_id: to_id},
            synchronize_session=False,
        )
        db.flush()

        task = orchestrator.create_task(
            "relay_tunnel",
            relay_id,
            "apply",
            {
                "exit_id": to_id,
                "evacuated_from_exit_id": from_id,
                "evacuated_subscription_ids": sub_ids,
                "new_interface": link.wg_interface_name,
            },
        )
        db.commit()
        orchestrator.run_task_async(task)
        task_ids.append(task.id)
        migrated.extend(sub_ids)

    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db,
        actor,
        "exit_bulk_evacuated",
        "wg_exit_node",
        from_exit.id,
        actor_type=actor_type,
        metadata={
            "from_exit_id": from_exit.id,
            "from_exit_name": from_exit.name,
            "to_exit_id": to_exit.id,
            "to_exit_name": to_exit.name,
            "considered_count": considered,
            "migrated_count": len(migrated),
            "failed_count": len(failed),
            "migrated_subscription_ids": migrated,
            "failed_subscription_ids": [f.subscription_id for f in failed],
            "failed_relays": failed_relays,
            "task_ids": task_ids,
        },
    )
    db.commit()

    return schemas.ExitEvacuateOut(
        from_exit_id=from_exit.id,
        to_exit_id=to_exit.id,
        considered_count=considered,
        migrated=migrated,
        failed=failed,
        task_ids=task_ids,
        failed_relays=failed_relays,
    )
