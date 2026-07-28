#!/usr/bin/env python3
"""Привести split-tunnel секции hysteria2 в соответствие состоянию туннелей.

Зачем отдельный реконсайлер. Полный рендер `config.yaml.j2` делает только роль
`install_hysteria2` в site.yml, а attach/detach линка гоняет `relay_tunnel_apply.yml`
с одной ролью `relay_jump_node` — она для xray патчит конфиги через
`xray_reconcile.jq`, а hysteria до 2026-07-28 не трогала вовсе (там был только
`stat` и debug-строка «Hysteria2 traffic exits directly»).

Без этого скрипта:
  * detach последнего линка (или первичного wg0 на мульти-линковом relay) оставлял
    в конфиге `bindDevice` на УДАЛЁННЫЙ интерфейс. hysteria v2 проверяет девайс на
    старте (verifyDeviceName → net.InterfaceByName) и уходит в Fatal → краш-луп, то
    есть hy2 терял даже РУ-сегмент, который обслуживал до этого;
  * attach на уже забутстрапленной ноде не доставлял split-секции вовсе — а это
    дефолтный путь КАЖДОЙ новой РУ-ноды (spawn → bootstrap без линков → админ
    цепляет exit).

Формат правок повторяет `manage_hy2_user.sh` (safe_load → dump), поэтому пер-юзерные
учётки и наши секции переживают правки друг друга. Комментарии в файле теряются —
как и при любой правке юзеров, это уже свойство конфига.

Env:
  HY2_CONFIG    — путь к конфигу (по умолчанию /etc/hysteria/config.yaml)
  HY2_BIND_DEV  — имя wgN. Пусто/отсутствует = снять split-секции (нода больше
                  не relay), непусто = поставить/обновить.
  HY2_RU_ZONES  — JSON-список зон верхнего уровня (ru, su, …)
  HY2_RU_DOMAINS— JSON-список доменов (vk.com, …)

Печатает `changed` или `unchanged` — вызывающая таска использует это как
changed_when, чтобы не дёргать рестарт hysteria на каждом прогоне.
"""
from __future__ import annotations

import json
import os
import sys

import yaml


def build_sections(bind_device: str, zones: list[str], domains: list[str]):
    """Секции outbounds/acl ровно в том же виде, что рендерит config.yaml.j2.

    Держать синхронно с шаблоном: расхождение означает, что после attach нода
    получит одну конфигурацию, а после site.yml — другую.
    """
    outbounds = [
        # tunnel ПЕРВЫМ намеренно: при неприменившемся ACL hysteria шлёт всё в
        # первый outbound, и деградация должна идти в сторону «РУ-сайты видят
        # зарубежный IP», а не «VPN не работает вовсе».
        {"name": "tunnel", "type": "direct", "direct": {"bindDevice": bind_device}},
        {"name": "local", "type": "direct", "direct": {"mode": "auto"}},
    ]
    acl_rules = ["reject(geoip:private)"]
    acl_rules += [f"local(suffix:{z})" for z in zones]
    acl_rules += [f"local(suffix:{d})" for d in domains]
    acl_rules += ["local(geoip:ru)", "tunnel(all)"]
    return outbounds, {
        "inline": acl_rules,
        "geoip": "/usr/local/share/xray/geoip.dat",
    }


def main() -> int:
    path = os.environ.get("HY2_CONFIG", "/etc/hysteria/config.yaml")
    bind_device = (os.environ.get("HY2_BIND_DEV") or "").strip()
    zones = json.loads(os.environ.get("HY2_RU_ZONES") or "[]")
    domains = json.loads(os.environ.get("HY2_RU_DOMAINS") or "[]")

    if not os.path.exists(path):
        print("unchanged (no config)")
        return 0

    with open(path) as fh:
        cfg = yaml.safe_load(fh) or {}

    before = json.dumps([cfg.get("outbounds"), cfg.get("acl")], sort_keys=True)

    if bind_device:
        if not zones:
            # Пустой список = кто-то не передал переменные. Молча поставить ACL
            # без РУ-правил значило бы увести ВЕСЬ трафик, включая российский,
            # в туннель — лучше не трогать конфиг и упасть громко.
            print("ERROR: HY2_RU_ZONES пуст — отказываюсь писать ACL без РУ-правил",
                  file=sys.stderr)
            return 1
        cfg["outbounds"], cfg["acl"] = build_sections(bind_device, zones, domains)
    else:
        # Нода больше не relay: снимаем обе секции целиком. Оставленный
        # bindDevice на удалённый интерфейс = hysteria не стартует вообще.
        cfg.pop("outbounds", None)
        cfg.pop("acl", None)

    after = json.dumps([cfg.get("outbounds"), cfg.get("acl")], sort_keys=True)
    if before == after:
        print("unchanged")
        return 0

    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        yaml.dump(cfg, fh, default_flow_style=False, allow_unicode=True)
    # Проверяем, что записанное парсится, ДО подмены боевого файла.
    with open(tmp) as fh:
        yaml.safe_load(fh)
    os.replace(tmp, path)
    print("changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
