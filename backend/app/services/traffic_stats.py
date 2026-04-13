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


@dataclass
class ProtocolStats:
    """Per-protocol byte/user totals over one collection interval."""

    uplink: int = 0
    downlink: int = 0
    users: set[str] = field(default_factory=set)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "uplink": self.uplink,
            "downlink": self.downlink,
            "users": len(self.users),
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
        # The local enforcer daemon appends JSONL to this file. We
        # read + truncate so each violation is ingested exactly once.
        try:
            viol_cmd = (
                "cat /var/log/xray/sharing_violations.jsonl 2>/dev/null "
                "&& truncate -s 0 /var/log/xray/sharing_violations.jsonl 2>/dev/null"
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
    from .. import models  # local import to avoid circular dep with services/__init__

    try:
        result = collect_node_stats(node)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "traffic_stats: collect failed for node %s (%s): %s",
            node.id, node.name, exc,
        )
        return None

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


def collect_all_active_nodes(session, interval_seconds: int) -> list[dict[str, Any]]:
    """Walk every active node and collect stats. Returns one summary
    row per successfully collected node.

    Skips nodes in ``registering`` (xray not yet up) and ``disabled``
    (no stats to read). Draining nodes are still collected because they
    keep serving subs until the migration tick clears them.
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

    summaries: list[dict[str, Any]] = []
    for node in nodes:
        summary = collect_and_persist(session, node, interval_seconds)
        if summary is not None:
            summaries.append(summary)
    if summaries:
        session.commit()
    return summaries
