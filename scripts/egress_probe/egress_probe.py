#!/usr/bin/env python3
"""Матрица «кред → реальный выход»: TCP, UDP (STUN), IPv6, взгляд chatgpt.com.

Вход: JSON-файл со списком {"id", "proto", "uri", "node"} (креды тестового
устройства из админ-API). Для каждого кред поднимается локальный клиент
(xray для vless-*, hysteria для hysteria2) с SOCKS-портом и UDP-форвардом на
STUN, и через него снимаются:

  * tcp4   — https://www.cloudflare.com/cdn-cgi/trace  (ip=, loc=)
  * openai — https://chatgpt.com/cdn-cgi/trace         (что видит edge OpenAI)
  * v6     — https://[2606:4700:4700::1111]/cdn-cgi/trace (выход по IPv6)
  * udp    — STUN binding к stun.cloudflare.com:3478 (публичный IP UDP-выхода)

Печатает JSON-строку на кред. Ничего не пишет в прод: только подключения.

    ./fetch_clients.sh                                  # один раз
    ./egress_probe.py creds.json > probe.jsonl
    ./show_probe.py --nodes nodes.json --exits exits.json probe.jsonl

Клиенты берутся из ${EGRESS_BIN_DIR:-~/.cache/vpn-egress-probe}. Временные
конфиги с кредами — в приватном temp-каталоге (0700), после прогона удаляются.
Классификатор агентской сессии не даёт исполнять скачанные бинари — запускает
оператор.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse

BIN_DIR = os.path.expanduser(os.getenv("EGRESS_BIN_DIR", "~/.cache/vpn-egress-probe"))
XRAY = os.path.join(BIN_DIR, "xray")
HY = os.path.join(BIN_DIR, "hysteria")
# Конфиги клиентов содержат креды — только в приватный каталог, не в репо.
WORK_DIR = tempfile.mkdtemp(prefix="egress-probe-")
os.chmod(WORK_DIR, 0o700)
STUN_HOST, STUN_PORT = "stun.cloudflare.com", 3478
CLEAN_ENV = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}


# ── конвертеры URI → клиентский конфиг ────────────────────────────────────

def _q(qs: dict, key: str, default: str = "") -> str:
    return (qs.get(key) or [default])[0]


def vless_outbound(uri: str) -> dict:
    u = urllib.parse.urlsplit(uri)
    qs = urllib.parse.parse_qs(u.query)
    net = _q(qs, "type", "tcp")
    sec = _q(qs, "security", "none")
    user = {"id": urllib.parse.unquote(u.username or ""), "encryption": _q(qs, "encryption", "none")}
    if _q(qs, "flow"):
        user["flow"] = _q(qs, "flow")
    stream: dict = {"network": net, "security": sec}
    sni = _q(qs, "sni") or _q(qs, "host") or u.hostname
    if sec == "reality":
        stream["realitySettings"] = {
            "serverName": sni, "fingerprint": _q(qs, "fp", "chrome"),
            "publicKey": _q(qs, "pbk"), "shortId": _q(qs, "sid"),
            "spiderX": urllib.parse.unquote(_q(qs, "spx", "/")),
        }
    elif sec == "tls":
        tls = {"serverName": sni, "fingerprint": _q(qs, "fp", "chrome")}
        if _q(qs, "alpn"):
            tls["alpn"] = urllib.parse.unquote(_q(qs, "alpn")).split(",")
        if _q(qs, "allowInsecure") in ("1", "true"):
            tls["allowInsecure"] = True
        stream["tlsSettings"] = tls
    path = urllib.parse.unquote(_q(qs, "path", "/"))
    host = _q(qs, "host")
    if net == "ws":
        stream["wsSettings"] = {"path": path, **({"host": host} if host else {})}
    elif net in ("xhttp", "splithttp"):
        xs = {"path": path, "mode": _q(qs, "mode", "auto")}
        if host:
            xs["host"] = host
        if _q(qs, "extra"):
            try:
                xs["extra"] = json.loads(urllib.parse.unquote(_q(qs, "extra")))
            except ValueError:
                pass
        stream["network"] = "xhttp"
        stream["xhttpSettings"] = xs
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": _q(qs, "serviceName")}
    return {
        "tag": "proxy", "protocol": "vless",
        "settings": {"vnext": [{"address": u.hostname, "port": u.port or 443, "users": [user]}]},
        "streamSettings": stream,
    }


def xray_config(uri: str, socks_port: int, udp_port: int) -> dict:
    stun_ip = socket.gethostbyname(STUN_HOST)
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "socks", "listen": "127.0.0.1", "port": socks_port, "protocol": "socks",
             "settings": {"udp": True}},
            {"tag": "stun", "listen": "127.0.0.1", "port": udp_port, "protocol": "dokodemo-door",
             "settings": {"address": stun_ip, "port": STUN_PORT, "network": "udp"}},
        ],
        "outbounds": [vless_outbound(uri)],
    }


def hy_config(uri: str, socks_port: int, udp_port: int) -> dict:
    u = urllib.parse.urlsplit(uri)
    qs = urllib.parse.parse_qs(u.query)
    auth = urllib.parse.unquote(u.username or "")
    if u.password:
        auth = f"{auth}:{urllib.parse.unquote(u.password)}"
    server = f"{u.hostname}:{u.port or 443}"
    mport = _q(qs, "mport")
    if mport:
        server = f"{u.hostname}:{mport}"
    cfg: dict = {
        "server": server,
        "auth": auth,
        "tls": {"sni": _q(qs, "sni") or u.hostname,
                "insecure": _q(qs, "insecure") in ("1", "true")},
        "socks5": {"listen": f"127.0.0.1:{socks_port}"},
        "udpForwarding": [{"listen": f"127.0.0.1:{udp_port}",
                           "remote": f"{socket.gethostbyname(STUN_HOST)}:{STUN_PORT}",
                           "timeout": "10s"}],
    }
    if _q(qs, "obfs"):
        cfg["obfs"] = {"type": _q(qs, "obfs"),
                       _q(qs, "obfs"): {"password": urllib.parse.unquote(_q(qs, "obfs-password"))}}
    if _q(qs, "pinSHA256"):
        cfg["tls"]["pinSHA256"] = _q(qs, "pinSHA256")
    return cfg


# ── измерения ─────────────────────────────────────────────────────────────

def trace(socks_port: int, url: str, timeout: int = 20) -> dict:
    res = subprocess.run(
        ["curl", "-sS", "-m", str(timeout), "--socks5-hostname", f"127.0.0.1:{socks_port}", url],
        capture_output=True, text=True, env=CLEAN_ENV,
    )
    if res.returncode != 0:
        return {"err": (res.stderr.strip() or f"rc={res.returncode}")[-160:]}
    kv = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
    return {"ip": kv.get("ip"), "loc": kv.get("loc"), "colo": kv.get("colo"), "http": kv.get("http")}


def stun(udp_port: int, timeout: float = 6.0) -> dict:
    txid = os.urandom(12)
    req = struct.pack("!HHI12s", 0x0001, 0, 0x2112A442, txid)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        for _ in range(3):
            s.sendto(req, ("127.0.0.1", udp_port))
            try:
                data, _ = s.recvfrom(2048)
            except socket.timeout:
                continue
            i = 20
            while i + 4 <= len(data):
                atype, alen = struct.unpack("!HH", data[i:i + 4])
                val = data[i + 4:i + 4 + alen]
                if atype in (0x0020, 0x0001) and len(val) >= 8 and val[1] == 0x01:
                    port = struct.unpack("!H", val[2:4])[0]
                    raw = val[4:8]
                    if atype == 0x0020:
                        port ^= 0x2112
                        raw = bytes(b ^ m for b, m in zip(raw, b"\x21\x12\xa4\x42"))
                    return {"ip": socket.inet_ntoa(raw), "port": port}
                i += 4 + alen + ((4 - alen % 4) % 4)
            return {"err": "no mapped address"}
        return {"err": "timeout"}
    finally:
        s.close()


def wait_port(port: int, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def probe(cred: dict, idx: int) -> dict:
    socks_port, udp_port = 21000 + idx * 2, 21001 + idx * 2
    out = {"id": cred.get("id"), "device": cred.get("device"), "node": cred.get("node"), "proto": cred.get("proto")}
    is_hy = cred["uri"].startswith(("hysteria2://", "hy2://"))
    try:
        cfg = hy_config(cred["uri"], socks_port, udp_port) if is_hy else xray_config(cred["uri"], socks_port, udp_port)
    except Exception as exc:  # noqa: BLE001
        out["err"] = f"config: {exc}"
        return out
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, dir=WORK_DIR) as f:
        json.dump(cfg, f)  # hysteria читает и JSON
        path = f.name
    cmd = [HY, "client", "-c", path] if is_hy else [XRAY, "run", "-c", path]
    log = open(path + ".log", "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=CLEAN_ENV)
    try:
        if not wait_port(socks_port):
            out["err"] = "client did not start"
            return out
        time.sleep(1.0)
        out["tcp4"] = trace(socks_port, "https://www.cloudflare.com/cdn-cgi/trace")
        out["openai"] = trace(socks_port, "https://chatgpt.com/cdn-cgi/trace")
        out["v6"] = trace(socks_port, "https://[2606:4700:4700::1111]/cdn-cgi/trace", timeout=12)
        out["udp"] = stun(udp_port)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        try:
            out["client_log_tail"] = open(path + ".log").read()[-300:]
        except OSError:
            pass
        for p in (path, path + ".log"):
            try:
                os.unlink(p)
            except OSError:
                pass
    return out


def main() -> None:
    creds = json.load(open(sys.argv[1]))
    try:
        for i, cred in enumerate(creds):
            print(json.dumps(probe(cred, i), ensure_ascii=False), flush=True)
    finally:
        try:
            os.rmdir(WORK_DIR)
        except OSError:
            pass


if __name__ == "__main__":
    main()
