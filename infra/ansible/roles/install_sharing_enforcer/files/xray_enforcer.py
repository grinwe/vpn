#!/usr/bin/env python3
"""xray_enforcer — local credential sharing enforcer daemon.

Runs on each VPN node as a systemd service.  Every CHECK_INTERVAL
seconds it reads the xray access logs, groups source IPs by email
(= Device.access_username), and when it finds an email with more
than MAX_IPS unique IPs within the WINDOW it:

  1. Reads the user's UUID + flow from the xray config JSON.
  2. Calls ``xray api rmuser`` on every protocol instance where the
     email exists  ->  all active connections with that UUID drop
     instantly (gRPC in-memory removal, no restart needed).
  3. Sleeps RECONNECT_DELAY seconds.
  4. Calls ``xray api adduser`` to restore the user  ->  the
     legitimate client auto-reconnects; the sharer (different IP)
     also reconnects, but the next check cycle catches it again
     within seconds, creating a disruptive loop that makes sharing
     practically unusable.

All violations are appended to VIOLATION_LOG as JSONL so the backend
can pick them up during its periodic traffic-stats SSH collection.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Configuration (env vars) ───────────────────────────────────────
CHECK_INTERVAL = int(os.getenv("ENFORCER_CHECK_INTERVAL", "10"))
WINDOW_SECONDS = int(os.getenv("ENFORCER_WINDOW_SECONDS", "120"))
MAX_IPS = int(os.getenv("ENFORCER_MAX_IPS", "1"))
RECONNECT_DELAY = float(os.getenv("ENFORCER_RECONNECT_DELAY", "2"))

ACCESS_LOG_DIR = Path(os.getenv("ENFORCER_LOG_DIR", "/var/log/xray"))
CONFIG_DIR = Path(os.getenv("ENFORCER_CONFIG_DIR", "/usr/local/etc/xray"))
VIOLATION_LOG = ACCESS_LOG_DIR / "sharing_violations.jsonl"
XRAY_BIN = os.getenv("ENFORCER_XRAY_BIN", "/usr/local/bin/xray")
TAIL_LINES = 3000

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
# Format: 2024/01/01 12:00:00 from 1.2.3.4:12345 accepted tcp:... email: user-1-2
_LINE_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})"
    r"\s+from\s+(\d+\.\d+\.\d+\.\d+):\d+"
    r"\s+accepted\b.*?"
    r"\s+email:\s+(\S+)"
)


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def read_active_ips(window: int = WINDOW_SECONDS) -> dict[str, set[str]]:
    """Return {email: {ip, ...}} from recent access log entries."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
    result: dict[str, set[str]] = defaultdict(set)

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
            result[m.group(3)].add(m.group(2))
    return dict(result)


# ── xray config reader ────────────────────────────────────────────
def _get_client_entries(email: str) -> list[dict]:
    """Find the client object(s) for ``email`` across all protocol configs.

    Returns a list of dicts, each containing ``uuid``, ``flow``,
    ``tag``, and ``port`` — everything needed for rmuser/adduser.
    """
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
    """Remove user from running xray instance.  Returns True on success."""
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
    """Add user back to running xray instance.  Returns True on success."""
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


# ── Enforcement ────────────────────────────────────────────────────
def _log_violation(email: str, ips: set[str], action: str) -> None:
    """Append a JSONL line to the violation log for backend pickup."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "email": email,
        "ips": sorted(ips),
        "ip_count": len(ips),
        "action": action,
    }
    try:
        with open(VIOLATION_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        log.warning("failed to write violation log for %s", email)


def enforce(email: str, ips: set[str]) -> None:
    """Remove user from all xray instances, wait, re-add."""
    entries = _get_client_entries(email)
    if not entries:
        log.warning("no config entry found for email=%s, skip", email)
        return

    # Step 1: remove from all protocols
    removed = 0
    for e in entries:
        if _xray_rmuser(e["port"], e["tag"], email):
            removed += 1
    log.info(
        "SHARING: email=%s ips=%s — removed from %d/%d protocols",
        email, sorted(ips), removed, len(entries),
    )

    # Step 2: wait
    time.sleep(RECONNECT_DELAY)

    # Step 3: re-add to all protocols
    restored = 0
    for e in entries:
        if _xray_adduser(e["port"], e["tag"], email, e["uuid"], e["flow"]):
            restored += 1
    log.info(
        "SHARING: email=%s — restored to %d/%d protocols",
        email, restored, len(entries),
    )

    _log_violation(email, ips, f"rmuser+adduser (rm={removed} add={restored})")


# ── Main loop ──────────────────────────────────────────────────────
def main() -> None:
    log.info(
        "xray_enforcer started: check=%ds window=%ds max_ips=%d delay=%.1fs",
        CHECK_INTERVAL, WINDOW_SECONDS, MAX_IPS, RECONNECT_DELAY,
    )

    # Sanity check: does xray binary exist?
    if not Path(XRAY_BIN).exists():
        log.error("xray binary not found at %s — exiting", XRAY_BIN)
        sys.exit(1)

    while True:
        try:
            email_ips = read_active_ips(WINDOW_SECONDS)
            for email, ips in email_ips.items():
                if len(ips) > MAX_IPS:
                    enforce(email, ips)
        except Exception:
            log.exception("enforcer tick failed")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
