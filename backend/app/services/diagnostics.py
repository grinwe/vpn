"""Local-side staged path probe for node/exit diagnostics (ASK-1).

Runs from the worker/backend container BEFORE any on-host ansible play:
``ping → tcp:port → ssh-pong`` (+ ``traceroute`` for context when the path
is broken). Each stage emits a check in the SAME
``{name, status, latency_ms, message, details}`` contract the relay-link
diagnose already produces, so the existing ``<DiagnoseResult>`` admin UI
renders them with zero changes.

The caller uses :attr:`PathProbeResult.ssh_ok` to decide whether to run the
on-host playbook at all — a fully unreachable host short-circuits to a
synthetic ``skip`` checklist instead of burning an ansible UNREACHABLE
(the exact case ``_auto_diagnose_unreachable_nodes`` mishandles today).

SSH reuses the provisioning Ed25519 key + constants from ``traffic_stats``,
same as ``relay_link_health``. ``ping``/``traceroute`` shell out; if the
binary is missing in the container the stage degrades to ``skip`` rather
than failing the whole probe (add iputils-ping + traceroute to the worker
image to light them up).
"""
from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field

from .traffic_stats import SSH_CONNECT_TIMEOUT, SSH_PORT_DEFAULT, SSH_USER

logger = logging.getLogger(__name__)

# Status vocabulary matches admin/src/diagnoseResult.tsx: ok | warn | fail | skip | info.
Check = dict


def _check(
    name: str,
    status: str,
    *,
    latency_ms: int | None = None,
    message: str = "",
    details: dict | None = None,
) -> Check:
    return {
        "name": name,
        "status": status,
        "latency_ms": latency_ms,
        "message": message,
        "details": details or {},
    }


@dataclass
class PathProbeResult:
    """Outcome of the local staged probe — feeds task.result['checks']."""

    checks: list[Check] = field(default_factory=list)
    ping_ok: bool = False
    ssh_ok: bool = False
    # True когда ssh-стадию НЕ смогли выполнить (нет paramiko / нет файла ключа),
    # а не «проверили — недоступно». Отличает конфиг-ошибку контроллера от
    # реальной недоступности хоста: при skip вызывающий (reachability-тик) не
    # должен переводить цель в unreachable — иначе потеря одного ключа кладёт
    # весь флот в ложный DOWN. См. finding #97.
    ssh_skipped: bool = False
    summary: str = ""


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# rtt line: "rtt min/avg/max/mdev = 0.3/0.5/0.8/0.1 ms"
_PING_AVG_RE = re.compile(r"=\s*[\d.]+/([\d.]+)/")
_PING_LOSS_RE = re.compile(r"(\d+)% packet loss")


def _probe_ping(host: str, *, count: int = 3, per_reply_timeout_s: int = 2) -> Check:
    cmd = ["ping", "-c", str(count), "-W", str(per_reply_timeout_s), host]
    try:
        cp = _run(cmd, timeout=count * (per_reply_timeout_s + 1) + 3)
    except FileNotFoundError:
        return _check("ping", "skip", message="ping не установлен в контейнере")
    except subprocess.TimeoutExpired:
        return _check(
            "ping", "fail", message=f"ping timeout — host не отвечает (>{count * per_reply_timeout_s}s)"
        )
    out = cp.stdout or ""
    loss_m = _PING_LOSS_RE.search(out)
    loss = int(loss_m.group(1)) if loss_m else (0 if cp.returncode == 0 else 100)
    if loss >= 100 or cp.returncode != 0:
        return _check(
            "ping", "fail", message="host не пингуется (100% loss)", details={"raw": out[-400:]}
        )
    avg_m = _PING_AVG_RE.search(out)
    avg = float(avg_m.group(1)) if avg_m else None
    status = "warn" if loss > 0 else "ok"
    msg = (f"avg {avg:.1f} ms" if avg is not None else "reachable") + (
        f", потери {loss}%" if loss else ""
    )
    return _check("ping", status, latency_ms=int(avg) if avg is not None else None, message=msg)


def _probe_tcp(host: str, port: int, *, label: str | None = None) -> tuple[bool, Check]:
    name = label or f"tcp:{port}"
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=SSH_CONNECT_TIMEOUT):
            pass
    except OSError as exc:
        return False, _check(
            name, "fail", message=f"порт {port} закрыт/недоступен ({exc.__class__.__name__})"
        )
    lat = int((time.monotonic() - t0) * 1000)
    return True, _check(name, "ok", latency_ms=lat, message=f"порт {port} открыт")


def _probe_ssh(host: str, ssh_port: int) -> tuple[bool, Check]:
    try:
        import paramiko  # noqa: WPS433 — lazy import, как в relay_link_health
    except ImportError:
        return False, _check("ssh", "skip", message="paramiko не установлен")

    key_path = (
        os.getenv("ANSIBLE_PRIVATE_KEY_FILE")
        or os.getenv("PROVISIONING_SSH_KEY")
        or "/run/secrets/provisioning_key"
    )
    if not os.path.exists(key_path):
        return False, _check("ssh", "skip", message=f"ssh-ключ не найден: {key_path}")

    t0 = time.monotonic()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=ssh_port or SSH_PORT_DEFAULT,
            username=SSH_USER,
            pkey=paramiko.Ed25519Key.from_private_key_file(key_path),
            timeout=SSH_CONNECT_TIMEOUT,
            banner_timeout=SSH_CONNECT_TIMEOUT,
            auth_timeout=SSH_CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        _stdin, stdout, _stderr = client.exec_command("echo pong", timeout=SSH_CONNECT_TIMEOUT)
        out = stdout.read().decode(errors="replace").strip()
        lat = int((time.monotonic() - t0) * 1000)
        if "pong" in out:
            return True, _check("ssh", "ok", latency_ms=lat, message="ssh handshake + exec ok")
        return False, _check("ssh", "fail", latency_ms=lat, message=f"ssh exec без pong: {out[:120]}")
    except Exception as exc:  # noqa: BLE001 — любая транспортная ошибка = ssh недоступен
        lat = int((time.monotonic() - t0) * 1000)
        return False, _check(
            "ssh", "fail", latency_ms=lat,
            message=f"ssh недоступен: {exc.__class__.__name__}: {str(exc)[:120]}",
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _resolve_host_ips(host: str) -> set[str]:
    """Все IP (v4/v6) для ``host`` через ``getaddrinfo``.

    Пустой set при ошибке резолва. Если ``host`` уже IP-литерал —
    ``getaddrinfo`` вернёт его же, так что вызывающий получит {host}.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return set()
    return {info[4][0] for info in infos}


def _probe_traceroute(host: str, *, max_hops: int = 12, wait_s: int = 2) -> Check:
    cmd = ["traceroute", "-n", "-m", str(max_hops), "-w", str(wait_s), host]
    try:
        cp = _run(cmd, timeout=max_hops * wait_s + 5)
    except FileNotFoundError:
        return _check("traceroute", "skip", message="traceroute не установлен")
    except subprocess.TimeoutExpired:
        return _check("traceroute", "info", message="traceroute timeout")
    out = cp.stdout or ""
    hop_lines = [ln for ln in out.splitlines()[1:] if ln.strip()]
    last_responding = 0
    for ln in hop_lines:
        if "* * *" in ln:
            continue
        m = re.match(r"\s*(\d+)", ln)
        if m:
            last_responding = int(m.group(1))
    # host у нод — как правило FQDN, а вывод traceroute с ``-n`` числовой:
    # доменное имя в последнем hop'е не встретится НИКОГДА, и сравнение
    # ``host in hop`` давало ложное «трасса НЕ доходит» на любой доменной
    # ноде (finding #103). Сравниваем по резолвнутым IP; при провале резолва
    # деградируем в старое строковое сравнение и помечаем это в message.
    target_ips = _resolve_host_ips(host)
    resolve_failed = not target_ips
    last_hop = hop_lines[-1] if hop_lines else ""
    if not hop_lines:
        reached = False
    elif target_ips:
        reached = any(ip in last_hop for ip in target_ips)
    else:
        reached = host in last_hop
    status = "info" if reached else "warn"
    msg = f"{len(hop_lines)} hops, последний отвечающий — hop {last_responding}"
    if not reached:
        msg += "; трасса НЕ доходит до host"
        if resolve_failed:
            msg += " (резолв имени не удался — сравнение по строке)"
    return _check("traceroute", status, message=msg, details={"raw": out[-800:]})


def run_local_path_probe(
    host: str,
    *,
    ssh_port: int = 22,
    extra_tcp_ports: list[int] | None = None,
    traceroute_on_fail: bool = True,
) -> PathProbeResult:
    """Staged controller→host reachability probe; ``ssh_ok`` gates on-host play.

    Order: ping → extra tcp ports → tcp:ssh → ssh-pong, then traceroute only
    when ssh failed (to localize where the path dies). Never raises — every
    stage is captured as a check.
    """
    result = PathProbeResult()

    ping = _probe_ping(host)
    result.checks.append(ping)
    result.ping_ok = ping["status"] in ("ok", "warn")

    for port in extra_tcp_ports or []:
        result.checks.append(_probe_tcp(host, port)[1])

    _tcp_ok, tcp_ssh_check = _probe_tcp(host, ssh_port, label=f"tcp:{ssh_port} (ssh)")
    result.checks.append(tcp_ssh_check)

    ssh_ok, ssh_check = _probe_ssh(host, ssh_port)
    result.checks.append(ssh_check)
    result.ssh_ok = ssh_ok
    # skip = ssh-стадию не смогли выполнить (нет paramiko / нет ключа), а не
    # доказанная недоступность. Вызывающий использует флаг, чтобы не метить
    # цель unreachable по конфиг-ошибке контроллера (finding #97).
    result.ssh_skipped = ssh_check["status"] == "skip"

    if not ssh_ok and traceroute_on_fail:
        result.checks.append(_probe_traceroute(host))

    if ssh_ok:
        result.summary = "host доступен по ssh"
    elif result.ping_ok:
        result.summary = "host пингуется, но ssh недоступен"
    else:
        result.summary = "host недоступен (ни ping, ни ssh)"
    return result


def skip_checks(names: list[str], *, reason: str) -> list[Check]:
    """Synthetic ``skip`` checks for on-host stages we never reached.

    Used when the staged probe short-circuits (ssh down) so the checklist
    still shows what WOULD have run, greyed out, instead of an empty result.
    """
    return [_check(n, "skip", message=reason) for n in names]


_STATUS_ICON = {"ok": "✅", "fail": "❌", "warn": "⚠️", "skip": "⏭", "info": "ℹ️"}


def summarize_checks(checks: list[Check], *, max_lines: int = 8) -> str:
    """Compact icon-per-line summary of a checks[] list for a Telegram push.

    Fails/warns first (the operator cares about what's broken), then the
    rest, capped at ``max_lines`` with a "…+N" tail so a long checklist
    stays readable in a push.
    """
    if not checks:
        return "нет чеков"
    order = {"fail": 0, "warn": 1, "skip": 2, "info": 3, "ok": 4}
    ordered = sorted(checks, key=lambda c: order.get(c.get("status", ""), 9))
    lines = []
    for c in ordered[:max_lines]:
        icon = _STATUS_ICON.get(c.get("status", ""), "•")
        msg = c.get("message") or ""
        suffix = f" — {msg}" if msg and c.get("status") in ("fail", "warn") else ""
        lines.append(f"{icon} {c.get('name', '?')}{suffix}")
    if len(ordered) > max_lines:
        lines.append(f"…+{len(ordered) - max_lines}")
    return "\n".join(lines)
