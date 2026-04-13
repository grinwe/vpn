#!/usr/bin/env python3
"""xray_enforcer — local credential sharing enforcer daemon (v2).

Runs on each VPN node as a systemd service.  Detects **concurrent**
connections (2+ IPs active in the same time slot) rather than simple
unique-IP counting, which eliminates false positives from mobile
network handoffs.

Three-tier graduated enforcement:

  Tier 1 — Warning:
    3 concurrent-IP detections within 1 hour → log violation with
    severity "warning".  Backend picks it up and sends a soft
    notification to the user via the Telegram bot.  No kick.

  Tier 2 — Kick with cooldown:
    After warning sent, continued concurrent detections → rmuser +
    sleep + adduser every KICK_COOLDOWN seconds.  VPN becomes
    practically unusable for the sharer.

  Tier 3 — Block:
    3 kicks within 12 hours → rmuser WITHOUT adduser.  User is
    fully blocked until an admin triggers an unblock.

All violations are appended to VIOLATION_LOG as JSONL with a
``severity`` field ("warning" / "kick" / "block") so the backend
can route them to different notification templates.

Blocked users are also written to BLOCKLIST_FILE so that resync
playbooks skip them.  Unblock requests arrive via UNBLOCK_FILE
(one email per line), processed on every tick.

Configuration is via environment variables (see defaults below).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Configuration (env vars) ───────────────────────────────────────
CHECK_INTERVAL = int(os.getenv("ENFORCER_CHECK_INTERVAL", "10"))
WINDOW_SECONDS = int(os.getenv("ENFORCER_WINDOW_SECONDS", "300"))
MAX_IPS = int(os.getenv("ENFORCER_MAX_IPS", "1"))
RECONNECT_DELAY = float(os.getenv("ENFORCER_RECONNECT_DELAY", "2"))

# Concurrent-IP slot width (seconds).  Two IPs must both appear
# within the same slot to count as concurrent.
SLOT_SECONDS = int(os.getenv("ENFORCER_SLOT_SECONDS", "120"))

# Tier 1: how many concurrent-IP detections within WARNING_WINDOW
# before a warning is emitted.
WARNING_THRESHOLD = int(os.getenv("ENFORCER_WARNING_THRESHOLD", "3"))
WARNING_WINDOW = int(os.getenv("ENFORCER_WARNING_WINDOW", "3600"))

# Tier 2: minimum seconds between kicks for the same email.
KICK_COOLDOWN = int(os.getenv("ENFORCER_KICK_COOLDOWN", "120"))

# Tier 3: kicks within BLOCK_WINDOW that trigger a full block.
BLOCK_THRESHOLD = int(os.getenv("ENFORCER_BLOCK_THRESHOLD", "3"))
BLOCK_WINDOW = int(os.getenv("ENFORCER_BLOCK_WINDOW", "43200"))

ACCESS_LOG_DIR = Path(os.getenv("ENFORCER_LOG_DIR", "/var/log/xray"))
CONFIG_DIR = Path(os.getenv("ENFORCER_CONFIG_DIR", "/usr/local/etc/xray"))
VIOLATION_LOG = ACCESS_LOG_DIR / "sharing_violations.jsonl"
BLOCKLIST_FILE = ACCESS_LOG_DIR / "enforcer_blocklist.txt"
UNBLOCK_FILE = ACCESS_LOG_DIR / "enforcer_unblock.txt"
STATE_FILE = ACCESS_LOG_DIR / "enforcer_state.json"
XRAY_BIN = os.getenv("ENFORCER_XRAY_BIN", "/usr/local/bin/xray")
TAIL_LINES = 5000

# (config_filename, inbound_tag, gRPC API port)
PROTOCOLS: list[tuple[str, str, int]] = [
    ("config.json", "vless-reality", 10085),
    ("config_ws_cdn.json", "vless-ws-cdn", 10086),
    ("config_xhttp.json", "vless-xhttp", 10087),
]

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("xray_enforcer")

# ── Access log parser ──────────────────────────────────────────────
# Format variants:
#   2024/01/01 12:00:00.123456 from 1.2.3.4:12345 accepted tcp:... email: user-1-2
#   2024/01/01 12:00:00 from tcp:1.2.3.4:12345 accepted ...  email: user-1-2
_LINE_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})(?:\.\d+)?"
    r"\s+from\s+(?:tcp:)?(\d+\.\d+\.\d+\.\d+):\d+"
    r"\s+accepted\b.*?"
    r"\s+email:\s+(\S+)"
)


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Per-email enforcement state ───────────────────────────────────
@dataclass
class EmailState:
    detections: list[str] = field(default_factory=list)
    warning_sent_at: str | None = None
    kicks: list[str] = field(default_factory=list)
    blocked: bool = False
    last_kick_at: str | None = None


def _load_state() -> dict[str, EmailState]:
    """Load persisted state from JSON, pruning entries older than 24h."""
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("state file corrupt, starting fresh")
        return {}

    cutoff = (_utcnow() - timedelta(hours=24)).isoformat()
    result: dict[str, EmailState] = {}
    for email, data in raw.items():
        if not isinstance(data, dict):
            continue
        es = EmailState(
            detections=[d for d in data.get("detections", []) if d > cutoff],
            warning_sent_at=data.get("warning_sent_at"),
            kicks=[k for k in data.get("kicks", []) if k > cutoff],
            blocked=data.get("blocked", False),
            last_kick_at=data.get("last_kick_at"),
        )
        # Keep entry if still blocked or has recent activity
        if es.blocked or es.detections or es.kicks or es.warning_sent_at:
            result[email] = es
    return result


def _save_state(state: dict[str, EmailState]) -> None:
    """Persist state atomically (write tmp + rename)."""
    data = {}
    for email, es in state.items():
        data[email] = {
            "detections": es.detections,
            "warning_sent_at": es.warning_sent_at,
            "kicks": es.kicks,
            "blocked": es.blocked,
            "last_kick_at": es.last_kick_at,
        }
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(STATE_FILE)
    except OSError:
        log.warning("failed to save state file")


# ── Concurrent IP detection ───────────────────────────────────────
def detect_concurrent_ips(
    window: int = WINDOW_SECONDS, slot: int = SLOT_SECONDS,
) -> dict[str, set[str]]:
    """Return {email: {concurrent_ips}} for emails with concurrent IPs.

    Divides the time window into non-overlapping slots of ``slot``
    seconds.  An email is flagged only if a single slot contains 2+
    distinct IPs, each with at least 2 log entries in that slot
    (filters out single-packet Wi-Fi→LTE handoff artifacts).
    """
    cutoff = _utcnow() - timedelta(seconds=window)
    # email → list of (timestamp, ip)
    entries: dict[str, list[tuple[datetime, str]]] = defaultdict(list)

    for log_file in sorted(ACCESS_LOG_DIR.glob("access-*.log")):
        try:
            raw = subprocess.check_output(
                ["tail", "-n", str(TAIL_LINES), str(log_file)],
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        for line in raw.splitlines():
            m = _LINE_RE.match(line)
            if not m:
                continue
            ts = _parse_ts(m.group(1))
            if ts is None or ts < cutoff:
                continue
            ip = m.group(2)
            if ip == "127.0.0.1":
                continue
            entries[m.group(3)].append((ts, ip))

    # Analyze slots per email
    result: dict[str, set[str]] = {}
    for email, pairs in entries.items():
        if len(pairs) < 2:
            continue
        # Group by slot: slot_index = (ts - cutoff) // slot_seconds
        slots: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for ts, ip in pairs:
            slot_idx = int((ts - cutoff).total_seconds()) // slot
            slots[slot_idx][ip] += 1

        # Check each slot for concurrent IPs with min 2 entries each
        for _slot_idx, ip_counts in slots.items():
            concurrent = {ip for ip, cnt in ip_counts.items() if cnt >= 2}
            if len(concurrent) > MAX_IPS:
                result[email] = concurrent
                break  # one offending slot is enough

    return result


# ── xray config reader ────────────────────────────────────────────
def _get_client_entries(email: str) -> list[dict]:
    """Find the client object(s) for ``email`` across all protocol configs."""
    entries: list[dict] = []
    for cfg_file, tag, port in PROTOCOLS:
        path = CONFIG_DIR / cfg_file
        if not path.exists():
            continue
        try:
            cfg = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for inbound in cfg.get("inbounds", []):
            if inbound.get("tag") != tag:
                continue
            for client in inbound.get("settings", {}).get("clients", []):
                if client.get("email") == email:
                    entries.append({
                        "uuid": client["id"],
                        "flow": client.get("flow", ""),
                        "tag": tag,
                        "port": port,
                    })
    return entries


# ── xray gRPC API calls ───────────────────────────────────────────
def _xray_rmuser(port: int, tag: str, email: str) -> bool:
    try:
        r = subprocess.run(
            [XRAY_BIN, "api", "rmuser",
             f"--server=127.0.0.1:{port}",
             f"-tag={tag}",
             f"-email={email}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except subprocess.SubprocessError as exc:
        log.debug("rmuser failed port=%s email=%s: %s", port, email, exc)
        return False


def _xray_adduser(port: int, tag: str, email: str, uuid: str, flow: str = "") -> bool:
    payload: dict = {"email": email, "id": uuid, "level": 0}
    if flow:
        payload["flow"] = flow
    try:
        r = subprocess.run(
            [XRAY_BIN, "api", "adduser",
             f"--server=127.0.0.1:{port}",
             f"-tag={tag}"],
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except subprocess.SubprocessError as exc:
        log.debug("adduser failed port=%s email=%s: %s", port, email, exc)
        return False


# ── Enforcement actions ───────────────────────────────────────────
def _log_violation(email: str, ips: set[str], action: str, severity: str) -> None:
    entry = {
        "ts": _utcnow().isoformat(),
        "email": email,
        "ips": sorted(ips),
        "ip_count": len(ips),
        "action": action,
        "severity": severity,
    }
    try:
        with open(VIOLATION_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        log.warning("failed to write violation log for %s", email)


def _do_kick(email: str, ips: set[str]) -> None:
    """rmuser + sleep + adduser — single disruption."""
    client_entries = _get_client_entries(email)
    if not client_entries:
        log.warning("no config entry for email=%s, skip kick", email)
        return

    removed = sum(1 for e in client_entries
                  if _xray_rmuser(e["port"], e["tag"], email))
    log.info("KICK: email=%s ips=%s — removed from %d/%d protocols",
             email, sorted(ips), removed, len(client_entries))

    time.sleep(RECONNECT_DELAY)

    restored = sum(1 for e in client_entries
                   if _xray_adduser(e["port"], e["tag"], email, e["uuid"], e["flow"]))
    log.info("KICK: email=%s — restored to %d/%d protocols",
             email, restored, len(client_entries))


def _do_block(email: str) -> None:
    """rmuser WITHOUT adduser — full block."""
    client_entries = _get_client_entries(email)
    if not client_entries:
        log.warning("no config entry for email=%s, skip block", email)
        return

    removed = sum(1 for e in client_entries
                  if _xray_rmuser(e["port"], e["tag"], email))
    log.info("BLOCK: email=%s — removed from %d/%d protocols, NO re-add",
             email, removed, len(client_entries))

    # Add to blocklist so resync skips this user
    _add_to_blocklist(email)


def _add_to_blocklist(email: str) -> None:
    """Append email to blocklist file (idempotent)."""
    existing = _read_blocklist()
    if email in existing:
        return
    try:
        with open(BLOCKLIST_FILE, "a") as f:
            f.write(email + "\n")
    except OSError:
        log.warning("failed to write blocklist for %s", email)


def _remove_from_blocklist(email: str) -> None:
    """Remove email from blocklist file."""
    existing = _read_blocklist()
    if email not in existing:
        return
    existing.discard(email)
    try:
        BLOCKLIST_FILE.write_text("\n".join(sorted(existing)) + "\n" if existing else "")
    except OSError:
        log.warning("failed to update blocklist removing %s", email)


def _read_blocklist() -> set[str]:
    if not BLOCKLIST_FILE.exists():
        return set()
    try:
        return {line.strip() for line in BLOCKLIST_FILE.read_text().splitlines() if line.strip()}
    except OSError:
        return set()


# ── Unblock processing ────────────────────────────────────────────
def _process_unblocks(state: dict[str, EmailState]) -> None:
    """Read enforcer_unblock.txt and restore listed users."""
    if not UNBLOCK_FILE.exists():
        return
    try:
        emails = {line.strip() for line in UNBLOCK_FILE.read_text().splitlines() if line.strip()}
    except OSError:
        return
    if not emails:
        return

    for email in emails:
        # Remove from state
        if email in state:
            del state[email]
        # Remove from blocklist
        _remove_from_blocklist(email)
        # Re-add to xray
        client_entries = _get_client_entries(email)
        restored = sum(1 for e in client_entries
                       if _xray_adduser(e["port"], e["tag"], email, e["uuid"], e["flow"]))
        log.info("UNBLOCK: email=%s — restored to %d/%d protocols",
                 email, restored, len(client_entries))

    # Truncate unblock file
    try:
        UNBLOCK_FILE.write_text("")
    except OSError:
        log.warning("failed to truncate unblock file")


# ── Tiered evaluation ─────────────────────────────────────────────
def evaluate_and_enforce(
    email: str,
    concurrent_ips: set[str],
    state: dict[str, EmailState],
) -> None:
    """Apply graduated enforcement for an email with concurrent IPs."""
    now = _utcnow()
    now_iso = now.isoformat()

    es = state.setdefault(email, EmailState())

    if es.blocked:
        return  # already blocked, enforcer_unblock.txt is the only way out

    # Record this detection
    es.detections.append(now_iso)

    # Prune old detections outside WARNING_WINDOW
    warning_cutoff = (now - timedelta(seconds=WARNING_WINDOW)).isoformat()
    es.detections = [d for d in es.detections if d > warning_cutoff]

    # ── Tier 1: Warning ──
    if es.warning_sent_at is None:
        if len(es.detections) >= WARNING_THRESHOLD:
            _log_violation(email, concurrent_ips, action="warning", severity="warning")
            es.warning_sent_at = now_iso
            log.info("WARNING: email=%s ips=%s — %d detections in window, warning logged",
                     email, sorted(concurrent_ips), len(es.detections))
        return  # not enough detections yet, or just sent warning — no kick

    # ── Tier 3 check (before kick): enough kicks to block? ──
    block_cutoff = (now - timedelta(seconds=BLOCK_WINDOW)).isoformat()
    es.kicks = [k for k in es.kicks if k > block_cutoff]

    if len(es.kicks) >= BLOCK_THRESHOLD:
        _do_block(email)
        _log_violation(email, concurrent_ips, action="block", severity="block")
        es.blocked = True
        log.info("BLOCK: email=%s — %d kicks in %dh window, blocked",
                 email, len(es.kicks), BLOCK_WINDOW // 3600)
        return

    # ── Tier 2: Kick with cooldown ──
    if es.last_kick_at:
        last_kick_dt = datetime.fromisoformat(es.last_kick_at)
        if now - last_kick_dt < timedelta(seconds=KICK_COOLDOWN):
            return  # still in cooldown

    _do_kick(email, concurrent_ips)
    _log_violation(email, concurrent_ips, action="kick", severity="kick")
    es.kicks.append(now_iso)
    es.last_kick_at = now_iso


# ── Main loop ──────────────────────────────────────────────────────
def main() -> None:
    log.info(
        "xray_enforcer v2 started: check=%ds window=%ds slot=%ds "
        "warn=%d/%ds kick_cd=%ds block=%d/%ds",
        CHECK_INTERVAL, WINDOW_SECONDS, SLOT_SECONDS,
        WARNING_THRESHOLD, WARNING_WINDOW,
        KICK_COOLDOWN,
        BLOCK_THRESHOLD, BLOCK_WINDOW,
    )

    if not Path(XRAY_BIN).exists():
        log.error("xray binary not found at %s — exiting", XRAY_BIN)
        sys.exit(1)

    state = _load_state()
    if state:
        log.info("loaded state for %d email(s), %d blocked",
                 len(state), sum(1 for es in state.values() if es.blocked))

    while True:
        try:
            # Process any pending unblocks first
            _process_unblocks(state)

            # Detect concurrent IPs
            concurrent = detect_concurrent_ips()
            for email, ips in concurrent.items():
                evaluate_and_enforce(email, ips, state)

            # Re-enforce blocks: if a blocked user somehow got re-added
            # (e.g. resync raced before reading blocklist), kick again
            for email, es in state.items():
                if es.blocked and email not in concurrent:
                    _do_block(email)

            _save_state(state)
        except Exception:
            log.exception("enforcer tick failed")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
