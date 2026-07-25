"""Warm credential pool (roadmap stage 2.5).

A "warm bundle" is a set of :class:`Credential` rows that share
``(node_id, access_username)`` with ``pool_state=warm`` and
``subscription_id=NULL``. Each bundle represents a fully-provisioned
identity already pushed to the node by Ansible — buying a subscription
means flipping it to ``assigned`` and binding it to a fresh
``Subscription`` + ``Device`` in a single transaction. Ansible is **not**
in the hot path: that's the entire point.

The warmer is a periodic background job (see ``app.worker``) that keeps
each active node's pool at ``WARM_POOL_TARGET`` bundles. When a purchase
hits an empty pool we fall back to cold provisioning (the old path),
increment ``vpn_warm_pool_misses_total`` and let the warmer catch up.

Revoke is split in two stages so user-facing API calls never block on
Ansible:

  1. ``unassign_bundle`` — flips ``assigned → revoked`` in DB only.
     The user's access stops working at xray's next config reload, but
     that's deferred to the worker.
  2. ``physical_revoke_credential_bundle`` — runs Ansible ``state=absent``
     for the bundle and deletes the rows. Driven by the periodic sweep
     ``run_warm_pool_revoke_sweep`` (see ``app.worker``), which picks up
     every ``revoked`` bundle — both user-unassigned ones and those left
     by ``invalidate_node_warm_pool`` — and physically removes them from
     the node. Without that sweep revoked identities pile up forever both
     in xray's config and as ``revoked`` rows in the DB.

The atomic assignment uses ``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1``
over the *anchor rows* of the warm pool — the lowest-id row of each
bundle (``MIN(id) GROUP BY access_username``). Restricting the candidate
set to one row per bundle is load-bearing: sibling rows of one bundle
have adjacent ids, so a naive "lowest warm id" pick would let worker B
skip a locked anchor and grab a *sibling* of the same bundle as its own
anchor — then A waits on B's row and B waits on A's row: a deadlock.
Anchoring on the per-bundle minimum guarantees two workers always land on
two different bundles, so no cross-bundle lock wait can form.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import uuid
from typing import Any

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy.orm import Session

from .. import models
from ..security import encrypt
from ..time_utils import utcnow
from .ansible_runner import build_inventory_for_node, run_playbook
from .relay import choose_exit_for_relay, resolve_exit_interface

logger = logging.getLogger(__name__)

# How deep we want each node's warm pool. Tunable per environment;
# default 10 covers a small operator and a single concurrent burst of
# purchases without falling back to cold provisioning.
WARM_POOL_TARGET = int(os.getenv("WARM_POOL_TARGET", "10"))
# How many bundles the warmer is allowed to provision per node per tick.
# Capped to avoid hammering a freshly-added node with 50 ansible runs in
# a row. The pool catches up over a few ticks instead.
WARM_POOL_BATCH_PER_TICK = int(os.getenv("WARM_POOL_BATCH_PER_TICK", "3"))

# Master switch — set to "0" if the operator wants to bypass the pool
# entirely (e.g. for emergency debugging). Cold path stays alive forever.
WARM_POOL_ENABLED = os.getenv("WARM_POOL_ENABLED", "1").lower() not in {"0", "false", "no"}

# How many revoked bundles the physical-revoke sweep processes per tick.
# Same throttling rationale as WARM_POOL_BATCH_PER_TICK — one Ansible run
# per bundle, don't stampede a node.
WARM_POOL_REVOKE_BATCH_PER_TICK = int(os.getenv("WARM_POOL_REVOKE_BATCH_PER_TICK", "5"))
# After this many consecutive Ansible failures the sweep stops retrying a
# bundle and logs it for manual review, so a permanently-broken node
# (dead SSH, decommissioned box) doesn't get hammered every tick forever.
WARM_POOL_REVOKE_MAX_ATTEMPTS = int(os.getenv("WARM_POOL_REVOKE_MAX_ATTEMPTS", "5"))

# Process-local failure counter for the revoke sweep, keyed by
# (node_id, access_username). Best-effort back-off only: it resets on
# worker restart (a restart is exactly when a stuck node might recover, so
# retrying then is fine) and, like _warmer_semaphore, only constrains one
# worker process. A durable counter would need a Credential column — see
# the audit note; deliberately kept in-memory to avoid a schema change.
_revoke_attempts: dict[tuple[int, str], int] = {}
_revoke_attempts_lock = threading.Lock()

# Concurrency cap on warmer Ansible runs — same idea as the orchestrator
# semaphore, but separate so warming doesn't starve user-facing
# provisioning when both run at once.
# NOTE: process-local. If we ever scale workers horizontally, this cap
# only constrains *one* worker process; the effective parallelism becomes
# WARM_POOL_MAX_CONCURRENT × N_workers. Move to a Redis-backed limiter
# (or rely on RQ queue concurrency) before adding a second worker replica.
_warmer_semaphore = threading.Semaphore(int(os.getenv("WARM_POOL_MAX_CONCURRENT", "2")))


# ── Metrics ──────────────────────────────────────────────────────────

POOL_DEPTH = Gauge(
    "vpn_warm_pool_depth",
    "Number of warm credential bundles available per node",
    ["node"],
)
POOL_HITS = Counter(
    "vpn_warm_pool_hits_total",
    "Subscriptions provisioned from a warm bundle (no Ansible)",
    ["node"],
)
POOL_MISSES = Counter(
    "vpn_warm_pool_misses_total",
    "Subscriptions that fell back to cold provisioning (pool empty)",
    ["node"],
)
WARM_PROVISION_SECONDS = Histogram(
    "vpn_warm_credential_provision_seconds",
    "Wall-clock duration of a warm credential provisioning run",
    ["node"],
)


# ── Identity helpers ─────────────────────────────────────────────────

def _generate_warm_username(node: models.VPNNode) -> str:
    """Stable, collision-resistant username for a warm bundle.

    Format ``warm-<node_id>-<8 hex>`` keeps it greppable in xray logs and
    distinct from real user-bound names (``user-<user_id>-<sub_id>``).
    Real assignment does not rename — the warm name persists for the
    lifetime of the subscription, which is fine because we never expose
    it to the end user.
    """
    return f"warm-{node.id}-{secrets.token_hex(4)}"


# Imported lazily inside functions to avoid a provisioning ↔ warm_pool
# circular import on module load.

def _build_credential_text(
    node: models.VPNNode,
    cfg: models.VPNConfig,
    username: str,
    password: str,
    user_uuid: str,
) -> str | None:
    # NB: this dispatch MUST cover every protocol that the cold path
    # (provision_subscription / reprovision_subscription) can build,
    # otherwise the warm bundle comes out with holes and a later
    # try_assign_bundle hands the user an incomplete sub-link. The
    # missing vless_xhttp branch was exactly how xhttp-only users
    # ended up with empty configs after the warm path kicked in.
    from .provisioning import (
        _build_hysteria2_credential,
        _build_shadowtls_credential,
        _build_vless_reality_credential,
        _build_vless_ws_cdn_credential,
        _build_vless_xhttp_credential,
        _hy2_auth,
    )

    if cfg.protocol == models.VPNConfigProtocol.shadowtls_ss:
        return _build_shadowtls_credential(node, cfg, username, password)
    if cfg.protocol == models.VPNConfigProtocol.vless_reality:
        return _build_vless_reality_credential(node, cfg, user_uuid)
    if cfg.protocol == models.VPNConfigProtocol.vless_ws_cdn:
        return _build_vless_ws_cdn_credential(node, cfg, user_uuid)
    if cfg.protocol == models.VPNConfigProtocol.vless_xhttp:
        return _build_vless_xhttp_credential(node, cfg, user_uuid)
    if cfg.protocol == models.VPNConfigProtocol.hysteria2:
        # Пара username:password — нода на auth.type: userpass матчит именно её.
        return _build_hysteria2_credential(node, cfg, _hy2_auth(username, password))
    logger.warning("warm_pool: unsupported protocol %s on node %s", cfg.protocol, node.id)
    return None


# ── Warming (writes) ─────────────────────────────────────────────────

def _best_effort_remove_identity(
    node: models.VPNNode,
    username: str,
    protocols_payload: list[dict[str, Any]],
) -> None:
    """Компенсация провала warm-прогрева: снять identity с ноды.

    Если ``provision_device.yml`` со ``state=present`` упал (rc≠0) или
    бросил исключение, часть протоколов могла уже залиться на ноду. В БД
    строк ещё нет (их пишем только после успеха), поэтому убрать огрызок
    можно только тем же плейбуком со ``state=absent`` по тому же username.

    Best-effort: любые ошибки только логируем — БД не трогаем, вызывающий
    всё равно вернёт ``None``. Вызывать ТОЛЬКО после освобождения
    ``_warmer_semaphore`` (внутри снова его берём).
    """
    payload = {
        "username": username,
        "protocols": [
            {"proto": p["proto"], "port": p.get("port")} for p in protocols_payload
        ],
        "state": "absent",
        "reason": "warm_pool warm failure cleanup",
    }
    inventory = None
    _warmer_semaphore.acquire()
    try:
        inventory = build_inventory_for_node(node)
        run_playbook(
            "playbooks/provision_device.yml",
            inventory,
            limit=node.name,
            extra_vars=payload,
        )
        logger.info(
            "warm_pool: compensating absent for %s on node %s (warm failure cleanup)",
            username, node.id,
        )
    except Exception:  # noqa: BLE001
        # Компенсация — best-effort; если и она упала, огрызок подберёт
        # ручной разбор / следующий resync. Не эскалируем.
        logger.exception(
            "warm_pool: compensating absent failed for %s on node %s",
            username, node.id,
        )
    finally:
        _warmer_semaphore.release()
        if inventory is not None:
            try:
                inventory.unlink()
            except OSError:
                pass


def warm_one_bundle(db: Session, node: models.VPNNode) -> int | None:
    """Provision one warm credential bundle on ``node``.

    Generates a new identity, builds credential URIs for every enabled
    protocol, runs Ansible ``state=present`` to push the user onto the
    node, and stores the rows with ``pool_state=warm`` and
    ``subscription_id=NULL``. Returns the count of credentials created
    or ``None`` on failure (Ansible non-zero exit, no enabled configs,
    etc.) so the caller can decide whether to retry or back off.
    """
    enabled = [c for c in node.configs if c.is_enabled]
    if not enabled:
        logger.info("warm_pool: node %s has no enabled configs, skipping", node.id)
        return None

    username = _generate_warm_username(node)
    password = secrets.token_urlsafe(12)
    user_uuid = str(uuid.uuid4())

    # G.4: pick the least-loaded exit for this whole bundle so all
    # protocols of one identity share a single egress. ``None`` on
    # non-relay nodes — legacy single-exit semantics preserved.
    bundle_exit_id = choose_exit_for_relay(db, node)

    protocols_payload: list[dict[str, Any]] = []
    creds_to_insert: list[models.Credential] = []
    for cfg in enabled:
        cred_text = _build_credential_text(node, cfg, username, password, user_uuid)
        if cred_text is None:
            continue
        creds_to_insert.append(
            models.Credential(
                subscription_id=None,
                device_id=None,
                config_id=cfg.id,
                node_id=node.id,
                exit_id=bundle_exit_id,
                proto=cfg.protocol.value,
                config_text=encrypt(cred_text),
                access_username=username,
                pool_state=models.CredentialPoolState.warm,
                warmed_at=utcnow(),
                is_active=False,  # not "active" until assigned
            )
        )
        entry: dict[str, Any] = {"proto": cfg.protocol.value, "port": cfg.port}
        if cfg.protocol == models.VPNConfigProtocol.shadowtls_ss:
            entry["method"] = (cfg.settings or {}).get("method", "chacha20-ietf-poly1305")
        protocols_payload.append(entry)

    if not protocols_payload:
        logger.warning("warm_pool: nothing to provision for node %s", node.id)
        return None

    payload: dict[str, Any] = {
        "username": username,
        "uuid": user_uuid,
        "password": password,
        "protocols": protocols_payload,
        "state": "present",
    }
    # G.6: pin warm identity to its exit's xray outbound (multi-link
    # relays only). None on single-link/direct — default ``direct``
    # outbound already routes those through the primary wgN.
    exit_iface = resolve_exit_interface(db, node.id, bundle_exit_id)
    if exit_iface:
        payload["exit_interface"] = exit_iface

    inventory = None
    result = None
    ansible_error = False
    _warmer_semaphore.acquire()
    try:
        with WARM_PROVISION_SECONDS.labels(node=node.name).time():
            inventory = build_inventory_for_node(node)
            result = run_playbook(
                "playbooks/provision_device.yml",
                inventory,
                limit=node.name,
                extra_vars=payload,
            )
    except Exception:  # noqa: BLE001
        logger.exception("warm_pool: ansible run failed for node %s", node.id)
        ansible_error = True
    finally:
        _warmer_semaphore.release()
        if inventory is not None:
            try:
                inventory.unlink()
            except OSError:
                pass

    if ansible_error or result is None or result.returncode != 0:
        if result is not None and result.returncode != 0:
            logger.error(
                "warm_pool: ansible non-zero (%s) for node %s: %s",
                result.returncode, node.id, (result.stderr or "")[:300],
            )
        # Плейбук мог залить часть протоколов до падения. Строк в БД ещё
        # нет — но identity на ноде может уже быть, снимаем её best-effort,
        # иначе на флапающей ноде мусорные клиенты xray копятся с каждым
        # warm-тиком (username каждый раз новый, удалить их некому).
        _best_effort_remove_identity(node, username, protocols_payload)
        return None

    # Ansible succeeded — only NOW persist the rows. If we wrote them
    # before the run, an Ansible failure would leave dead "warm" entries
    # the assignment path would later hand to a paying user.
    for cred in creds_to_insert:
        db.add(cred)
    db.commit()

    POOL_DEPTH.labels(node=node.name).set(pool_depth(db, node.id))
    logger.info(
        "warm_pool: warmed bundle %s on node %s (%d protocols)",
        username, node.id, len(creds_to_insert),
    )
    return len(creds_to_insert)


def pool_depth(db: Session, node_id: int) -> int:
    """How many warm bundles are currently waiting on this node.

    A bundle = unique ``access_username`` on the node with at least one
    warm credential. Counted with ``COUNT(DISTINCT)`` because each bundle
    has N protocol rows but is one logical slot.
    """
    from sqlalchemy import distinct, func

    return (
        db.query(func.count(distinct(models.Credential.access_username)))
        .filter(models.Credential.node_id == node_id)
        .filter(models.Credential.pool_state == models.CredentialPoolState.warm)
        .scalar()
        or 0
    )


def ensure_pool(db: Session) -> dict[int, int]:
    """Top up every active node's warm pool to ``WARM_POOL_TARGET``.

    Called by the worker on a periodic schedule. Returns a
    ``{node_id: warmed_count}`` dict so the RQ result backend captures
    a useful audit trail per tick.
    """
    if not WARM_POOL_ENABLED:
        return {}

    nodes = (
        db.query(models.VPNNode)
        .filter(models.VPNNode.is_active.is_(True))
        .filter(models.VPNNode.status == models.VPNNodeStatus.active)
        .all()
    )

    summary: dict[int, int] = {}
    for node in nodes:
        depth = pool_depth(db, node.id)
        POOL_DEPTH.labels(node=node.name).set(depth)
        deficit = WARM_POOL_TARGET - depth
        if deficit <= 0:
            summary[node.id] = 0
            continue
        # Cap per-tick work so a freshly added node doesn't trigger 10
        # serialised ansible runs in one minute.
        to_warm = min(deficit, WARM_POOL_BATCH_PER_TICK)
        warmed = 0
        for _ in range(to_warm):
            res = warm_one_bundle(db, node)
            if res is None:
                # Stop early on failure — likely SSH/ansible env issue,
                # don't burn the rest of the budget on the same fault.
                break
            warmed += 1
        summary[node.id] = warmed
        POOL_DEPTH.labels(node=node.name).set(pool_depth(db, node.id))
    return summary


# ── Atomic assignment (the hot path) ─────────────────────────────────

def try_assign_bundle(
    db: Session, node_id: int, subscription_id: int
) -> list[models.Credential] | None:
    """Atomically pick a warm bundle on the node and bind it to a sub.

    Returns the list of credentials in the bundle on success, ``None``
    if the pool is empty. The caller is expected to commit the
    transaction; we only ``flush`` here so the caller can roll us back
    cleanly if its own follow-up work fails.

    Race-safety: locks one *anchor row per bundle* — the lowest id of
    each ``access_username`` group — with ``FOR UPDATE SKIP LOCKED``.
    Restricting candidates to per-bundle minimums is what prevents a
    deadlock: if we simply locked "the lowest warm id on the node", a
    second worker could SKIP LOCKED past a locked anchor and grab a
    *sibling* of that same bundle (adjacent id) as its own anchor, after
    which each worker would block on the other's row. Anchoring on the
    per-bundle minimum guarantees two workers always pick two distinct
    bundles. The siblings of the chosen anchor are then locked explicitly
    under that same transaction.
    """
    if not WARM_POOL_ENABLED:
        return None

    from sqlalchemy import func

    # Anchor candidates: the first (lowest-id) row of every warm bundle
    # on the node. Grouping by ``access_username`` collapses each bundle
    # to a single lockable row so parallel callers never contend on
    # sibling rows of the same bundle (see docstring — deadlock guard).
    anchor_ids = (
        db.query(func.min(models.Credential.id))
        .filter(models.Credential.node_id == node_id)
        .filter(models.Credential.pool_state == models.CredentialPoolState.warm)
        .group_by(models.Credential.access_username)
    )
    anchor = (
        db.query(models.Credential)
        .filter(models.Credential.id.in_(anchor_ids))
        .order_by(models.Credential.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if anchor is None or not anchor.access_username:
        return None

    # Lock the rest of the bundle. ``with_for_update`` here is a real
    # lock (not skip-locked) — we want to wait, briefly, if a parallel
    # worker is in the same transaction touching a row from this same
    # bundle. With the per-bundle anchor above two workers can never pick
    # the same bundle, but the explicit lock keeps the bundle consistent
    # even if a future refactor changes anchor selection.
    bundle = (
        db.query(models.Credential)
        .filter(models.Credential.node_id == node_id)
        .filter(models.Credential.access_username == anchor.access_username)
        .filter(models.Credential.pool_state == models.CredentialPoolState.warm)
        .with_for_update()
        .all()
    )
    if not bundle:
        return None

    now = utcnow()
    for cred in bundle:
        cred.pool_state = models.CredentialPoolState.assigned
        cred.subscription_id = subscription_id
        cred.assigned_at = now
        cred.is_active = True

    db.flush()

    # Find the node name for metrics — cheap, the row is in the session.
    node = db.get(models.VPNNode, node_id)
    if node is not None:
        POOL_HITS.labels(node=node.name).inc()
        POOL_DEPTH.labels(node=node.name).set(pool_depth(db, node_id))

    return bundle


def record_pool_miss(db: Session, node_id: int) -> None:
    """Increment the miss counter when assignment fell through to cold."""
    node = db.get(models.VPNNode, node_id)
    if node is not None:
        POOL_MISSES.labels(node=node.name).inc()


# ── Two-stage revoke ─────────────────────────────────────────────────

def unassign_bundle(
    db: Session, credentials: list[models.Credential]
) -> list[str]:
    """Stage 1 of revoke: flip ``assigned → revoked`` in the DB only.

    Returns a list of ``(node_id, access_username)`` pairs the worker
    needs to physically remove from the nodes. Caller commits.
    """
    pending: dict[tuple[int, str], None] = {}
    now = utcnow()
    for cred in credentials:
        cred.pool_state = models.CredentialPoolState.revoked
        cred.is_active = False
        cred.revoked_at = cred.revoked_at or now
        if cred.node_id and cred.access_username:
            pending[(cred.node_id, cred.access_username)] = None
    db.flush()
    return [f"{n}:{u}" for (n, u) in pending.keys()]


def physical_revoke_credential_bundle(
    db: Session, node_id: int, access_username: str
) -> bool:
    """Stage 2 of revoke: actually remove the user from the node.

    Idempotent: if the bundle was already removed (rows missing) we
    return ``True`` so a retry doesn't spuriously fail. Returns
    ``False`` only on Ansible non-zero, in which case the worker job
    will retry under RQ's retry policy.
    """
    creds = (
        db.query(models.Credential)
        .filter(models.Credential.node_id == node_id)
        .filter(models.Credential.access_username == access_username)
        .filter(models.Credential.pool_state == models.CredentialPoolState.revoked)
        .all()
    )
    if not creds:
        logger.info(
            "physical_revoke: bundle %s on node %s already gone, no-op",
            access_username, node_id,
        )
        return True

    node = db.get(models.VPNNode, node_id)
    if node is None:
        logger.warning("physical_revoke: node %s missing, dropping bundle", node_id)
        for c in creds:
            db.delete(c)
        db.commit()
        return True

    protocols_payload: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in creds:
        if c.proto in seen:
            continue
        seen.add(c.proto)
        entry: dict[str, Any] = {"proto": c.proto}
        if c.config is not None:
            entry["port"] = c.config.port
        protocols_payload.append(entry)

    payload = {
        "username": access_username,
        "protocols": protocols_payload,
        "state": "absent",
        "reason": "warm_pool revoke",
    }

    inventory = None
    _warmer_semaphore.acquire()
    try:
        inventory = build_inventory_for_node(node)
        result = run_playbook(
            "playbooks/provision_device.yml",
            inventory, limit=node.name, extra_vars=payload,
        )
    except Exception:  # noqa: BLE001
        logger.exception("physical_revoke: ansible run failed for %s", access_username)
        return False
    finally:
        _warmer_semaphore.release()
        if inventory is not None:
            try:
                inventory.unlink()
            except OSError:
                pass

    if result.returncode != 0:
        logger.error(
            "physical_revoke: ansible non-zero for %s on %s: %s",
            access_username, node.name, (result.stderr or "")[:300],
        )
        return False

    for c in creds:
        db.delete(c)
    db.commit()
    logger.info("physical_revoke: removed bundle %s from node %s", access_username, node.name)
    return True


def invalidate_node_warm_pool(db: Session, node_id: int, *, reason: str = "config changed") -> int:
    """Drop every warm bundle on a node — used when its protocol set changes.

    Marks them ``revoked`` (not deleted) so the worker can run
    ``state=absent`` and clean up the node before the rows go away.
    Returns the number of credentials marked.
    """
    creds = (
        db.query(models.Credential)
        .filter(models.Credential.node_id == node_id)
        .filter(models.Credential.pool_state == models.CredentialPoolState.warm)
        .all()
    )
    now = utcnow()
    for c in creds:
        c.pool_state = models.CredentialPoolState.revoked
        c.revoked_at = now
        c.is_active = False
    db.flush()
    db.commit()
    logger.info("warm_pool: invalidated %d warm credentials on node %s (%s)", len(creds), node_id, reason)
    return len(creds)


def run_warm_pool_revoke_sweep(
    db: Session, batch_limit: int | None = None
) -> dict[str, bool]:
    """Stage 2 driver: physically remove revoked warm bundles from nodes.

    Finds every distinct ``(node_id, access_username)`` still sitting in
    ``pool_state=revoked`` — both bundles unassigned by a user and those
    dropped by :func:`invalidate_node_warm_pool` — and runs
    :func:`physical_revoke_credential_bundle` for each, up to
    ``batch_limit`` per tick. Without this sweep revoked identities never
    leave the node (extra xray clients) and their rows accumulate forever.

    Meant to be called from a periodic worker tick (see ``app.worker``,
    alongside ``run_warm_pool_check``). Returns a ``{"node:username":
    ok}`` summary for the tick's audit trail.

    A bundle that fails Ansible stays ``revoked`` and is retried next tick
    (``physical_revoke_credential_bundle`` is idempotent). After
    ``WARM_POOL_REVOKE_MAX_ATTEMPTS`` consecutive failures it is skipped
    and logged for manual review so a dead node isn't hammered forever.
    """
    if not WARM_POOL_ENABLED:
        return {}

    limit = batch_limit if batch_limit is not None else WARM_POOL_REVOKE_BATCH_PER_TICK

    pending = (
        db.query(models.Credential.node_id, models.Credential.access_username)
        .filter(models.Credential.pool_state == models.CredentialPoolState.revoked)
        .filter(models.Credential.access_username.isnot(None))
        .filter(models.Credential.node_id.isnot(None))
        .distinct()
        .all()
    )

    summary: dict[str, bool] = {}
    processed = 0
    for node_id, username in pending:
        if processed >= limit:
            break
        key = (node_id, username)
        with _revoke_attempts_lock:
            if _revoke_attempts.get(key, 0) >= WARM_POOL_REVOKE_MAX_ATTEMPTS:
                # Уже сдались по этому бандлу — не трогаем до перезапуска
                # воркера / ручного разбора, не тратим бюджет тика.
                continue
        processed += 1
        try:
            ok = physical_revoke_credential_bundle(db, node_id, username)
        except Exception:  # noqa: BLE001
            logger.exception(
                "warm_pool: revoke sweep crashed on %s (node %s)", username, node_id
            )
            ok = False
        summary[f"{node_id}:{username}"] = ok
        with _revoke_attempts_lock:
            if ok:
                _revoke_attempts.pop(key, None)
            else:
                attempts = _revoke_attempts.get(key, 0) + 1
                _revoke_attempts[key] = attempts
                if attempts >= WARM_POOL_REVOKE_MAX_ATTEMPTS:
                    logger.error(
                        "warm_pool: bundle %s on node %s failed physical revoke "
                        "%d times — giving up, needs manual review",
                        username, node_id, attempts,
                    )
    return summary
