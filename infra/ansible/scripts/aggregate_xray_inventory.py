#!/usr/bin/env python3
"""Свести per-node xray-конфиги в users.json + nodes.json для DB-rebuild.

Принимает каталог, куда роль `recover_xray_inventory` сложила сырые
``<hostname>.config.json``, и выдаёт два файла:

* ``users.json`` — список реальных подписок (user_id, subscription_id,
  node, uuid, flow, protocol_tags, raw_emails). Используется как
  входной для пересоздания User/Subscription/Device.

* ``nodes.json`` — список нод и их VLESS-инбаундов (port, protocol,
  sni, short_id, ws_path, xhttp_path, private_key и т.д.). ``host`` и
  ``region`` тут пусты — их добирает ``generate_restore_sql.py`` из
  ansible-inventory. Этого достаточно, чтобы воссоздать vpn_nodes +
  vpn_configs с теми же Reality-ключами, что сейчас крутятся на нодах,
  и не пересобирать xray-стек на каждой ноде.

Логика по users.json:

* В каждом inbound xray-конфига читаем ``settings.clients[]``.
* Фильтруем по email-маске ``^user-(\\d+)-(\\d+)(?:-.*)?$`` — формат
  на ноде: ``user-{user_id}-{subscription_id}[-{epoch}-{nonce}]``.
  Бэкенд раньше писал bare ``user-N-M``, потом стал добавлять
  ``-{epoch_seconds}-{4hex}`` для уникальности при ре-провижининге;
  поддерживаем оба варианта. Warm-pool записи (``warm-N-hex``) и
  ручные tech-аккаунты отсекаются и считаются в summary.
* Группируем по ``(user_id, subscription_id, node)``. Разъехавшиеся
  uuid'ы для одной подписки фиксируем в ``uuid_conflicts`` —
  обычно индикатор partial-failed reprovision'а, оператор смотрит руками.

Логика по nodes.json:

* Walk по inbounds, пропускаем ``api-in``/`dokodemo-door` и любые
  не-vless протоколы. Для каждого VLESS-инбаунда (tag начинается с
  ``vless-``) выгребаем ``port`` и протокол-специфичные поля из
  ``streamSettings`` + ``streamSettings.{reality,ws,xhttp}Settings``.

Usage:
    python3 scripts/aggregate_xray_inventory.py \\
        --input-dir infra/ansible/recovered

Output:
    <input-dir>/users.json
    <input-dir>/nodes.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

USER_EMAIL_RE = re.compile(r"^user-(\d+)-(\d+)(?:-.*)?$")

# Маппинг xray-tag → models.VPNConfigProtocol value. Источник правды —
# backend/app/models.py:VPNConfigProtocol. Не меняй здесь не сверяясь.
TAG_TO_PROTOCOL = {
    "vless-reality": "vless-reality",
    "vless-xhttp": "vless-xhttp",
    "vless-ws-cdn": "vless-ws-cdn",
}


def _extract_inbound_config(inbound: dict) -> dict | None:
    """Из xray-inbound собирает запись для vpn_configs.

    Возвращает None для не-VLESS / служебных inbound'ов.
    """
    tag = inbound.get("tag") or ""
    protocol = TAG_TO_PROTOCOL.get(tag)
    if protocol is None:
        return None

    port = inbound.get("port")
    stream = inbound.get("streamSettings") or {}
    out: dict = {
        "tag": tag,
        "protocol": protocol,
        "port": port,
        # is_enabled — по факту присутствия в config.json считаем включённым;
        # отдельной "disabled" ветки xray не держит, ему положили — он гонит.
        "is_enabled": True,
    }

    if protocol == "vless-reality":
        rs = stream.get("realitySettings") or {}
        names = rs.get("serverNames") or []
        short_ids = rs.get("shortIds") or []
        out.update({
            "sni": names[0] if names else None,
            "server_names": names,
            "short_ids": short_ids,
            "private_key": rs.get("privateKey"),
            "camo_dest": rs.get("dest"),
        })

    elif protocol == "vless-ws-cdn":
        ws = stream.get("wsSettings") or {}
        out.update({
            # У ws-cdn клиент идёт через CDN-домен; sni = тот же host,
            # path — секретный WS endpoint, который nginx проксирует
            # на xray.
            "sni": (ws.get("headers") or {}).get("Host") or ws.get("host"),
            "ws_path": ws.get("path"),
        })

    elif protocol == "vless-xhttp":
        xs = stream.get("xhttpSettings") or {}
        out.update({
            "sni": xs.get("host"),
            "xhttp_path": xs.get("path"),
            "xhttp_mode": xs.get("mode") or "auto",
        })

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument(
        "--users-output",
        type=Path,
        help="Куда писать users.json (default: <input-dir>/users.json)",
    )
    parser.add_argument(
        "--nodes-output",
        type=Path,
        help="Куда писать nodes.json (default: <input-dir>/nodes.json)",
    )
    args = parser.parse_args()

    if not args.input_dir.is_dir():
        print(f"input-dir does not exist: {args.input_dir}", file=sys.stderr)
        return 2

    users_output = args.users_output or (args.input_dir / "users.json")
    nodes_output = args.nodes_output or (args.input_dir / "nodes.json")

    grouped: dict[tuple[int, int, str], dict] = {}
    nodes: dict[str, dict] = {}
    nodes_seen = 0
    nodes_unparseable = 0
    clients_total = 0
    clients_skipped_non_user = 0

    # Поддерживаем два формата имён файлов:
    # 1. Новый "<host>__<basename>.json"  — один файл на (host, protocol).
    #    Возникает после обновления роли (тут несколько файлов на хост:
    #    config.json для reality, config_xhttp.json для xhttp, ...).
    # 2. Старый "<host>.config.json" — один файл на хост, до апдейта.
    #    Оставляем для обратной совместимости с уже выкаченными данными.
    config_files = sorted(args.input_dir.glob("*.json"))
    # Дедуп по содержимому: если есть И новый, И старый файл одного хоста —
    # пропускаем старый (он гарантированно содержит подмножество нового).
    have_new_for_host: set[str] = set()
    for p in config_files:
        if "__" in p.name:
            have_new_for_host.add(p.name.split("__", 1)[0])

    hosts_with_data: set[str] = set()
    for cfg_path in config_files:
        if "__" in cfg_path.name:
            host, _, _proto_file = cfg_path.name.partition("__")
        else:
            # Старый формат "<host>.config.json"
            host = cfg_path.name.removesuffix(".config.json")
            if host == cfg_path.name:
                # Что-то нестандартное — пропускаем
                continue
            if host in have_new_for_host:
                # Есть новый формат для этого хоста — старый игнорим
                continue
        node = host
        if node not in hosts_with_data:
            hosts_with_data.add(node)
            nodes_seen += 1
        try:
            data = json.loads(cfg_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"[skip] {cfg_path.name}: invalid JSON ({exc})", file=sys.stderr)
            nodes_unparseable += 1
            continue

        node_configs: list[dict] = []

        for inbound in data.get("inbounds") or []:
            # Сначала vpn_configs запись (если это VLESS-inbound).
            cfg_record = _extract_inbound_config(inbound)
            if cfg_record is not None:
                node_configs.append(cfg_record)

            # Параллельно — список юзеров. Источник тот же inbound.
            tag = inbound.get("tag") or inbound.get("protocol") or "?"
            clients = (inbound.get("settings") or {}).get("clients") or []
            for client in clients:
                clients_total += 1
                email = client.get("email") or ""
                match = USER_EMAIL_RE.match(email)
                if not match:
                    clients_skipped_non_user += 1
                    continue

                user_id = int(match.group(1))
                subscription_id = int(match.group(2))
                uuid = client.get("id")
                flow = client.get("flow") or None
                key = (user_id, subscription_id, node)
                existing = grouped.get(key)
                if existing is None:
                    grouped[key] = {
                        "user_id": user_id,
                        "subscription_id": subscription_id,
                        "node": node,
                        "protocol_tags": [tag],
                        # Каждое устройство = уникальный email/uuid в xray
                        # clients[]. Если на одной (user,sub,node) лежит
                        # несколько таких записей — это несколько физических
                        # устройств юзера, а не конфликт.
                        "devices": [{
                            "email": email,
                            "uuid": uuid,
                            "flow": flow,
                            "protocols": [tag],
                        }],
                    }
                else:
                    if tag not in existing["protocol_tags"]:
                        existing["protocol_tags"].append(tag)
                    # Дедуп устройств по email: одно и то же устройство
                    # появляется в clients[] каждого инбаунда (Reality,
                    # XHTTP, WS-CDN) — c одинаковыми email/uuid. Объединяем
                    # protocols, не дублируем строку устройства.
                    dev = next((d for d in existing["devices"] if d["email"] == email), None)
                    if dev is None:
                        existing["devices"].append({
                            "email": email,
                            "uuid": uuid,
                            "flow": flow,
                            "protocols": [tag],
                        })
                    else:
                        if tag not in dev["protocols"]:
                            dev["protocols"].append(tag)
                        # Sanity: uuid обязан быть одинаковым на всех инбаундах
                        # для одного email. Если разъехался — фиксируем.
                        if uuid and dev["uuid"] and uuid != dev["uuid"]:
                            dev.setdefault("uuid_conflicts", []).append(uuid)

        # Аккумулируем конфиги по хосту (может быть несколько файлов
        # на ноду — по одному на каждый протокол). Дедуп по tag,
        # чтобы случайный двойной слурп не задублировал inbound.
        existing_node = nodes.get(node)
        if existing_node is None:
            nodes[node] = {
                "name": node,
                # host/region добирает generate_restore_sql.py из inventory.
                "host": None,
                "region": None,
                "configs": node_configs,
            }
        else:
            seen_tags = {c.get("tag") for c in existing_node["configs"]}
            for cfg in node_configs:
                if cfg.get("tag") not in seen_tags:
                    existing_node["configs"].append(cfg)
                    seen_tags.add(cfg.get("tag"))

    users_list = sorted(
        grouped.values(),
        key=lambda r: (r["user_id"], r["subscription_id"], r["node"]),
    )
    nodes_list = sorted(nodes.values(), key=lambda r: r["name"])

    users_output.write_text(json.dumps(users_list, indent=2, ensure_ascii=False) + "\n")
    nodes_output.write_text(json.dumps(nodes_list, indent=2, ensure_ascii=False) + "\n")

    user_ids = {r["user_id"] for r in users_list}
    sub_pairs = {(r["user_id"], r["subscription_id"]) for r in users_list}
    total_devices = sum(len(r["devices"]) for r in users_list)
    uuid_conflicts = sum(
        1 for r in users_list for d in r["devices"] if "uuid_conflicts" in d
    )
    total_configs = sum(len(n["configs"]) for n in nodes_list)

    print(
        f"nodes_seen={nodes_seen} unparseable={nodes_unparseable} "
        f"clients_total={clients_total} non_user_skipped={clients_skipped_non_user} "
        f"users={len(user_ids)} subscriptions={len(sub_pairs)} "
        f"devices={total_devices} uuid_conflicts={uuid_conflicts} "
        f"vpn_configs={total_configs} "
        f"-> {users_output}, {nodes_output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
