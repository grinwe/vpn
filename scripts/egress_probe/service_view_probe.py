#!/usr/bin/env python3
"""Что видят сами сервисы через каждый выход: Google (Gemini API + YouTube GL) и OpenAI API.

Вход: JSON-список кредов (как у egress_probe). Для каждого поднимается клиент
(xray/hysteria) и через его SOCKS:

  * cf      — cloudflare trace (ip/loc) — чей это выход
  * gemini  — generativelanguage.googleapis.com с заведомо неверным ключом:
              «User location is not supported» → Google режет страну;
              «API key not valid» → страна поддерживается
  * yt_gl   — youtube.com, "GL":"XX" — страна по мнению Google
  * openai  — api.openai.com/v1/models без ключа:
              unsupported_country_region_territory → OpenAI режет страну;
              иначе (401 missing key) → страна поддерживается

Ключи не нужны и не отправляются. Ничего не пишет в прод.

    ./service_view_probe.py creds.json        # хватит одного reality-креда на выход

Важно: cloudflare trace (и chatgpt.com/cdn-cgi/trace) — гео Cloudflare, а не
Google/MaxMind. 29.09.2026 CF видел CZ/NL/DK, а Google — RU (Gemini-приложение
«недоступно в вашей стране»).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import os

import egress_probe as e


def fetch(port: int, url: str, extra: list[str] | None = None, timeout: int = 20) -> tuple[str, str]:
    r = subprocess.run(
        ["curl", "-sS", "-m", str(timeout), "-w", "\n%{http_code}",
         "--socks5-hostname", f"127.0.0.1:{port}", *(extra or []), url],
        capture_output=True, text=True, env=e.CLEAN_ENV,
    )
    if r.returncode != 0:
        return "ERR", (r.stderr.strip() or f"rc={r.returncode}")[-120:]
    body, _, code = r.stdout.rpartition("\n")
    return code, body


def gemini(port: int) -> str:
    code, body = fetch(port, "https://generativelanguage.googleapis.com/v1beta/models?key=AIzaInvalidKeyForGeoCheck000000000000")
    if code == "ERR":
        return "ERR " + body
    if "location is not supported" in body.lower():
        return f"🔴 BLOCKED ({code})"
    if "api key not valid" in body.lower() or "api_key_invalid" in body.lower():
        return f"ok ({code})"
    return f"? {code} {body[:80]!r}"


def openai(port: int) -> str:
    code, body = fetch(port, "https://api.openai.com/v1/models")
    if code == "ERR":
        return "ERR " + body
    if "unsupported_country" in body:
        return f"🔴 BLOCKED ({code})"
    if code in ("401", "403") and ("api key" in body.lower() or "authorization" in body.lower() or "missing" in body.lower()):
        return f"ok ({code})"
    return f"? {code} {body[:80]!r}"


def yt_gl(port: int) -> str:
    code, body = fetch(port, "https://www.youtube.com/?hl=en", ["-H", "Accept-Language: en"])
    if code == "ERR":
        return "ERR " + body
    m = re.search(r'"GL"\s*:\s*"([A-Z]{2})"', body)
    return m.group(1) if m else f"? {code}"


def probe(cred: dict, idx: int) -> dict:
    socks_port, udp_port = 23000 + idx * 2, 23001 + idx * 2
    out = {"node": cred.get("node"), "proto": cred.get("proto")}
    is_hy = cred["uri"].startswith(("hysteria2://", "hy2://"))
    cfg = e.hy_config(cred["uri"], socks_port, udp_port) if is_hy else e.xray_config(cred["uri"], socks_port, udp_port)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, dir=e.WORK_DIR) as f:
        json.dump(cfg, f)
        path = f.name
    proc = subprocess.Popen([e.HY, "client", "-c", path] if is_hy else [e.XRAY, "run", "-c", path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=e.CLEAN_ENV)
    try:
        if not e.wait_port(socks_port):
            out["err"] = "client did not start"
            return out
        time.sleep(1.0)
        out["cf"] = e.trace(socks_port, "https://www.cloudflare.com/cdn-cgi/trace")
        out["gemini"] = gemini(socks_port)
        out["yt_gl"] = yt_gl(socks_port)
        out["openai"] = openai(socks_port)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.unlink(path)
    return out


def main() -> None:
    creds = json.load(open(sys.argv[1]))
    print(f"{'node':12} {'proto':14} {'cf-exit':22} {'gemini':22} {'yt_GL':6} openai")
    try:
        _run(creds)
    finally:
        try:
            os.rmdir(e.WORK_DIR)
        except OSError:
            pass


def _run(creds: list) -> None:
    for i, c in enumerate(creds):
        r = probe(c, i)
        if "err" in r:
            print(f"{r['node']:12} {r['proto']:14} ERR {r['err']}")
            continue
        cf = r["cf"]
        cfs = f"{cf.get('ip')}/{cf.get('loc')}" if "ip" in cf else "ERR " + cf.get("err", "")[:18]
        print(f"{r['node']:12} {r['proto']:14} {cfs:22} {r['gemini']:22} {r['yt_gl']:6} {r['openai']}", flush=True)


if __name__ == "__main__":
    main()
