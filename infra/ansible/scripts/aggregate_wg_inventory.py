#!/usr/bin/env python3
"""Парсит wg-конфиги, выгруженные ролью recover_wg_inventory, и собирает
их в wg.json — карту exit-нод и relay-links для DB-rebuild.

Вход: каталог с файлами ``<hostname>__wg<N>.conf``, по одному файлу на
WG-интерфейс (на jump-нодах обычно wg0, на exit'е тоже wg0 — но может
быть и wg1/wg2 если несколько туннелей).

Wg-конфиг — простой INI-подобный формат, но с **множественными**
``[Peer]`` секциями (configparser давится дубликатами секций), поэтому
парсим вручную линейно.

Различаем jump vs exit по структуре:
* **Jump** (client-side): один [Peer], в котором есть ``Endpoint``
  (адрес exit-сервера). [Interface].Address — узкая маска (/32).
* **Exit** (server-side): один или несколько [Peer], **без**
  Endpoint у них (peer'ы там — это jump'ы, которые сами коннектятся).
  [Interface].Address — широкая маска (/24).

Output ``wg.json`` имеет вид::

    {
      "exits": [
        {
          "name": "kr-pq-01",
          "host": "45.136.149.113",
          "wg_port": 51820,
          "wg_address_v4": "10.77.0.1/24",
          "wg_private_key": "<server priv>",
          "wg_public_key": "<server pub>",
          "region": "kr"
        }, ...
      ],
      "links": [
        {
          "jump": "ru-pq-01",
          "exit": "kr-pq-01",
          "wg_interface_name": "wg0",
          "wg_client_private_key": "<jump priv>",
          "wg_client_public_key": "<jump pub>",
          "wg_client_address_v4": "10.77.0.5/32"
        }, ...
      ],
      "unmatched": [...]   # jump-конфиги, у которых не нашёлся exit
    }

Public-ключи производятся из private через X25519 (та же кривая что
у Reality). Inventory нужен для добывания exit-нодного ``host`` и
``region`` — этих данных в самом wg-конфиге нет.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required.", file=sys.stderr)
    sys.exit(2)

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import x25519
except ImportError:
    print("cryptography required.", file=sys.stderr)
    sys.exit(2)


def _derive_wg_public_key(private_b64: str) -> str:
    """WireGuard private→public — X25519, тот же что у Reality, но
    в обычном base64 (стандартный, не url-safe), 32 байта, padding есть.
    """
    raw = base64.b64decode(private_b64)
    if len(raw) != 32:
        raise ValueError(f"wg private key must be 32 bytes, got {len(raw)}")
    priv = x25519.X25519PrivateKey.from_private_bytes(raw)
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(pub).decode()


def parse_wg_conf(text: str) -> dict:
    """Парсит ``wgN.conf`` в структуру.

    Returns:
        {
          "interface": {"PrivateKey", "Address", "ListenPort"?},
          "peers": [{"PublicKey", "Endpoint"?, "AllowedIPs", "PersistentKeepalive"?}, ...]
        }
    """
    interface: dict = {}
    peers: list[dict] = []
    current: dict | None = None
    current_kind: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
            if section == "interface":
                current = interface
                current_kind = "interface"
            elif section == "peer":
                current = {}
                peers.append(current)
                current_kind = "peer"
            else:
                current = None
                current_kind = None
            continue
        if current is None or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        current[k] = v
    return {"interface": interface, "peers": peers}


def is_exit_config(parsed: dict) -> bool:
    """Exit (server) — все peer'ы БЕЗ Endpoint. Jump (client) — есть."""
    return all("Endpoint" not in p for p in parsed["peers"]) and len(parsed["peers"]) > 0


def load_inventory(path: Path) -> dict[str, dict]:
    """name → {host, region} из all.children.{vpn_nodes,wg_exit_nodes}."""
    inv = yaml.safe_load(path.read_text()) or {}
    children = (inv.get("all") or {}).get("children") or {}
    out: dict[str, dict] = {}
    for group in ("vpn_nodes", "wg_exit_nodes"):
        hosts = (children.get(group) or {}).get("hosts") or {}
        for name, info in hosts.items():
            out[name] = {
                "host": (info or {}).get("ansible_host"),
                "region": (info or {}).get("location"),
                "group": group,
            }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if not args.input_dir.is_dir():
        print(f"input-dir does not exist: {args.input_dir}", file=sys.stderr)
        # 0 — это не ошибка для рекавери-плейбука, если wg вообще не используется.
        return 0

    inventory = load_inventory(args.inventory)

    # Сначала разбираем всё, потом маршим. Файлы названы как
    # "<hostname>__wgN.conf" — split по "__".
    parsed_files: list[dict] = []
    for path in sorted(args.input_dir.glob("*.conf")):
        host_part, _, iface_part = path.name.partition("__")
        if not iface_part:
            print(f"[skip] {path.name}: unexpected filename, no __", file=sys.stderr)
            continue
        iface = iface_part.replace(".conf", "")
        try:
            parsed = parse_wg_conf(path.read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {path.name}: parse error {exc}", file=sys.stderr)
            continue
        parsed_files.append({
            "host": host_part,
            "interface": iface,
            "parsed": parsed,
        })

    # Bucketize.
    exits: dict[str, dict] = {}
    jumps: list[dict] = []
    for f in parsed_files:
        host = f["host"]
        inv = inventory.get(host) or {}
        if is_exit_config(f["parsed"]):
            priv = f["parsed"]["interface"].get("PrivateKey", "")
            try:
                pub = _derive_wg_public_key(priv) if priv else None
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] {host}: cannot derive public key: {exc}", file=sys.stderr)
                pub = None
            exits[host] = {
                "name": host,
                "host": inv.get("host"),
                "region": inv.get("region") or "unknown",
                "wg_port": int(f["parsed"]["interface"].get("ListenPort", 51820)),
                "wg_address_v4": f["parsed"]["interface"].get("Address", "10.77.0.1/24"),
                "wg_private_key": priv,
                "wg_public_key": pub,
                "interface": f["interface"],
                "peers_count": len(f["parsed"]["peers"]),
            }
        else:
            # Jump-сторона. Может быть несколько wgN на одной jump-ноде.
            jumps.append({
                "host": host,
                "interface": f["interface"],
                "parsed": f["parsed"],
            })

    # Связь jump → exit. Каждый peer в jump'е имеет Endpoint = "<IP>:port".
    # Сопоставляем с exits[*].host. Public-ключ peer'а = exit'а public_key —
    # это вторичная сверка.
    links: list[dict] = []
    unmatched: list[dict] = []
    for j in jumps:
        iface_info = j["parsed"]["interface"]
        client_priv = iface_info.get("PrivateKey", "")
        try:
            client_pub = _derive_wg_public_key(client_priv) if client_priv else None
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] jump {j['host']}/{j['interface']}: pub-key derive: {exc}", file=sys.stderr)
            client_pub = None
        for peer in j["parsed"]["peers"]:
            endpoint = peer.get("Endpoint", "")
            ep_host = endpoint.split(":", 1)[0] if endpoint else ""
            exit_record = None
            for exit_name, ex in exits.items():
                if ex.get("host") and ex["host"] == ep_host:
                    exit_record = ex
                    break
            if exit_record is None:
                unmatched.append({
                    "jump": j["host"],
                    "interface": j["interface"],
                    "endpoint": endpoint,
                    "peer_public_key": peer.get("PublicKey"),
                })
                continue
            links.append({
                "jump": j["host"],
                "exit": exit_record["name"],
                "wg_interface_name": j["interface"],
                "wg_client_private_key": client_priv,
                "wg_client_public_key": client_pub,
                "wg_client_address_v4": iface_info.get("Address", ""),
            })

    out = {
        "exits": sorted(exits.values(), key=lambda x: x["name"]),
        "links": sorted(links, key=lambda l: (l["jump"], l["exit"])),
        "unmatched": unmatched,
    }
    args.output.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")

    print(
        f"exits={len(out['exits'])} links={len(out['links'])} "
        f"unmatched_jumps={len(unmatched)} -> {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
