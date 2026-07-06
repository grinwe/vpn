"""Passive xray traffic stats collector (Phase B).

Why this exists
---------------
Active probes from a DC vantage point catch *physical* node death (host
unreachable, TLS broken, port refused) but cannot see the thing we
actually care about — RKN edge blocking that hits residential ISPs in
Russia. The DC has its own backbone routing and reaches our nodes even
when half the country can't.

So we add a *passive* signal alongside the active probes:
xray's gRPC StatsService is already enabled on every install_vless_*
node (loopback only — see roles/install_vless_*/templates/*.json.j2).
The service exposes per-user uplink/downlink byte counters by email.
Calling ``xray api statsquery --reset`` returns the deltas since the
last reset, which we read every TRAFFIC_STATS_INTERVAL seconds and
persist into ``node_traffic_samples``.

We deliberately do NOT yet wire these rows into the health detector.
The first iteration is "collect data, see what normal looks like" —
the threshold logic that flips a node to ``blocked_regions`` based on
a sustained traffic drop lives in a follow-up once we have a few days
of baseline.

Per-protocol gRPC ports (defaults baked into the install_vless_* roles):

  vless-reality → 127.0.0.1:10085  (config.json.j2 line 37)
  vless-ws-cdn  → 127.0.0.1:10086  (config_ws_cdn.json.j2 line 35)
  vless-xhttp   → 127.0.0.1:10087  (config_xhttp.json.j2 line 35)

The collector tries each port that exists; missing protocols don't
raise — they just contribute 0 bytes to the row and land in the
``_errors`` key of the ``details`` JSONB so we can see why.

SSH transport
-------------
We reuse the same ed25519 key the worker uses for ansible runs
(``ANSIBLE_PRIVATE_KEY_FILE`` → ``/run/secrets/provisioning_key`` in
the worker container). paramiko is already a backend dep (used by
node_spawner for the post-spawn waitssh probe).

The ``UNKNOWN_HOSTKEY_POLICY`` is ``AutoAddPolicy`` because nodes are
churned in/out by the autoscaler — pinning host keys would break
every spawn. The key file mode and Docker network isolation are the
defence-in-depth here.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent import futures as _futures
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# (protocol, loopback gRPC port) — keep in sync with the role defaults.
# Order is irrelevant; we walk the whole list and merge results.
KNOWN_PROTOCOL_PORTS: list[tuple[str, int]] = [
    ("vless-reality", 10085),
    ("vless-ws-cdn", 10086),
    ("vless-xhttp", 10087),
]

XRAY_BIN = "/usr/local/bin/xray"
SSH_PORT_DEFAULT = 22
SSH_USER = "root"
SSH_CONNECT_TIMEOUT = 10
SSH_COMMAND_TIMEOUT = 15

# Per-tick бюджеты сборщика (см. collect_all_active_nodes). RQ-джоба
# tick-traffic-stats имеет job_timeout=120s (queue.py::TICK_TIMEOUTS) —
# wall-clock-бюджет держим ниже с запасом, чтобы тик успел вернуть уже
# собранные сэмплы вместо того, чтобы быть убитым kill-horse'ом.
TRAFFIC_STATS_BUDGET_SEC_DEFAULT = 100
# Сколько SSH-сессий держать параллельно. paramiko-вызовы блокирующие
# и независимы по нодам; при последовательном обходе недоступная нода
# стоит 10-30с и хвост флота не успевает опроситься за бюджет.
TRAFFIC_STATS_SSH_WORKERS_DEFAULT = 8


@dataclass
class _NodeRef:
    """Снимок атрибутов ноды для SSH-потоков.

    Per-node commit в ``collect_all_active_nodes`` экспайрит ORM-объекты
    (expire_on_commit), и обращение к ним из воркер-потоков дёрнуло бы
    сессию не из главного потока. Поэтому всё нужное копируем заранее.
    """

    id: int
    name: str
    host: str
    ssh_port: int | None


@dataclass
class ProtocolStats:
    """Per-protocol byte/user totals over one collection interval."""

    uplink: int = 0
    downlink: int = 0
    users: set[str] = field(default_factory=set)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # ``users`` used to be a bare count (``int``). We now persist the
        # sorted list of access_usernames so the admin UI can show "who
        # is on this node right now" without a second round-trip to SSH.
        # ``user_count`` kept for consumers that only want the cardinal.
        # Readers must be tolerant to the legacy int format on historical
        # samples — see api/nodes.py::list_node_users.
        out: dict[str, Any] = {
            "uplink": self.uplink,
            "downlink": self.downlink,
            "users": sorted(self.users),
            "user_count": len(self.users),
        }
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class SharingViolation:
    """One violation entry read from the enforcer's JSONL log."""

    ts: str
    email: str
    ips: list[str]
    ip_count: int
    action: str
    severity: str  # "warning" | "kick" | "block"


@dataclass
class NodeStatsResult:
    """Aggregate stats for one node over one tick."""

    uplink_bytes: int = 0
    downlink_bytes: int = 0
    active_users: int = 0
    per_protocol: dict[str, ProtocolStats] = field(default_factory=dict)
    sharing_violations: list[SharingViolation] = field(default_factory=list)

    def to_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            proto: stats.to_dict() for proto, stats in self.per_protocol.items()
        }
        errors = {p: s.error for p, s in self.per_protocol.items() if s.error}
        if errors:
            details["_errors"] = errors
        return details


def _parse_stat_name(name: str) -> tuple[str, str] | None:
    """Parse an xray stat name into ``(email, direction)``.

    xray emits stats like:
        user>>>alice@example.com>>>traffic>>>uplink
        user>>>alice@example.com>>>traffic>>>downlink
        inbound>>>vless-reality>>>traffic>>>uplink

    We only care about the per-user traffic counters here. The
    inbound>>> stats double-count what's already in user>>> and the
    detector wants to know which *users* moved bytes, not the inbound
    aggregate.
    """
    parts = name.split(">>>")
    if len(parts) != 4:
        return None
    if parts[0] != "user" or parts[2] != "traffic":
        return None
    email = parts[1]
    direction = parts[3]
    if direction not in ("uplink", "downlink"):
        return None
    return email, direction


def _parse_xray_stats_payload(raw: str) -> ProtocolStats:
    """Parse xray's ``statsquery --reset`` JSON output for one protocol.

    Format (from xray-core source xray/main/commands/api/statsquery.go):
        {
          "stat": [
            {"name": "user>>>alice@example.com>>>traffic>>>uplink",
             "value": "12345"},
            ...
          ]
        }

    Empty/missing ``stat`` key on a fresh node with no traffic is fine
    — returns a zero-filled ProtocolStats. We tolerate missing
    ``value`` (treat as 0) and non-int values (skip + log).
    """
    stats = ProtocolStats()
    if not raw.strip():
        return stats
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        stats.error = f"json parse: {exc}"
        return stats

    rows = payload.get("stat") or []
    if not isinstance(rows, list):
        stats.error = "stat key is not a list"
        return stats

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str):
            continue
        parsed = _parse_stat_name(name)
        if parsed is None:
            continue
        email, direction = parsed
        try:
            value = int(row.get("value") or 0)
        except (TypeError, ValueError):
            logger.debug("skipping non-int stat value for %s: %r", name, row.get("value"))
            continue
        if value <= 0:
            # Reset counters return 0s for users with no activity since
            # last reset; they're noise, drop them. (We still want to
            # count *positive* deltas as "this user is active".)
            continue
        if direction == "uplink":
            stats.uplink += value
        else:
            stats.downlink += value
        stats.users.add(email)
    return stats


def _ssh_run(client, command: str) -> tuple[int, str, str]:
    """Run ``command`` on the connected ``paramiko.SSHClient``.

    Returns ``(exit_status, stdout, stderr)``. Times out the command at
    SSH_COMMAND_TIMEOUT.
    """
    stdin, stdout, stderr = client.exec_command(command, timeout=SSH_COMMAND_TIMEOUT)
    out_text = stdout.read().decode("utf-8", errors="replace")
    err_text = stderr.read().decode("utf-8", errors="replace")
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, out_text, err_text


def collect_node_stats(node) -> NodeStatsResult:
    """SSH into ``node`` and pull current xray stats for each protocol.

    Returns a ``NodeStatsResult`` even on partial failure — protocols
    that errored out get a populated ``error`` field but the others
    still contribute. Raises only on hard transport failure (key
    missing, connection refused, auth failed) so the worker tick can
    log + skip the row instead of crashing the whole tick.
    """
    try:
        import paramiko  # noqa: WPS433 — lazy import keeps it out of API
    except ImportError as exc:  # pragma: no cover — paramiko is in requirements.txt
        raise RuntimeError("paramiko not installed in the worker container") from exc

    key_path = (
        os.getenv("ANSIBLE_PRIVATE_KEY_FILE")
        or os.getenv("PROVISIONING_SSH_KEY")
        or "/run/secrets/provisioning_key"
    )
    if not os.path.exists(key_path):
        raise RuntimeError(f"provisioning ssh key not found at {key_path}")

    pkey = paramiko.Ed25519Key.from_private_key_file(key_path)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=node.host,
            port=node.ssh_port or SSH_PORT_DEFAULT,
            username=SSH_USER,
            pkey=pkey,
            timeout=SSH_CONNECT_TIMEOUT,
            banner_timeout=SSH_CONNECT_TIMEOUT,
            auth_timeout=SSH_CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )

        result = NodeStatsResult()
        all_users: set[str] = set()
        for proto, port in KNOWN_PROTOCOL_PORTS:
            cmd = (
                f"{XRAY_BIN} api statsquery "
                f"--server=127.0.0.1:{port} "
                f"--reset 2>/dev/null"
            )
            try:
                exit_status, stdout, stderr = _ssh_run(client, cmd)
            except Exception as exc:  # noqa: BLE001
                stats = ProtocolStats(error=f"ssh exec: {exc}")
                result.per_protocol[proto] = stats
                continue

            if exit_status != 0:
                # Most common cause: this protocol isn't installed on
                # the node, so the gRPC port doesn't exist. xray's CLI
                # exits non-zero with a "connection refused" message.
                # Don't surface that as a row-level error — just record
                # an empty ProtocolStats and move on.
                stats = ProtocolStats()
                if stderr.strip():
                    stats.error = stderr.strip().splitlines()[-1][:200]
                result.per_protocol[proto] = stats
                continue

            stats = _parse_xray_stats_payload(stdout)
            result.per_protocol[proto] = stats
            result.uplink_bytes += stats.uplink
            result.downlink_bytes += stats.downlink
            all_users.update(stats.users)

        result.active_users = len(all_users)

        # ── Read sharing enforcer violations (Phase 2) ─────────────
        # Gated off by default: the v2 enforcer was kicking legitimate
        # users on small-fleet prod (phone + laptop = "concurrent IP"
        # under CGNAT). We keep the read wired for when detection is
        # tuned and re-enabled — flip `SHARING_ENFORCEMENT_ENABLED=1`
        # on the worker. See matching ansible gate in
        # install_sharing_enforcer/tasks/main.yml.
        if os.getenv("SHARING_ENFORCEMENT_ENABLED", "0") == "1":
            try:
                # Атомарный забор лога: rename в уникальное имя (в пределах
                # одной ФС rename(2) атомарен), затем читаем уже
                # переименованный файл и удаляем его. Прежний
                # `cat && truncate -s 0` терял события, дописанные энфорсером
                # между cat и truncate. После mv энфорсер пересоздаёт
                # оригинальный путь при следующей записи — гонка сужается до
                # одного write, попавшего между read()+rename ядра, что
                # практически недостижимо. $$ = PID удалённого shell'а,
                # $RANDOM защищает от коллизии перекрывающихся тиков.
                viol_cmd = (
                    "f=/var/log/xray/sharing_violations.jsonl; "
                    't=$f.reading.$$.$RANDOM; '
                    'mv "$f" "$t" 2>/dev/null && cat "$t"; '
                    'rm -f "$t" 2>/dev/null'
                )
                v_rc, v_out, _ = _ssh_run(client, viol_cmd)
                if v_rc == 0 and v_out.strip():
                    for line in v_out.strip().splitlines():
                        try:
                            row = json.loads(line)
                            result.sharing_violations.append(SharingViolation(
                                ts=row.get("ts", ""),
                                email=row.get("email", ""),
                                ips=row.get("ips", []),
                                ip_count=row.get("ip_count", 0),
                                action=row.get("action", ""),
                                severity=row.get("severity", "warning"),
                            ))
                        except (json.JSONDecodeError, KeyError):
                            continue
            except Exception:  # noqa: BLE001
                logger.debug("sharing violations read failed on node %s", node.id)

        return result
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 — defensive close
            pass


def collect_and_persist(session, node, interval_seconds: int) -> dict[str, Any] | None:
    """High-level wrapper: collect for one node and write a row.

    Returns the inserted row as a dict for the worker tick summary, or
    ``None`` on hard failure (already logged).

    Also ingests any sharing violations the local enforcer daemon
    detected since the last tick, writing them as AuditLog rows for
    admin visibility.
    """
    try:
        result = collect_node_stats(node)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "traffic_stats: collect failed for node %s (%s): %s",
            node.id, node.name, exc,
        )
        return None

    return _persist_node_result(session, node, result, interval_seconds)


def _persist_node_result(session, node, result: NodeStatsResult, interval_seconds: int) -> dict[str, Any]:
    """Записать уже собранный ``NodeStatsResult`` одной ноды в сессию.

    Вынесено из ``collect_and_persist``, чтобы параллельный сборщик
    (``collect_all_active_nodes``) мог собирать по SSH в потоках, а все
    записи в сессию делать строго из главного потока. ``node`` — ORM-нода
    либо ``_NodeRef`` (используются только ``.id`` и ``.name``).
    """
    from .. import models  # local import to avoid circular dep with services/__init__

    sample = models.NodeTrafficSample(
        node_id=node.id,
        interval_seconds=interval_seconds,
        uplink_bytes=result.uplink_bytes,
        downlink_bytes=result.downlink_bytes,
        active_users=result.active_users,
        details=result.to_details(),
    )
    session.add(sample)

    # Ingest sharing violations into AuditLog for admin visibility
    # and user-facing notifications (via bot notification poller).
    # Deduplicate: only one AuditLog per (email, severity) per batch.
    # The enforcer may fire dozens of violations per email between
    # collection ticks — the user should get at most one notification
    # per severity level per tick.
    _SEVERITY_ACTION_MAP = {
        "warning": "sharing_warning",
        "kick": "sharing_kick",
        "block": "sharing_block",
    }
    _seen_violations: set[tuple[str, str]] = set()  # (email, severity)
    for v in result.sharing_violations:
        dedup_key = (v.email, v.severity)
        if dedup_key in _seen_violations:
            continue
        _seen_violations.add(dedup_key)

        audit_action = _SEVERITY_ACTION_MAP.get(v.severity, "sharing_warning")

        # Resolve email (access_username) → Device → User → telegram_id
        telegram_id = None
        user_id = None
        device = (
            session.query(models.Device)
            .filter_by(access_username=v.email)
            .first()
        )
        if device and device.user:
            user_id = device.user_id
            telegram_id = str(device.user.telegram_id) if device.user.telegram_id else None

        session.add(models.AuditLog(
            actor="system",
            actor_type=models.AuditActor.system,
            action=audit_action,
            target_type="user" if user_id else "node",
            target_id=user_id or node.id,
            extra={
                "node_name": node.name,
                "email": v.email,
                "ips": v.ips,
                "ip_count": v.ip_count,
                "action": v.action,
                "severity": v.severity,
                "enforcer_ts": v.ts,
                "telegram_id": telegram_id,
            },
        ))
    if result.sharing_violations:
        logger.warning(
            "traffic_stats: %d sharing violation(s) on node %s (%s)",
            len(result.sharing_violations), node.id, node.name,
        )

    return {
        "node_id": node.id,
        "node": node.name,
        "uplink_bytes": result.uplink_bytes,
        "downlink_bytes": result.downlink_bytes,
        "active_users": result.active_users,
        "sharing_violations": len(result.sharing_violations),
    }


# Effectively disables auto-migration at small fleet sizes. While the
# service has on the order of ~10 concurrent users total, any natural
# fluctuation can drop active_users to 0 for a tick, and auto-migration
# on that basis causes far more damage than it prevents (sub.node_id
# flips, warm pool churn, device re-provisioning). We keep the detector
# wired so the audit trail (`traffic_drop_detected`/`_cleared`) still
# records suspicious silence, but the migration gate is set high enough
# that it only fires once we have real scale (50+ concurrent users on
# the smallest node). Lower the env var `TRAFFIC_DROP_MIN_USERS` when
# ready to re-enable at smaller thresholds.
MIN_ACTIVE_USERS_DEFAULT = 100
# Number of consecutive zero-user ticks we need to see after a drop
# before migrating. With TRAFFIC_STATS_INTERVAL=300 the default of 3
# means ~15 min of sustained silence — long enough that a transient
# all-idle window (video paused, phone screens off) doesn't trigger
# migration. A real TSPU edge-block will keep giving us zeros past
# this bound, so confirmation still fires within one interval budget.
CONFIRM_TICKS_DEFAULT = 3
# Minimum traffic floor (bytes per tick) that flips the "no users"
# reading from "likely idle" to "likely blocked". xray keepalives and
# idle TLS chatter typically produce <100KB/5min per connection, so
# anything above ~1MB total across all users says somebody is really
# moving data — not a block scenario.
MIN_IDLE_BYTES_DEFAULT = 1_000_000


def detect_traffic_drops(session, current_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Phase D — detect nodes where traffic went quiet after being busy.

    Called after ``collect_all_active_nodes`` on every traffic_stats tick.

    The detector is **two-stage** on purpose — we don't trust a single
    zero-users tick. ``xray api statsquery --reset`` only counts users
    who actually moved bytes in the interval, so a tick where every
    client happens to be idle (video paused, screen off, keepalive-only)
    legitimately reports ``active_users=0`` with no block involved.

    Stage A (this tick, Phase 2 below) — mark suspect:
      * ``curr.active_users == 0`` AND
      * ``curr.uplink+downlink < MIN_IDLE_BYTES`` AND
      * previous tick showed ``active_users >= MIN_USERS``
      → set ``suspect_since = now``. **No migration yet.**

    Stage B (subsequent ticks, Phase 1 below) — confirm or clear:
      * Once ``suspect_since`` is older than ``CONFIRM_TICKS * interval``,
        inspect the source node's own recent samples.
      * If any of the last ``CONFIRM_TICKS`` samples had active_users>0 or
        traffic above the idle floor → false alarm, clear ``suspect_since``.
      * Otherwise → migrate subs off to another region, mark ``error``.

    Migration is deliberately guarded behind confirmation: flipping
    ``subscription.node_id`` has real UX cost (users reconnect, warm
    pool consumed, admin sees divergence between DB and xray config)
    and has to be a deliberate decision, not a single-tick reaction.

    DISABLED 2026-04-15: детектор триггерился на обычные idle-окна и
    начинал переселять активных пользователей, из-за чего у юзеров
    отваливалось подключение каждые 5-10 минут (совпадает с
    TRAFFIC_STATS_INTERVAL=300s). Пороги/эвристику нужно пересмотреть
    до возврата автомиграции. Функция оставлена no-op — достаточно
    снять early-return ниже чтобы восстановить поведение.
    """
    return []

    # ── original body kept for future re-enable ─────────────────────
    from .. import models  # noqa: E402
    from ..time_utils import utcnow  # noqa: E402
    from .health import migrate_subscriptions_off, DEFAULT_COOLDOWN  # noqa: E402

    min_users = int(os.getenv("TRAFFIC_DROP_MIN_USERS", str(MIN_ACTIVE_USERS_DEFAULT)))
    confirm_ticks = int(os.getenv("TRAFFIC_DROP_CONFIRM_TICKS", str(CONFIRM_TICKS_DEFAULT)))
    min_idle_bytes = int(os.getenv("TRAFFIC_DROP_MIN_IDLE_BYTES", str(MIN_IDLE_BYTES_DEFAULT)))
    interval = int(os.getenv("TRAFFIC_STATS_INTERVAL", "300"))
    results: list[dict[str, Any]] = []

    # ── Phase 1: confirm or clear existing suspects ──────────────────
    suspect_nodes = (
        session.query(models.VPNNode)
        .filter(models.VPNNode.suspect_since.isnot(None))
        .all()
    )
    for node in suspect_nodes:
        elapsed = (utcnow() - node.suspect_since).total_seconds()
        if elapsed < interval * confirm_ticks:
            continue  # not enough ticks yet

        # Pull the last N samples on THIS node (not the migration
        # target) — the question we're answering is "did the source
        # node stay quiet?", not "are users now somewhere else?".
        recent = (
            session.query(models.NodeTrafficSample)
            .filter(models.NodeTrafficSample.node_id == node.id)
            .order_by(models.NodeTrafficSample.observed_at.desc())
            .limit(confirm_ticks)
            .all()
        )
        still_silent = bool(recent) and all(
            s.active_users == 0
            and (s.uplink_bytes + s.downlink_bytes) < min_idle_bytes
            for s in recent
        )

        if not still_silent:
            # Users came back or traffic reappeared — false alarm.
            node.suspect_since = None
            session.add(node)
            session.add(models.AuditLog(
                actor="traffic_drop_detector",
                actor_type=models.AuditActor.system,
                action="traffic_drop_cleared",
                target_type="vpn_node",
                target_id=node.id,
                extra={"node_name": node.name},
            ))
            results.append({"node_id": node.id, "node": node.name, "outcome": "cleared"})
            logger.info("traffic_drop: false alarm on node %s (%s), cleared", node.id, node.name)
            continue

        # Sustained silence across the confirmation window — now we
        # actually migrate. Up until this point no subscription.node_id
        # has been touched.
        try:
            migration = migrate_subscriptions_off(
                session, node, reason="traffic_drop", exclude_same_region=True,
            )
            migrated_ids = migration["subscription_ids"]
        except Exception:  # noqa: BLE001
            logger.exception(
                "traffic_drop: migration failed for node %s — leaving suspect flag set",
                node.id,
            )
            results.append({"node_id": node.id, "node": node.name, "outcome": "confirm_migration_failed"})
            continue

        node.status = models.VPNNodeStatus.error
        node.is_active = False
        node.cooldown_until = utcnow() + DEFAULT_COOLDOWN
        node.suspect_since = None
        session.add(node)
        session.add(models.AuditLog(
            actor="traffic_drop_detector",
            actor_type=models.AuditActor.system,
            action="traffic_drop_confirmed",
            target_type="vpn_node",
            target_id=node.id,
            extra={
                "node_name": node.name,
                "migrated_subscription_ids": migrated_ids,
                "confirm_ticks": confirm_ticks,
            },
        ))
        results.append({
            "node_id": node.id,
            "node": node.name,
            "outcome": "confirmed",
            "migrated": migrated_ids,
        })
        logger.warning(
            "traffic_drop: CONFIRMED block on node %s (%s), migrated %d subs",
            node.id, node.name, len(migrated_ids),
        )

    # ── Phase 2: detect new drops — mark suspect only, don't migrate ──
    now = utcnow()
    for summary in current_summaries:
        if summary["active_users"] != 0:
            continue
        # Bytes floor — if data is still flowing, this is an idle-user
        # artefact of statsquery, not a block.
        if (summary.get("uplink_bytes", 0) + summary.get("downlink_bytes", 0)) >= min_idle_bytes:
            continue

        node_id = summary["node_id"]
        node = session.get(models.VPNNode, node_id)
        if not node:
            continue

        # Already suspect — phase 1 above will handle it.
        if node.suspect_since is not None:
            continue

        # Get previous sample (the one before the current tick)
        prev_sample = (
            session.query(models.NodeTrafficSample)
            .filter(models.NodeTrafficSample.node_id == node_id)
            .order_by(models.NodeTrafficSample.observed_at.desc())
            # offset 1 = skip the sample we just wrote in this tick
            .offset(1)
            .first()
        )
        if not prev_sample or prev_sample.active_users < min_users:
            continue

        logger.warning(
            "traffic_drop: suspect on node %s (%s): %d → 0 users, awaiting %d confirm ticks",
            node.id, node.name, prev_sample.active_users, confirm_ticks,
        )
        node.suspect_since = now
        session.add(node)
        session.add(models.AuditLog(
            actor="traffic_drop_detector",
            actor_type=models.AuditActor.system,
            action="traffic_drop_detected",
            target_type="vpn_node",
            target_id=node.id,
            extra={
                "node_name": node.name,
                "prev_active_users": prev_sample.active_users,
                "curr_active_users": 0,
                "confirm_ticks_required": confirm_ticks,
            },
        ))
        results.append({"node_id": node.id, "node": node.name, "outcome": "suspect"})

    if results:
        session.commit()
    return results


def collect_all_active_nodes(session, interval_seconds: int) -> list[dict[str, Any]]:
    """Walk every active node and collect stats. Returns one summary
    row per successfully collected node.

    Skips nodes in ``registering`` (xray not yet up) and ``disabled``
    (no stats to read). Draining nodes are still collected because they
    keep serving subs until the migration tick clears them.

    Сбор параллельный (ThreadPoolExecutor, ``TRAFFIC_STATS_SSH_WORKERS``,
    default 8): SSH-вызовы блокирующие и независимы по нодам, а записи в
    сессию делаются только из главного потока. Каждый успешно собранный
    сэмпл коммитится сразу (per-node commit) — если тик убьют по
    job_timeout, частичный прогресс не теряется. Wall-clock-бюджет
    ``TRAFFIC_STATS_BUDGET_SEC`` (default 100с, job_timeout тика = 120с):
    при исчерпании недособранный хвост нод откладывается до следующего
    тика — тот же паттерн, что в run_node_reachability_tick.
    """
    from .. import models

    nodes = (
        session.query(models.VPNNode)
        .filter(
            models.VPNNode.is_active.is_(True),
            models.VPNNode.status.in_(
                [
                    models.VPNNodeStatus.active,
                    models.VPNNodeStatus.draining,
                ]
            ),
        )
        .all()
    )
    if not nodes:
        return []

    # Снимок атрибутов ДО старта потоков — см. docstring _NodeRef.
    refs = [
        _NodeRef(id=n.id, name=n.name, host=n.host, ssh_port=n.ssh_port)
        for n in nodes
    ]

    budget_s = int(
        os.getenv("TRAFFIC_STATS_BUDGET_SEC", str(TRAFFIC_STATS_BUDGET_SEC_DEFAULT))
    )
    workers = max(
        1,
        int(os.getenv("TRAFFIC_STATS_SSH_WORKERS", str(TRAFFIC_STATS_SSH_WORKERS_DEFAULT))),
    )

    summaries: list[dict[str, Any]] = []
    executor = _futures.ThreadPoolExecutor(
        max_workers=min(workers, len(refs)),
        thread_name_prefix="traffic-stats-ssh",
    )
    try:
        future_to_ref = {
            executor.submit(collect_node_stats, ref): ref for ref in refs
        }
        try:
            for fut in _futures.as_completed(future_to_ref, timeout=budget_s):
                ref = future_to_ref[fut]
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "traffic_stats: collect failed for node %s (%s): %s",
                        ref.id, ref.name, exc,
                    )
                    continue
                try:
                    summary = _persist_node_result(session, ref, result, interval_seconds)
                    # Per-node commit: kill по job_timeout не теряет уже
                    # собранные сэмплы этого тика.
                    session.commit()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "traffic_stats: persist failed for node %s (%s)",
                        ref.id, ref.name,
                    )
                    if session.is_active:
                        session.rollback()
                    continue
                summaries.append(summary)
        except _futures.TimeoutError:
            pending = [r.name for f, r in future_to_ref.items() if not f.done()]
            logger.warning(
                "traffic_stats: wall-clock budget %ss hit, %d node(s) deferred "
                "to next tick: %s",
                budget_s, len(pending), pending,
            )
    finally:
        # Не ждём зависшие SSH-сессии: нестартовавшие фьючи отменяем,
        # уже бегущие потоки дособерут в фоне и умрут вместе с джобой.
        executor.shutdown(wait=False, cancel_futures=True)
    return summaries
