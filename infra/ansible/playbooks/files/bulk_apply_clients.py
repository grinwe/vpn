#!/usr/bin/env python3
"""Применить СРАЗУ ВЕСЬ список клиентов на ноде за один заход.

Зачем: ресинк ноды раньше гонял ansible-цикл, где каждый клиент = отдельный
SSH-раунд и отдельная перезапись конфига. Время росло линейно
(T ≈ 15с + 0.38с × клиентов, замер по 25 прогонам), и при ~740 кредах на ноду
упиралось в ANSIBLE_PLAYBOOK_TIMEOUT=300 — таска падала, нода оставалась
несинхронизированной. Здесь тот же результат достигается за один вызов:
время перестаёт зависеть от числа клиентов (аудит 2026-07-25).

Семантика намеренно повторяет ``manage_*_user.sh add`` для каждого клиента:
  * vless: клиент с таким email удаляется и добавляется заново (upsert);
  * routing (G.6): клиент с непустым ``exit_interface`` снимается со всех
    ``direct-wg*`` правил и прикрепляется к ``direct-<iface>`` (правило
    создаётся при отсутствии, опустевшие ``direct-wg*`` удаляются). Клиент БЕЗ
    iface роутинг не трогает — иначе ресинк затирал бы состояние, которое
    только что отрендерил шаблон роли;
  * hy2: пара username→password кладётся в ``auth.userpass``.

Скрипт НЕ удаляет клиентов, которых нет во входном списке — ровно как и
прежний цикл: снятие доступа идёт отдельным путём (``del`` / revoke).

Блокировки берём те же, что и per-user скрипты, иначе параллельный
device/apply может потерять правку (классическая гонка read-modify-write).

Usage:
  bulk_apply_clients.py --kind reality|xhttp|ws|hy2 --clients /path/clients.json
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
import tempfile

KINDS = {
    "reality": {
        "config": "/usr/local/etc/xray/config.json",
        "tag": "vless-reality",
        "lock": "/var/lock/manage_vless_user.lock",
        "flow": "xtls-rprx-vision",
    },
    "xhttp": {
        "config": "/usr/local/etc/xray/config_xhttp.json",
        "tag": "vless-xhttp",
        "lock": "/var/lock/manage_vless_xhttp.lock",
        "flow": "",
    },
    "ws": {
        "config": "/usr/local/etc/xray/config_ws_cdn.json",
        "tag": "vless-ws-cdn",
        "lock": "/var/lock/manage_vless_ws.lock",
        "flow": "",
    },
    "hy2": {
        "config": "/etc/hysteria/config.yaml",
        "lock": "/var/lock/manage_hy2_user.lock",
    },
}


def _atomic_write(path: str, render, *, mode: int, group: str | None) -> None:
    """Записать конфиг атомарно, сохранив владельца/права.

    Через временный файл в ТОЙ ЖЕ директории: ``mv`` из /tmp может пересечь
    файловую систему, а заодно снести группу, которую xray (работающий под
    nobody) должен иметь для чтения — без неё сервис падает с "permission
    denied" на следующем рестарте.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bulk-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            render(fh)
        os.chmod(tmp, mode)
        if group is not None:
            try:
                shutil.chown(tmp, user="root", group=group)
            except (LookupError, PermissionError):
                pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _apply_vless(cfg: dict, spec: dict, clients: list[dict]) -> int:
    tag = spec["tag"]
    inbound = next(
        (i for i in cfg.get("inbounds", []) if i.get("tag") == tag), None
    )
    if inbound is None:
        raise SystemExit(f"inbound с тегом {tag} не найден в {spec['config']}")

    settings = inbound.setdefault("settings", {})
    existing = settings.setdefault("clients", [])
    incoming = {c["username"]: c for c in clients if c.get("username") and c.get("uuid")}

    kept = [c for c in existing if c.get("email") not in incoming]
    for name, c in incoming.items():
        entry = {"id": c["uuid"], "email": name}
        flow = c.get("flow", spec.get("flow") or "")
        if flow:
            entry["flow"] = flow
        kept.append(entry)
    settings["clients"] = kept

    # ── routing (G.6) ────────────────────────────────────────────────
    routed = {n: c["exit_interface"] for n, c in incoming.items() if c.get("exit_interface")}
    if routed:
        rules = cfg.setdefault("routing", {}).setdefault("rules", [])
        for rule in rules:
            if str(rule.get("outboundTag", "")).startswith("direct-wg"):
                rule["user"] = [u for u in rule.get("user", []) if u not in routed]
        rules = [
            r
            for r in rules
            if not str(r.get("outboundTag", "")).startswith("direct-wg")
            or r.get("user")
        ]
        for name, iface in routed.items():
            target = f"direct-{iface}"
            rule = next((r for r in rules if r.get("outboundTag") == target), None)
            if rule is None:
                rules.append({"type": "field", "user": [name], "outboundTag": target})
            elif name not in rule.setdefault("user", []):
                rule["user"].append(name)
        cfg["routing"]["rules"] = rules
    return len(incoming)


def _apply_hy2(cfg: dict, clients: list[dict]) -> int:
    up = cfg.setdefault("auth", {}).setdefault("userpass", {})
    n = 0
    for c in clients:
        if not c.get("username") or c.get("password") in (None, ""):
            continue
        up[c["username"]] = c["password"]
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kind", required=True, choices=sorted(KINDS))
    ap.add_argument("--clients", required=True, help="JSON-файл со списком клиентов")
    args = ap.parse_args()

    spec = KINDS[args.kind]
    config_path = spec["config"]
    if not os.path.isfile(config_path):
        print(f"конфиг {config_path} не найден — нечего применять", file=sys.stderr)
        return 0

    with open(args.clients) as fh:
        clients = json.load(fh)
    if not clients:
        print("список клиентов пуст — no-op")
        return 0

    lock_fd = os.open(spec["lock"], os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # Блокируемся так же, как per-user скрипты: параллельный device/apply
        # иначе прочитает конфиг до нашей записи и затрёт её своей версией.
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        if args.kind == "hy2":
            import yaml

            with open(config_path) as fh:
                cfg = yaml.safe_load(fh) or {}
            applied = _apply_hy2(cfg, clients)
            _atomic_write(
                config_path,
                lambda fh: yaml.dump(cfg, fh, default_flow_style=False),
                mode=0o640,
                group=None,
            )
        else:
            with open(config_path) as fh:
                cfg = json.load(fh)
            applied = _apply_vless(cfg, spec, clients)
            _atomic_write(
                config_path,
                lambda fh: json.dump(cfg, fh, indent=2),
                mode=0o640,
                group="nogroup",
            )
    finally:
        os.close(lock_fd)

    print(f"{args.kind}: применено клиентов {applied} (из {len(clients)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
