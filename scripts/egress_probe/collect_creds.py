#!/usr/bin/env python3
"""Собрать креды для пробников из выгрузки ``GET /api/users/<id>``.

    # все 16 кредов тест-устройства (4 ноды × 4 протокола)
    ./collect_creds.py --user user1.json --nodes nodes.json --sub 21 --device 11324 > creds.json

    # покрыть ноды, которых нет у тест-устройства: по одному ПРОСТАИВАЮЩЕМУ
    # устройству подписки на ноду (active, last_seen старше --idle-days)
    ./collect_creds.py --user user1.json --nodes nodes.json --sub 21 --idle-cover > creds.json

    # ВСЕ простаивающие устройства на ноде: у разных устройств одной RU relay-ноды
    # разные exit-ы (выбор per device) — так покрываются все линки relay→exit
    ./collect_creds.py ... --all-idle --only-nodes dc-ru-01 > creds.json

    # только перечисленные ноды/протоколы
    ... --only-nodes dc-ru-01,ufo-ru-02 --only-protos vless-reality

API не отдаёт is_active креда: у active-устройства в выборку могут попасть
деактивированные креды (после swap_node_out и т.п.). ERR на таком креде — не
мёртвая нога; сверять SQL ``SELECT is_active, leg_published FROM credentials
WHERE id=<id>``.

Нода определяется резолвом хоста из URI и сверкой с ``host`` из ``GET /api/nodes``
(ws-cdn/xhttp-хосты — DNS-only домены на IP ноды): у CredentialOut нет node_id.
Живые устройства (свежий last_seen) --idle-cover не берёт — не мешаем людям,
даже при выключенном sharing-энфорсере.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone


def load_sub(path: str, sub_id: int) -> dict:
    data = json.load(open(path))
    subs = data if isinstance(data, list) else data.get("subscriptions", data)
    for s in subs:
        if s["id"] == sub_id:
            return s
    raise SystemExit(f"subscription {sub_id} not found in {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="выгрузка GET /api/users/<id>")
    ap.add_argument("--nodes", required=True, help="выгрузка GET /api/nodes")
    ap.add_argument("--sub", type=int, required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--device", type=int)
    g.add_argument("--idle-cover", action="store_true",
                   help="по одному простаивающему устройству на ноду")
    g.add_argument("--all-idle", action="store_true",
                   help="все простаивающие устройства (все exit-ы relay-ноды)")
    ap.add_argument("--idle-days", type=int, default=3)
    ap.add_argument("--exclude-device", type=int, action="append", default=[])
    ap.add_argument("--only-nodes", default="")
    ap.add_argument("--only-protos", default="")
    a = ap.parse_args()

    nodes = {n["host"]: n["name"] for n in json.load(open(a.nodes))}
    sub = load_sub(a.user, a.sub)
    now = datetime.now(timezone.utc)
    devices = {d["id"]: d for d in sub.get("devices", [])}
    cache: dict[str, str] = {}

    def node_of(host: str) -> str:
        if host not in cache:
            try:
                ip = socket.gethostbyname(host)
            except OSError:
                ip = ""
            cache[host] = nodes.get(ip) or nodes.get(host) or f"?{host}"
        return cache[host]

    def idle(dev: dict) -> bool:
        ls = dev.get("last_seen_at")
        if not ls:
            return True
        return now - datetime.fromisoformat(ls.replace("Z", "+00:00")) > timedelta(days=a.idle_days)

    rows = []
    for c in sub["credentials"]:
        did = c.get("device_id")
        dev = devices.get(did)
        if dev is None or dev.get("status") != "active" or did in a.exclude_device:
            continue
        host = urllib.parse.urlsplit(c["config_text"]).hostname or ""
        rows.append({"id": c["id"], "proto": c["proto"], "uri": c["config_text"],
                     "node": node_of(host), "device": did, "idle": idle(dev)})

    if a.device:
        out = [r for r in rows if r["device"] == a.device]
    elif a.all_idle:
        out = [r for r in rows if r["idle"]]
    else:
        out, taken = [], {}
        for r in sorted(rows, key=lambda r: (r["node"], r["device"])):
            if not r["idle"]:
                continue
            dev = taken.setdefault(r["node"], r["device"])
            if dev == r["device"]:
                out.append(r)
    if a.only_nodes:
        keep = set(a.only_nodes.split(","))
        out = [r for r in out if r["node"] in keep]
    if a.only_protos:
        keep = set(a.only_protos.split(","))
        out = [r for r in out if r["proto"] in keep]
    for r in out:
        r.pop("idle", None)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    cover = sorted({(r["node"], r["device"]) for r in out})
    print(f"\n{len(out)} creds: {cover}", file=sys.stderr)


if __name__ == "__main__":
    main()
