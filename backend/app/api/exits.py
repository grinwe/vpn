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
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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


def _link_to_out(link: models.RelayExitLink) -> schemas.RelayExitLinkOut:
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
    return [_link_to_out(r) for r in rows]


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

    link_id = link.id
    relay = db.get(models.VPNNode, relay_node_id)

    # ── Авто-миграция осиротевших creds ──────────────────────────────
    # До удаления линка фиксируем, кого надо переносить: каждый живой
    # cred (``pool_state != revoked``) с ``node_id == relay.id`` и
    # ``exit_id == exit_id`` указывает на уже отрезаемый exit.
    # Если у релея остаются другие линки — раскладываем эти creds по
    # оставшимся exit'ам least-loaded (в памяти, чтобы распределить
    # пачку равномерно, а не свалить всех на один наименее
    # загруженный). Если линков больше нет — релей де-факто становится
    # direct-нодой, чистим ``exit_id`` в NULL, иначе
    # ``build_xray_relay_outbounds`` будет видеть stale exit_id и
    # исключать email'ы из routing rules → юзер попадёт на default
    # outbound без sockopt (= прямой egress из РФ). Ранее этот шаг не
    # делался вообще — creds жили с указателем на удалённый линк до
    # следующего ручного switch-exit или миграции.
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
