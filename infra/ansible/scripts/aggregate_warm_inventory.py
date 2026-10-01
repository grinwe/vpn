#!/usr/bin/env python3
"""Свести warm-pool данные с конфигов нод и runtime-state в credentials.json.

Архитектура напоминалка: backend кладёт warm-bundles в xray под именами
``warm-<node_id>-<8hex>`` (см. warm_pool.py:_generate_warm_username).
Когда юзер покупает подписку, warm-bundle binding'ится в БД
(``Credential.subscription_id``) БЕЗ переименования в xray. Значит
имя на ноде остаётся warm-*, а связь с юзером ЖИВЁТ только в DB.
Если DB потеряна — нужно восстановить:
1. ВСЕ warm-bundles из config.json (UUID, password, email) — это база
   таблицы ``credentials``.
2. Какие из них РЕАЛЬНО used юзерами — из xray runtime stats и access-log
   (см. recover_xray_runtime.yml). Они получают state="assigned" с пустым
   subscription_id, ждут /restore-flow для привязки к real-юзеру.
3. Какие неиспользованные — state="warm", чистый пул для нового backend.

Output ``credentials.json``:
[
  {
    "node": "ru-pq-01",
    "node_id_legacy": 9,
    "email": "warm-9-0d46dc82",
    "uuid": "abcdef...",
    "config_tag": "vless-reality",
    "state": "assigned" | "warm",
    "traffic_seen": true | false,        # из xray stats
    "access_seen": true | false          # из access-логов
  },
  ...
]

Также печатает map `node_name -> node_id_legacy` чтобы юзер мог передать
в generate_restore_sql.py через --node-id-map.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WARM_EMAIL_RE = re.compile(r"^warm-(\d+)-([0-9a-f]+)$")


def load_runtime_emails(runtime_dir: Path) -> dict[str, dict[str, set[str]]]:
    """host -> {"stats": set[email], "access": set[email]}.

    Объединяет reality_stats_emails + xhttp_stats_emails в stats,
    access_emails_seen в access.
    """
    out: dict[str, dict[str, set[str]]] = {}
    if not runtime_dir.is_dir():
        return out
    for p in sorted(runtime_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        host = data.get("host") or p.stem
        out[host] = {
            "stats": set(data.get("reality_stats_emails") or [])
                   | set(data.get("xhttp_stats_emails") or []),
            "access": set(data.get("access_emails_seen") or []),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recovered-dir", required=True, type=Path,
        help="Где лежат <host>__config*.json (slurp xray-конфигов).",
    )
    parser.add_argument(
        "--runtime-dir", required=True, type=Path,
        help="Где лежат <host>.json (recover_xray_runtime.yml output).",
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Куда писать credentials.json.",
    )
    args = parser.parse_args()

    runtime = load_runtime_emails(args.runtime_dir)

    # email -> {uuid, config_tag, node, ...} — дедуп по email (одинаковый
    # email на reality и xhttp одного хоста = один bundle).
    creds: dict[tuple[str, str], dict] = {}
    node_id_map: dict[str, int] = {}

    for cfg_path in sorted(args.recovered_dir.glob("*__config*.json")):
        host, _, _proto_file = cfg_path.name.partition("__")
        try:
            data = json.loads(cfg_path.read_text())
        except Exception:
            continue
        for inbound in data.get("inbounds") or []:
            tag = inbound.get("tag") or inbound.get("protocol") or "?"
            clients = (inbound.get("settings") or {}).get("clients") or []
            for c in clients:
                email = c.get("email") or ""
                m = WARM_EMAIL_RE.match(email)
                if not m:
                    continue
                node_id = int(m.group(1))
                # legacy node_id из email-префикса. Должен совпадать у
                # всех warm-* записей с одного хоста.
                prev = node_id_map.get(host)
                if prev is not None and prev != node_id:
                    print(
                        f"[warn] {host}: смесь node_id_legacy ({prev} vs {node_id}) "
                        f"в email-префиксах — может быть reprovision history. "
                        f"Берём первый: {prev}.",
                        file=sys.stderr,
                    )
                else:
                    node_id_map[host] = node_id

                key = (host, email)
                existing = creds.get(key)
                if existing is None:
                    creds[key] = {
                        "node": host,
                        "node_id_legacy": node_id,
                        "email": email,
                        "uuid": c.get("id"),
                        "config_tag": tag,
                        "config_tags": [tag],
                    }
                else:
                    if tag not in existing["config_tags"]:
                        existing["config_tags"].append(tag)

    # Аннотируем state через runtime/access.
    state_assigned = 0
    state_warm = 0
    for (host, email), rec in creds.items():
        r = runtime.get(host) or {"stats": set(), "access": set()}
        traffic_seen = email in r["stats"]
        access_seen = email in r["access"]
        # Если хотя бы один из источников видел email — bundle был
        # реально использован = assigned. Иначе — чистый warm-слот.
        rec["traffic_seen"] = traffic_seen
        rec["access_seen"] = access_seen
        if traffic_seen or access_seen:
            rec["state"] = "assigned"
            state_assigned += 1
        else:
            rec["state"] = "warm"
            state_warm += 1

    output_list = sorted(
        creds.values(),
        key=lambda r: (r["node"], r["state"], r["email"]),
    )
    args.output.write_text(
        json.dumps(output_list, indent=2, ensure_ascii=False) + "\n"
    )

    print("=== node_id_legacy map (передать в generate_restore_sql.py) ===", file=sys.stderr)
    node_map_arg = ",".join(f"{h}:{i}" for h, i in sorted(node_id_map.items()))
    print(f"--node-id-map '{node_map_arg}'", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        f"warm_bundles_total={len(output_list)} "
        f"state_assigned={state_assigned} state_warm={state_warm} "
        f"nodes={len(node_id_map)} -> {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
