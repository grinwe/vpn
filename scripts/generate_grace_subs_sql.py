#!/usr/bin/env python3
"""Generate grace-period subscriptions SQL after the 2026-05-19 DR.

Контекст: после восстановления `Credential.pool_state='warm'` в БД 0 строк,
а на нодах в xray.clients[] живут ~91 warm-UUID. Любая новая активация
уйдёт в cold-path → ансибл-чейн нагрузит ноды.

Скрипт по recovered/credentials.json (snapshot warm-pool на момент DR) +
recovered/restore.sql (шаблоны config_text per node + per protocol):

* для каждого admin_user БЕЗ active sub в БД создаёт 1 Solo до 2026-06-15
  (29 шт, кроме user_9)
* для user_id=9 (telegram_id 1678661092) — Solo с extra_device_slots=1
  до 2026-06-13 (2 девайса, оба на одной ноде если возможно)
* UPDATE 8 known active sub → expires_at 2026-06-15
* orphan-subs на placeholder 999999 НЕ трогает — у них 18.06 дедлайн

Output идемпотентен (ON CONFLICT DO NOTHING) + setval-вызовы на seq.
Применять при остановленном боте (см. POSTMORTEM §3.3.8 race на user_id=1).
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

SOLO_PLAN_ID = 1
PLACEHOLDER_USER_ID = 999999
DEFAULT_GRACE_UNTIL = "2026-06-15"
USER_9_GRACE_UNTIL = "2026-06-13"
USER_9_TG = "1678661092"

# Идём сильно выше max в restore.sql (~10028 на credentials), чтобы оба
# дампа можно было прогнать на одной БД без конфликта по id.
SUB_ID_START = 11001
DEVICE_ID_START = 11001
CRED_ID_START = 11001


class NodeTemplate(NamedTuple):
    node_id: int
    host: str
    reality: dict
    xhttp: dict


_CRED_RE = re.compile(
    r"INSERT INTO credentials[^V]*VALUES \([^,]+,\s*[^,]+,\s*[^,]+,\s*"
    r"(\d+),\s*(\d+),\s*'([^']+)',\s*'(vless://[^']+)'"
)
_SUB_RE = re.compile(
    r"INSERT INTO subscriptions[^V]*VALUES \((\d+),\s*(\d+),"
)
_NODE_RE = re.compile(
    r"INSERT INTO vpn_nodes[^V]*VALUES \((\d+),\s*'([^']+)',\s*'([^']+)',\s*'([^']+)'"
)


def parse_admin_users(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return [
        {
            "id": int(e["id"]),
            "telegram_id": str(e["telegram_id"]),
            "subs": int(e.get("subs", "0")),
        }
        for e in data
    ]


def parse_warm_pool(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return [e for e in data if e.get("state") == "warm"]


def parse_restore_sql(path: Path) -> tuple[
    dict[int, NodeTemplate],
    set[int],
    dict[int, list[int]],
]:
    """Return (templates_per_node, known_user_ids, user_id -> [sub_ids])."""
    text = path.read_text()
    node_hosts: dict[int, str] = {}
    for m in _NODE_RE.finditer(text):
        node_hosts[int(m.group(1))] = m.group(4)

    known_users: set[int] = set()
    user_subs: dict[int, list[int]] = {}
    for m in _SUB_RE.finditer(text):
        sub_id = int(m.group(1))
        user_id = int(m.group(2))
        if user_id != PLACEHOLDER_USER_ID:
            known_users.add(user_id)
        user_subs.setdefault(user_id, []).append(sub_id)

    by_proto: dict[tuple[int, str], dict] = {}
    for m in _CRED_RE.finditer(text):
        config_id = int(m.group(1))
        node_id = int(m.group(2))
        proto = m.group(3)
        url = m.group(4)
        by_proto.setdefault(
            (node_id, proto),
            {"config_id": config_id, "url_template": url},
        )

    templates: dict[int, NodeTemplate] = {}
    for node_id, host in node_hosts.items():
        reality = by_proto.get((node_id, "vless-reality"))
        xhttp = by_proto.get((node_id, "vless-xhttp"))
        if reality and xhttp:
            templates[node_id] = NodeTemplate(
                node_id=node_id, host=host, reality=reality, xhttp=xhttp,
            )
    return templates, known_users, user_subs


def build_url(template: str, new_uuid: str) -> str:
    return re.sub(
        r"vless://[0-9a-f-]{36}@",
        f"vless://{new_uuid}@",
        template,
        count=1,
    )


def gen_sub_token() -> str:
    return secrets.token_urlsafe(32)


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--admin-users", required=True, type=Path)
    ap.add_argument("--credentials", required=True, type=Path)
    ap.add_argument("--restore-sql", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--grace-until", default=DEFAULT_GRACE_UNTIL)
    ap.add_argument("--user-9-until", default=USER_9_GRACE_UNTIL)
    args = ap.parse_args()

    admin_users = parse_admin_users(args.admin_users)
    warm_pool = parse_warm_pool(args.credentials)
    templates, known_users, user_subs = parse_restore_sql(args.restore_sql)

    by_node_warm: dict[int, list[dict]] = {}
    for w in warm_pool:
        by_node_warm.setdefault(w["node_id_legacy"], []).append(w)

    def pick_warm(node_id: int | None = None) -> dict:
        if node_id is not None and by_node_warm.get(node_id):
            return by_node_warm[node_id].pop(0)
        for n in sorted(by_node_warm, key=lambda k: -len(by_node_warm[k])):
            if by_node_warm[n]:
                return by_node_warm[n].pop(0)
        raise RuntimeError("warm-pool exhausted")

    grace_until_dt = f"'{args.grace_until} 23:59:59+00'"
    user_9_until_dt = f"'{args.user_9_until} 23:59:59+00'"

    out: list[str] = []
    out.append("-- Generated by scripts/generate_grace_subs_sql.py")
    out.append(f"-- {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    out.append(
        "-- Apply with bot stopped: docker compose stop bot;"
        " psql ... < grace.sql; docker compose start bot",
    )
    out.append("BEGIN;")
    out.append("")

    next_sub_id = SUB_ID_START
    next_dev_id = DEVICE_ID_START
    next_cred_id = CRED_ID_START

    # ── A. Extend known subs ────────────────────────────────────────
    out.append("-- ─── A. Extend known active subs to grace deadline ─────────")
    extended_count = 0
    for u in admin_users:
        if u["id"] not in known_users:
            continue
        for sub_id in user_subs.get(u["id"], []):
            out.append(
                f"UPDATE subscriptions SET expires_at={grace_until_dt}, "
                f"updated_at=NOW() WHERE id={sub_id} "
                f"AND user_id={u['id']};",
            )
            extended_count += 1
    out.append(f"-- ({extended_count} known sub rows extended to {args.grace_until})")
    out.append("")

    # ── B. Provision grace Solo for users without active sub ────────
    out.append("-- ─── B. Provision grace Solo (1 device by default) ─────────")
    created = 0
    user_9_seen = False

    for u in admin_users:
        if u["id"] in known_users:
            continue
        is_user_9 = u["telegram_id"] == USER_9_TG
        if is_user_9:
            user_9_seen = True
            expires_dt = user_9_until_dt
            extra_slots = 1
            device_count = 2
            note = (
                f"grace-period after DR 2026-05-19; user_id=9 special "
                f"(Solo+extra, expires {args.user_9_until})"
            )
        else:
            expires_dt = grace_until_dt
            extra_slots = 0
            device_count = 1
            note = f"grace-period after DR 2026-05-19 (Solo, expires {args.grace_until})"

        try:
            primary = pick_warm()
        except RuntimeError:
            out.append(f"-- ⚠ warm-pool exhausted at user_id={u['id']}; skipping")
            continue
        node_id = primary["node_id_legacy"]
        tmpl = templates.get(node_id)
        if tmpl is None:
            out.append(
                f"-- ⚠ no template for node_id={node_id} "
                f"(user_id={u['id']}, email={primary['email']})",
            )
            continue

        bundles = [primary]
        for _ in range(device_count - 1):
            try:
                bundles.append(pick_warm(node_id))
            except RuntimeError:
                out.append(
                    f"-- ⚠ warm-pool exhausted for user_id={u['id']} extra device",
                )
                break

        sub_id = next_sub_id
        next_sub_id += 1
        sub_token = gen_sub_token()
        out.append(
            f"INSERT INTO subscriptions (id, user_id, plan_id, node_id, "
            f"created_at, updated_at, expires_at, status, sub_token, "
            f"auto_renew, extra_device_slots, notes) VALUES ("
            f"{sub_id}, {u['id']}, {SOLO_PLAN_ID}, {node_id}, NOW(), NOW(), "
            f"{expires_dt}, 'active', {sql_str(sub_token)}, FALSE, "
            f"{extra_slots}, {sql_str(note)}) ON CONFLICT (id) DO NOTHING;",
        )

        for idx, bundle in enumerate(bundles):
            dev_id = next_dev_id
            next_dev_id += 1
            dev_token = gen_sub_token()
            name = "primary" if idx == 0 else f"device-{idx + 1}"
            out.append(
                f"INSERT INTO devices (id, user_id, subscription_id, "
                f"config_id, name, status, access_username, sub_token, "
                f"created_at, updated_at) VALUES ("
                f"{dev_id}, {u['id']}, {sub_id}, "
                f"{tmpl.reality['config_id']}, "
                f"{sql_str(name)}, 'active', {sql_str(bundle['email'])}, "
                f"{sql_str(dev_token)}, NOW(), NOW()) "
                f"ON CONFLICT (id) DO NOTHING;",
            )
            r_url = build_url(tmpl.reality["url_template"], bundle["uuid"])
            x_url = build_url(tmpl.xhttp["url_template"], bundle["uuid"])
            out.append(
                f"INSERT INTO credentials (id, subscription_id, device_id, "
                f"config_id, node_id, proto, config_text, access_username, "
                f"pool_state, is_active, created_at, assigned_at) VALUES ("
                f"{next_cred_id}, {sub_id}, {dev_id}, "
                f"{tmpl.reality['config_id']}, {node_id}, 'vless-reality', "
                f"{sql_str(r_url)}, {sql_str(bundle['email'])}, "
                f"'assigned', TRUE, NOW(), NOW()) "
                f"ON CONFLICT (id) DO NOTHING;",
            )
            next_cred_id += 1
            out.append(
                f"INSERT INTO credentials (id, subscription_id, device_id, "
                f"config_id, node_id, proto, config_text, access_username, "
                f"pool_state, is_active, created_at, assigned_at) VALUES ("
                f"{next_cred_id}, {sub_id}, {dev_id}, "
                f"{tmpl.xhttp['config_id']}, {node_id}, 'vless-xhttp', "
                f"{sql_str(x_url)}, {sql_str(bundle['email'])}, "
                f"'assigned', TRUE, NOW(), NOW()) "
                f"ON CONFLICT (id) DO NOTHING;",
            )
            next_cred_id += 1
        created += 1

    out.append("")
    out.append(f"-- ({created} new grace subs created)")
    if not user_9_seen:
        out.append(
            f"-- ⚠ user with telegram_id={USER_9_TG} not found "
            f"in admin_users.json — extra-device special-case not applied",
        )
    out.append("")

    # ── C. Sequences ────────────────────────────────────────────────
    out.append("-- ─── C. Bump sequences past inserted ids ───────────────────")
    for seq, tbl in (
        ("subscriptions_id_seq", "subscriptions"),
        ("devices_id_seq", "devices"),
        ("credentials_id_seq", "credentials"),
    ):
        out.append(
            f"SELECT setval('{seq}', "
            f"GREATEST((SELECT COALESCE(MAX(id),0) FROM {tbl}), 1));",
        )
    out.append("")
    out.append("COMMIT;")
    out.append("")

    args.output.write_text("\n".join(out))

    print("=== grace.sql generated ===")
    print(f"  Output:           {args.output}")
    print(f"  Extended (UPD):   {extended_count} known sub rows")
    print(f"  Created (INS):    {created} new grace subs")
    devices_used = next_dev_id - DEVICE_ID_START
    print(f"  Warm bundles used: {devices_used}")
    remaining = sum(len(v) for v in by_node_warm.values())
    print(f"  Warm-pool remaining: {remaining}")
    if user_9_seen:
        print(
            f"  user_9 (tg {USER_9_TG}): 2 devices, expires {args.user_9_until}",
        )
    else:
        print(f"  ⚠ user_9 (tg {USER_9_TG}) was NOT in admin_users.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
