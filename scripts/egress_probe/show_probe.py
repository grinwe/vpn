#!/usr/bin/env python3
"""Матрица из вывода egress_probe.py: IP выхода подписан exit-ом/нодой.

    ./show_probe.py --nodes nodes.json --exits exits.json probe.jsonl [probe2.jsonl ...]

Норма для RU relay-ноды: tcp/udp/openai = exit (не NODE:<ру-нода>), v6 — обрыв
(v6-выхода нет, «ERR …unexpected eof while reading»). ERR в tcp/openai/udp —
нога не работает (или кред деактивирован: #<id> → SQL is_active). Для зарубежной direct-ноды — сама нода. «NODE:<ру-нода>» на
иностранном трафике = утечка РУ-IP.
"""
import argparse
import json


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", required=True)
    ap.add_argument("--exits", required=True)
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    ex = json.load(open(a.exits))
    ex = ex if isinstance(ex, list) else ex.get("items", ex)
    exits = {x.get("public_ip") or x.get("host"): x["name"] for x in ex}
    nodes = {n["host"]: n["name"] for n in json.load(open(a.nodes))}

    def who(ip):
        if not ip:
            return "-"
        return exits.get(ip) or (nodes.get(ip) and "NODE:" + nodes[ip]) or ip

    def fmt(r):
        if not r:
            return "-"
        if "err" in r:
            # хвост, а не голова: «...unexpected eof while reading» / «timed out» — в конце
            return "ERR …" + r["err"][-38:]
        return f"{who(r.get('ip'))}/{r.get('loc', '')}"

    rows = [json.loads(line) for f in a.files for line in open(f) if line.strip()]
    for r in sorted(rows, key=lambda r: (r.get("node") or "", r.get("proto") or "")):
        if "tcp4" not in r:
            print(f"{r.get('node', '?'):12} {r.get('proto', '?'):14} #{r.get('id')} ERR {r.get('err')} | {r.get('client_log_tail', '')[-150:]!r}")
            continue
        u = r.get("udp") or {}
        udp = ("ERR " + u["err"]) if "err" in u else who(u.get("ip"))
        print(f"{r['node']:12} {r['proto']:14} #{r.get('id')} tcp={fmt(r.get('tcp4')):24} "
              f"openai={fmt(r.get('openai')):24} udp={udp:22} v6={fmt(r.get('v6'))}")


if __name__ == "__main__":
    main()
