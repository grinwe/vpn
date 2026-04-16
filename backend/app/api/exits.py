"""WireGuard exit-node admin endpoints: ``/api/exits/*``.

CRUD for foreign exit nodes that RU relay jump nodes tunnel to. See
``docs/RELAY_ROADMAP.md`` stage B for the architecture.

Private keys are stored encrypted via ``security.encrypt`` (same Fernet
scheme as cloud tokens). Responses expose ``wg_public_key`` but never
the private half — only ``has_private_key: bool`` to let the UI tell
whether keygen is needed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin
from ..security import encrypt as _encrypt
from ..services.provisioning import ProvisioningOrchestrator
from ..services.relay import (
    RelayAllocationError,
    allocate_client_address,
    build_relay_config,
    validate_requested_address,
)
from ..services.vless import generate_wireguard_keypair
from ._common import ADMIN_ACTOR_HEADER, _audit, _resolve_admin_actor, get_db

router = APIRouter()


def _link_to_out(link: models.RelayExitLink) -> schemas.RelayExitLinkOut:
    return schemas.RelayExitLinkOut(
        id=link.id,
        relay_node_id=link.relay_node_id,
        relay_node_name=link.relay_node.name if link.relay_node else "",
        exit_id=link.exit_id,
        exit_name=link.exit_node.name if link.exit_node else "",
        wg_client_public_key=link.wg_client_public_key,
        wg_client_address_v4=link.wg_client_address_v4,
        created_at=link.created_at,
    )


def _to_out(
    exit_node: models.WGExitNode, *, peers_count: int = 0
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
    counts: dict[int, int] = dict(
        db.query(
            models.RelayExitLink.exit_id,
            func.count(models.RelayExitLink.id),
        )
        .group_by(models.RelayExitLink.exit_id)
        .all()
    )
    return [_to_out(r, peers_count=counts.get(r.id, 0)) for r in rows]


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
    return [_link_to_out(r) for r in rows]


@router.post("/exits/{exit_id}/links", response_model=schemas.RelayExitLinkOut)
def attach_relay(
    exit_id: int,
    payload: schemas.RelayExitLinkCreate,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Attach a relay VPN node to this exit.

    Generates a fresh WG client keypair, allocates a free /32 in the
    exit's subnet, writes the link row, and populates the relay node's
    ``relay_config`` in a single transaction, then schedules a
    ``relay_tunnel`` task on the worker that re-renders the exit's peer
    list and brings wg0 up on the relay (stage E).
    """
    exit_node = db.get(models.WGExitNode, exit_id)
    if not exit_node:
        raise HTTPException(status_code=404, detail="Exit node not found")
    if not exit_node.is_active:
        raise HTTPException(status_code=400, detail="Exit is not active")

    relay = db.get(models.VPNNode, payload.relay_node_id)
    if not relay:
        raise HTTPException(status_code=404, detail="Relay node not found")

    existing = (
        db.query(models.RelayExitLink)
        .filter(models.RelayExitLink.relay_node_id == relay.id)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Relay {relay.name} is already attached to exit "
                f"{existing.exit_id}; detach first"
            ),
        )

    try:
        if payload.wg_client_address_v4:
            client_address = validate_requested_address(
                db, exit_node, payload.wg_client_address_v4
            )
        else:
            client_address = allocate_client_address(db, exit_node)
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
        wg_client_private_key_enc=_encrypt(priv),
        wg_client_public_key=pub,
        wg_client_address_v4=client_address,
    )
    db.add(link)
    relay.relay_config = relay_config
    db.commit()
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


@router.delete("/exits/{exit_id}/links/{relay_node_id}", status_code=200)
def detach_relay(
    exit_id: int,
    relay_node_id: int,
    db: Session = Depends(get_db),
    admin_token: str = Depends(require_admin),
    admin_actor: str | None = Header(default=None, alias=ADMIN_ACTOR_HEADER),
):
    """Detach a relay from this exit.

    Drops the link row and clears the relay's ``relay_config``, then
    schedules a ``relay_tunnel`` teardown task (re-renders the exit's
    peer list without this client via ``bootstrap_exit.yml`` +
    ``wg syncconf``, and brings wg0 down + un-patches Xray on the relay
    via ``relay_tunnel_apply.yml`` — the ``relay_jump_node`` role reads
    the empty ``relay_wg_*`` vars and takes the teardown branch).

    Returns ``task_id`` so the admin UI can surface progress in
    /tasks. It is ``None`` only if the relay row was already gone
    (rare — FK cascade order drops the link first).
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

    link_id = link.id
    relay = db.get(models.VPNNode, relay_node_id)
    if relay:
        relay.relay_config = None

    db.delete(link)
    db.commit()
    actor, actor_type = _resolve_admin_actor(admin_actor)
    _audit(
        db, actor, "relay_exit_detached", "relay_exit_link", link_id,
        actor_type=actor_type,
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
    }
