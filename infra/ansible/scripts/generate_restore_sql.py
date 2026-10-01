#!/usr/bin/env python3
"""Из users.json + nodes.json + (опц.) wg.json + ansible inventory собрать restore.sql.

Скрипт собирает DDL/DML для воссоздания минимально-рабочего состояния
БД после потери mgmt-хоста, опираясь только на:

* ``users.json`` — список (user_id, subscription_id, node, uuid, ...)
  с реальных нод (см. ``aggregate_xray_inventory.py``).
* ``nodes.json`` — параметры VLESS-инбаундов (port, sni, short_id,
  ws_path, xhttp_path, private_key) — взятые из тех же config.json.
* ``wg.json`` (опц.) — exit-ноды и relay-links из wg-конфигов
  (см. ``aggregate_wg_inventory.py``).

APP_SECRET_KEY обязателен (``--app-secret-key`` либо одноимённая переменная
окружения): им шифруются ВСЕ секреты, которые backend читает через
``security.decrypt()`` — ``credentials.config_text`` (внутри UUID клиента),
reality-``private_key``, WG-ключи exit'ов и relay-линков. Без ключа скрипт
отказывается генерировать SQL: иначе restore зальёт в свежую БД плейнтекст и
молча откатит фикс 223dd71. Осознанный обход — ``--allow-plaintext-secrets``
(сценарий «ключ утерян безвозвратно, поднимаемся с новым»); после такого
восстановления обязателен прогон
``docker compose exec -T backend python -m scripts.encrypt_legacy_secrets --apply``.
* ``inventories/prod/hosts.yml`` — карта ``name → ansible_host``
  и ``location`` (= region). Без неё мы не знаем IP'ов нод.

На выходе один файл, который можно скормить psql:

    docker compose exec -T db psql -U vpn -d vpn < restore.sql

Что генерируем (idempotent — все INSERT'ы с ON CONFLICT DO NOTHING):

  1. ``vpn_nodes``        — по строке на ноду из nodes.json, с
                            фиксированным id из --node-id-map (или
                            автоинкрементом, если не задано).
  2. ``vpn_configs``      — по строке на VLESS-инбаунд каждой ноды;
                            ``public_key`` для Reality выводим из
                            ``private_key`` через X25519.
  3. ``users``            — фиксированные id из users.json, плюс
                            опциональный ``--telegram-map`` чтобы
                            привязать tg_id где знаем.
  4. ``subscriptions``    — id, user_id, node_id; expires_at +
                            grace-период (--grace-days, default 60),
                            plan_id из --default-plan-id.
  5. ``devices``          — по одному на subscription, привязан к
                            первому VLESS-конфигу ноды; uuid из
                            users.json, sub_token — sgenerированный
                            новый (старый юзеру всё равно отдавать
                            заново).
  6. ``setval()``         — двигает sequence'ы users/subscriptions
                            past max(id), чтобы новые регистрации
                            не наехали.

НЕ генерируем: warm-pool, balance/credit, plans (предполагаем что
plans уже создан в новой БД либо отдельно сидится), invoices,
audit_log, referral_codes — это всё либо потеряно навсегда, либо
не критично для восстановления доступа.

Usage:
    APP_SECRET_KEY="$(ssh root@<mgmt> 'docker compose -f /opt/vpn/docker-compose.yml exec -T backend env' | sed -n 's/^APP_SECRET_KEY=//p')" \\
    python3 scripts/generate_restore_sql.py \\
        --users-json   infra/ansible/recovered/users.json \\
        --nodes-json   infra/ansible/recovered/nodes.json \\
        --inventory    infra/ansible/inventories/prod/hosts.yml \\
        --output       infra/ansible/recovered/restore.sql \\
        --default-plan-id 1 \\
        --grace-days 60 \\
        --telegram-map  '9:1678661092'
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml  # PyYAML — стандартно есть на ansible-controller.
except ImportError:
    print("PyYAML required. `pip install pyyaml`", file=sys.stderr)
    sys.exit(2)

try:
    import hashlib
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import x25519
except ImportError:
    print(
        "cryptography required. `pip install cryptography`. "
        "Альтернатива: запустить `xray x25519 -i <private>` для каждого "
        "Reality-конфига вручную и подставить в SQL.",
        file=sys.stderr,
    )
    sys.exit(2)


# ── Fernet encryption ровно как в backend/app/security.py ─────────────
# Префикс enc:v1: + Fernet-токен. Ключ — sha256(APP_SECRET_KEY),
# url-safe base64. Если backend меняет схему — синхронизируй здесь.
_ENC_PREFIX = "enc:v1:"


def _fernet_from_secret(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_with_app_key(value: str | None, app_secret_key: str | None) -> str | None:
    """Шифрует строку в формате backend'а. Без ключа — None (низкоуровневый
    примитив; вызывающие ходят через :func:`protect_secret`)."""
    if value is None:
        return None
    if not app_secret_key:
        return None
    cipher = _fernet_from_secret(app_secret_key)
    token = cipher.encrypt(value.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def resolve_app_secret_key(cli_value: str | None) -> str | None:
    """Ключ из флага, иначе из окружения.

    Env — предпочтительный путь в DR: ключ и так лежит в .env web-хоста и в
    vault, а argv светится в ``ps`` и в history оператора, куда секретам не
    место.
    """
    if cli_value and cli_value.strip():
        return cli_value.strip()
    return (os.getenv("APP_SECRET_KEY") or "").strip() or None


def protect_secret(
    value: str | None, app_secret_key: str | None, *, allow_plaintext: bool
) -> str | None:
    """Значение для колонки, которую backend читает через ``decrypt()``.

    Отдельно от :func:`encrypt_with_app_key`, потому что здесь «нет ключа» НЕ
    означает NULL: ``credentials.config_text`` — NOT NULL, а reality без
    private_key просто не поднимется. Молча писать плейнтекст нельзя — ровно
    так в проде осело 28 кредов до 223dd71, — поэтому единственный путь без
    ключа явный: ``--allow-plaintext-secrets``.
    """
    if value is None:
        return None
    if app_secret_key:
        return encrypt_with_app_key(value, app_secret_key)
    if allow_plaintext:
        return value
    raise RuntimeError(
        "protect_secret вызван без APP_SECRET_KEY и без allow_plaintext — "
        "баг вызывающего: проверка обязана была отработать в начале main()."
    )


def key_fingerprint(app_secret_key: str) -> str:
    """8 hex — чтобы оператор глазами сверил ключ генерации с тем, что поедет
    в .env нового backend'а.

    Домен-сепаратор обязателен: сам Fernet-ключ = sha256(APP_SECRET_KEY), так
    что печатать куски этого дайджеста нельзя — это буквально байты ключа.
    Рассинхрон ключей — главный новый способ отстрелить себе ногу: с ним
    restore ляжет успешно, а декрипт кредов вернёт None и сабы отдадут 503.
    """
    return hashlib.sha256(
        b"restore-sql-key-fingerprint:" + app_secret_key.encode("utf-8")
    ).hexdigest()[:8]


def derive_reality_public_key(private_b64url: str) -> str:
    """Из xray-овой Reality privateKey (base64url, 32 байта) — public.

    Xray использует X25519 без padding'а в base64url. Возвращаем
    тоже base64url без padding'а — формат, который кладётся в
    ``vless://...?pbk=...`` и в БД (``vpn_configs.public_key``).
    """
    pad = "=" * ((4 - len(private_b64url) % 4) % 4)
    raw_priv = base64.urlsafe_b64decode(private_b64url + pad)
    if len(raw_priv) != 32:
        raise ValueError(f"x25519 private key must be 32 bytes, got {len(raw_priv)}")
    priv = x25519.X25519PrivateKey.from_private_bytes(raw_priv)
    raw_pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode()


def sql_str(v) -> str:
    """SQL-safe single-quoted literal, NULL для None."""
    if v is None:
        return "NULL"
    s = str(v).replace("'", "''")
    return f"'{s}'"


def sql_bool(v: bool) -> str:
    return "TRUE" if v else "FALSE"


def sql_int(v) -> str:
    return "NULL" if v is None else str(int(v))


def sql_jsonb(d: dict | None) -> str:
    if d is None or d == {}:
        return "NULL"
    return f"'{json.dumps(d, ensure_ascii=False).replace(chr(39), chr(39)*2)}'::jsonb"


def load_inventory(path: Path) -> dict[str, dict]:
    """name → {host, region} из ansible hosts.yml.

    Структура у нас:
      all.children.vpn_nodes.hosts.<name>.ansible_host, .location
    """
    with path.open() as f:
        inv = yaml.safe_load(f)
    nodes = (inv or {}).get("all", {}).get("children", {}).get("vpn_nodes", {}).get("hosts", {}) or {}
    return {
        name: {
            "host": (info or {}).get("ansible_host"),
            "region": (info or {}).get("location"),
        }
        for name, info in nodes.items()
    }


def parse_node_id_map(spec: str | None) -> dict[str, int]:
    """``name1:id1,name2:id2`` → dict. Пустой spec = пустой dict.

    Когда не задано — используем "виртуальные" id 1..N в порядке
    nodes.json и докатываем sequence. Если задано — конкретные id,
    чтобы попасть в те же значения, что были у нод до инцидента
    (полезно если есть скрин админки с node_id колонкой).
    """
    if not spec:
        return {}
    out = {}
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        name, _, raw_id = piece.partition(":")
        out[name.strip()] = int(raw_id)
    return out


def parse_telegram_map(spec: str | None) -> dict[int, str]:
    """``user_id:tg_id,...`` → dict[int,str].

    Нужно, чтобы вкатить известные telegram_id'ы (из скрина админки,
    из Stars-истории) в новые users-строки сразу, а не ждать пока
    юзер сам напишет /restore.
    """
    if not spec:
        return {}
    out = {}
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        raw_uid, _, tg = piece.partition(":")
        out[int(raw_uid)] = tg.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users-json", required=True, type=Path)
    parser.add_argument("--nodes-json", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--wg-json", type=Path, default=None,
        help="wg.json от aggregate_wg_inventory.py — если задан, "
             "генерим INSERT'ы для wg_exit_nodes и relay_exit_links.",
    )
    parser.add_argument(
        "--app-secret-key", default=None,
        help="APP_SECRET_KEY (Fernet) — тот же, что был у старого backend'а. "
             "Им шифруются credentials.config_text, reality private_key и "
             "WG-ключи. Можно (предпочтительно) передать через переменную "
             "окружения APP_SECRET_KEY — argv виден в ps. Без ключа скрипт "
             "отказывается генерировать SQL.",
    )
    parser.add_argument(
        "--allow-plaintext-secrets", action="store_true",
        help="Осознанно писать секреты открытым текстом — только когда ключ "
             "утерян безвозвратно и новый backend поднимается с новым "
             "APP_SECRET_KEY. decrypt() на плейнтексте — no-op, БД заведётся; "
             "сразу после restore прогони `docker compose exec -T backend "
             "python -m scripts.encrypt_legacy_secrets --apply`.",
    )
    parser.add_argument(
        "--default-plan-id", type=int, default=1,
        help="plan_id для всех восстановленных подписок (default: 1).",
    )
    parser.add_argument(
        "--grace-days", type=int, default=60,
        help="Сколько дней грейс-периода поставить как expires_at "
             "(default: 60).",
    )
    parser.add_argument(
        "--node-id-map", default="",
        help="`name1:id1,name2:id2` — если нужно сохранить старые "
             "node_id (например когда есть скрин админки).",
    )
    parser.add_argument(
        "--telegram-map", default="",
        help="`user_id:tg_id,...` — известные telegram_id для users. "
             "Игнорируется/дополняется --admin-users-json.",
    )
    parser.add_argument(
        "--admin-users-json", type=Path, default=None,
        help="JSON-выгрузка из админки (admin_users.json): users.id, "
             "telegram_id, balance, created_at. Если задан — это AUTHORITATIVE "
             "источник для таблицы users, поверх xray-инвентаря.",
    )
    parser.add_argument(
        "--credentials-json", type=Path, default=None,
        help="credentials.json из aggregate_warm_inventory.py — warm-pool "
             "bundles с метками 'assigned'/'warm'. Orphan-bundles получают "
             "отдельные Subscription+Device строки на placeholder-юзера "
             "(--orphan-owner-id), с expires_at=NOW+grace. Admin-claim "
             "endpoint потом просто TRANSFER'ит subscription к реальному "
             "юзеру.",
    )
    parser.add_argument(
        "--orphan-owner-id", type=int, default=999999,
        help="user_id placeholder-юзера для orphan-подписок. "
             "Создаётся в users со специальным telegram_id="
             "'__recovery_orphans__'. Дефолт 999999 — далеко от auto-increment.",
    )
    parser.add_argument(
        "--plan-duration-days", type=int, default=30,
        help="Длительность плана (Solo) в днях для расчёта expires_at "
             "user-* sub'ов: expires_at = first_provision_epoch + N дней. "
             "Дефолт 30 (Solo). Если у тебя другие планы — допилишь руками "
             "после применения SQL.",
    )
    args = parser.parse_args(argv)

    # Ключ резолвим ДО чтения входных файлов: DR-скрипт обязан падать мгновенно
    # и одинаково, а не после минуты работы и не на середине записи restore.sql.
    app_key = resolve_app_secret_key(args.app_secret_key)
    allow_plaintext = bool(args.allow_plaintext_secrets)
    if not app_key and not allow_plaintext:
        print(
            "[fatal] APP_SECRET_KEY не задан (ни --app-secret-key, ни переменная "
            "окружения). restore.sql пишет секреты в поля, которые backend читает "
            "через decrypt(): credentials.config_text (внутри UUID клиента) и "
            "reality private_key. Без ключа они лягут в новую БД открытым текстом "
            "и молча откатят фикс 223dd71.\n"
            "        Где взять: vault → deploy_app_stack_app_secret_key, либо .env "
            "на web-хосте.\n"
            "        Если ключ утерян безвозвратно — перезапусти с "
            "--allow-plaintext-secrets и сразу после restore прогони "
            "`docker compose exec -T backend python -m "
            "scripts.encrypt_legacy_secrets --apply`.",
            file=sys.stderr,
        )
        return 3
    if not app_key:
        print(
            "[warn] --allow-plaintext-secrets: секреты уедут в restore.sql "
            "ОТКРЫТЫМ ТЕКСТОМ. Сам файл — секрет: не клади в git, удали с mgmt "
            "и с ноутбука после применения.",
            file=sys.stderr,
        )

    users = json.loads(args.users_json.read_text())
    nodes = json.loads(args.nodes_json.read_text())
    inventory = load_inventory(args.inventory)
    node_id_map = parse_node_id_map(args.node_id_map)
    tg_map = parse_telegram_map(args.telegram_map)

    # Назначаем id нодам. Сначала те, что заданы явно в --node-id-map,
    # потом остальные — следующими свободными int'ами.
    explicit_ids = set(node_id_map.values())
    assigned: dict[str, int] = dict(node_id_map)
    next_id = 1
    for n in nodes:
        if n["name"] in assigned:
            continue
        while next_id in explicit_ids or next_id in assigned.values():
            next_id += 1
        assigned[n["name"]] = next_id
        next_id += 1

    lines: list[str] = []
    grace = timedelta(days=args.grace_days)
    # Дефолт expires_at для случаев когда мы не знаем эпоху провижининга
    # (orphans + edge cases): NOW + grace.
    default_expires_at = (
        datetime.now(timezone.utc) + grace
    ).strftime("%Y-%m-%d %H:%M:%S+00")
    plan_duration = timedelta(days=args.plan_duration_days)

    lines.append("-- restore.sql — авто-сгенерированный recovery snapshot.")
    lines.append(f"-- generated_at: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"-- grace_days: {args.grace_days}  default_plan_id: {args.default_plan_id}")
    lines.append("-- Все INSERT'ы идемпотентны (ON CONFLICT DO NOTHING).")
    if app_key:
        # Отпечаток в шапке, чтобы при разборе полётов было видно, каким ключом
        # шифровался файл: сверить с новым .env дешевле, чем ловить 503 на сабах.
        lines.append(f"-- app_secret_key_fingerprint: {key_fingerprint(app_key)}")
    else:
        lines.append(
            "-- ВНИМАНИЕ: сгенерировано с --allow-plaintext-secrets — "
            "config_text и reality private_key лежат ОТКРЫТЫМ ТЕКСТОМ."
        )
        lines.append(
            "-- После применения обязательно: docker compose exec -T backend "
            "python -m scripts.encrypt_legacy_secrets --apply"
        )
    lines.append("BEGIN;")
    lines.append("")

    # ── vpn_nodes ────────────────────────────────────────────────────
    lines.append("-- vpn_nodes")
    for n in nodes:
        name = n["name"]
        node_id = assigned[name]
        inv = inventory.get(name) or {}
        host = inv.get("host")
        region = inv.get("region") or "unknown"
        if host is None:
            print(
                f"[warn] node {name}: нет ansible_host в inventory — "
                f"подставляем 0.0.0.0, поправь руками в БД.",
                file=sys.stderr,
            )
            host = "0.0.0.0"
        lines.append(
            "INSERT INTO vpn_nodes "
            "(id, name, region, host, ssh_port, status, is_active, created_at, updated_at) "
            f"VALUES ({node_id}, {sql_str(name)}, {sql_str(region)}, "
            f"{sql_str(host)}, 22, 'active', TRUE, NOW(), NOW()) "
            "ON CONFLICT (id) DO NOTHING;"
        )
    lines.append(
        "SELECT setval('vpn_nodes_id_seq', "
        "GREATEST((SELECT COALESCE(MAX(id),0) FROM vpn_nodes), 1));"
    )
    lines.append("")

    # ── vpn_configs ──────────────────────────────────────────────────
    # Считаем id для configs по порядку (по нодам), запоминаем "первый
    # config" каждой ноды — он используется как Device.config_id.
    # Также записываем полные параметры каждого конфига в
    # cfg_per_node_protocol — нужны для построения VLESS-URL'ов в
    # credentials-секции ниже.
    lines.append("-- vpn_configs")
    cfg_id = 1
    first_cfg_per_node: dict[str, int] = {}
    cfg_per_node_protocol: dict[tuple[str, str], dict] = {}
    skipped_no_port: list[str] = []
    for n in nodes:
        node_id = assigned[n["name"]]
        for cfg in n["configs"]:
            if cfg.get("port") is None:
                skipped_no_port.append(f"{n['name']}/{cfg.get('tag')}")
                continue
            protocol = cfg["protocol"]
            sni = cfg.get("sni")
            public_key = None
            settings: dict = {}
            if protocol == "vless-reality":
                if cfg.get("private_key"):
                    try:
                        public_key = derive_reality_public_key(cfg["private_key"])
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"[warn] derive_reality_public_key failed for "
                            f"{n['name']}/{cfg['tag']}: {exc}",
                            file=sys.stderr,
                        )
                # Берём первый shortId (бэкенд читает settings.short_id).
                short_ids = cfg.get("short_ids") or []
                short_id = next((sid for sid in short_ids if sid), "")
                # private_key — секрет: живой код кладёт его в
                # settings.private_key_enc через encrypt() (node_spawner.py), а
                # читает как decrypt(private_key_enc or private_key)
                # (provisioning._collect_site_extra_vars). Имя поля не должно
                # врать про содержимое, поэтому в плейнтекст-режиме пишем в
                # легаси-имя private_key — бэкенд понимает обе ветки.
                priv_field = "private_key_enc" if app_key else "private_key"
                settings = {
                    priv_field: protect_secret(
                        cfg.get("private_key"), app_key,
                        allow_plaintext=allow_plaintext,
                    ),
                    "public_key": public_key,
                    "short_id": short_id,
                    "server_name": sni,
                    "camo_dest": cfg.get("camo_dest"),
                }
            elif protocol == "vless-ws-cdn":
                settings = {
                    "cdn_domain": sni,
                    "ws_path": cfg.get("ws_path") or "/ws",
                }
            elif protocol == "vless-xhttp":
                settings = {
                    "domain": sni,
                    "xhttp_path": cfg.get("xhttp_path") or "/xh",
                    "xhttp_mode": cfg.get("xhttp_mode") or "auto",
                }

            # vpn_configs.protocol — PG enum, label = python NAME
            # (underscore), не value (hyphen). См. models.VPNConfigProtocol:
            # `vless_reality = "vless-reality"`, SQLAlchemy Enum() хранит
            # имя. credentials.proto ниже остаётся в hyphen-form, т.к.
            # это String column и backend сравнивает по .value (см.
            # services/provisioning.py:_VLESS_FAMILY_PROTOS).
            protocol_enum_label = protocol.replace("-", "_")
            lines.append(
                "INSERT INTO vpn_configs "
                "(id, node_id, name, protocol, port, sni, public_key, "
                "settings, is_enabled, created_at, updated_at) "
                f"VALUES ({cfg_id}, {node_id}, {sql_str(cfg['tag'])}, "
                f"{sql_str(protocol_enum_label)}, {sql_int(cfg['port'])}, "
                f"{sql_str(sni)}, {sql_str(public_key)}, "
                f"{sql_jsonb(settings)}, TRUE, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING;"
            )
            first_cfg_per_node.setdefault(n["name"], cfg_id)
            # Сохраняем полную инфу — нужна для credentials-секции
            # (построение VLESS-URL для orphan-credentials).
            cfg_per_node_protocol[(n["name"], cfg.get("tag"))] = {
                "config_id": cfg_id,
                "protocol": protocol,
                "port": cfg.get("port"),
                "sni": sni,
                "_settings_for_url": settings,
            }
            cfg_id += 1

    if skipped_no_port:
        print(
            f"[warn] {len(skipped_no_port)} configs пропущены (нет port): "
            f"{', '.join(skipped_no_port)}",
            file=sys.stderr,
        )
    lines.append(
        "SELECT setval('vpn_configs_id_seq', "
        "GREATEST((SELECT COALESCE(MAX(id),0) FROM vpn_configs), 1));"
    )
    lines.append("")

    # ── wg_exit_nodes + relay_exit_links ─────────────────────────────
    exit_id_by_name: dict[str, int] = {}
    if args.wg_json and args.wg_json.is_file():
        wg = json.loads(args.wg_json.read_text())
        wg_exits = wg.get("exits") or []
        wg_links = wg.get("links") or []

        lines.append("-- wg_exit_nodes")
        for idx, ex in enumerate(wg_exits, start=1):
            exit_id_by_name[ex["name"]] = idx
            # `or None`, а не `or ""`: encrypt_with_app_key("") успешно шифрует
            # пустую строку, и exit БЕЗ ключа уезжал в БД с «ключом» из нуля
            # байт — гейт `if not exit_node.wg_private_key_enc`
            # (provisioning.py) такой шифртекст пропускает, и WG-туннель молча
            # не поднимается. Старый warning этот случай не ловил: он требовал
            # непустой wg_private_key и срабатывал только когда не задан
            # APP_SECRET_KEY, а теперь ключ проверен в начале main().
            priv_enc = protect_secret(
                ex.get("wg_private_key") or None, app_key,
                allow_plaintext=allow_plaintext,
            )
            if priv_enc is None:
                print(
                    f"[warn] wg_exit_nodes.{ex['name']}: в wg.json нет "
                    f"wg_private_key → wg_private_key_enc=NULL, туннели с этого "
                    f"exit'а не поднимутся. Допиши ключ в БД руками.",
                    file=sys.stderr,
                )
            # Для wg_exit_nodes используем DO UPDATE (а не DO NOTHING),
            # потому что при первом прогоне wg.json мог быть собран без
            # приватных ключей (ноды создались с private_key_enc=NULL) либо
            # прогон был плейнтекстовым. COALESCE(EXCLUDED, старое) берёт
            # EXCLUDED всегда, когда он не NULL, — повторный прогон с ключом
            # ЗАМЕНЯЕТ значение, а не только дошивает. Public key/host/region/
            # port менять не рискуем — оставляем что было.
            lines.append(
                "INSERT INTO wg_exit_nodes "
                "(id, name, region, host, ssh_port, wg_port, wg_address_v4, "
                "wg_public_key, wg_private_key_enc, status, is_active, "
                "created_at, updated_at) "
                f"VALUES ({idx}, {sql_str(ex['name'])}, "
                f"{sql_str(ex.get('region') or 'unknown')}, "
                f"{sql_str(ex.get('host') or '0.0.0.0')}, 22, "
                f"{sql_int(ex.get('wg_port'))}, "
                f"{sql_str(ex.get('wg_address_v4'))}, "
                f"{sql_str(ex.get('wg_public_key'))}, "
                f"{sql_str(priv_enc)}, 'active', TRUE, NOW(), NOW()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "wg_private_key_enc = COALESCE("
                "EXCLUDED.wg_private_key_enc, wg_exit_nodes.wg_private_key_enc"
                "), updated_at = NOW();"
            )
        if wg_exits:
            lines.append(
                "SELECT setval('wg_exit_nodes_id_seq', "
                "GREATEST((SELECT COALESCE(MAX(id),0) FROM wg_exit_nodes), 1));"
            )
        lines.append("")

        # relay_exit_links.wg_client_private_key_enc — NOT NULL, поэтому линки
        # без ключа пропускаем поштучно. Ветки «нет APP_SECRET_KEY → пропустить
        # всю таблицу» больше нет: ключ (или явное разрешение на плейнтекст)
        # проверен в начале main(), до чтения входных файлов.
        lines.append("-- relay_exit_links")
        link_id = 1
        skipped_links = 0
        for ln in wg_links:
            jump_id = assigned.get(ln["jump"])
            exit_id = exit_id_by_name.get(ln["exit"])
            if jump_id is None or exit_id is None:
                print(
                    f"[warn] relay_exit_link {ln['jump']}→{ln['exit']}: "
                    f"jump_id={jump_id} exit_id={exit_id} — пропуск.",
                    file=sys.stderr,
                )
                skipped_links += 1
                continue
            # `or None`, а не `or ""` — см. коммент у wg_exit_nodes. Здесь
            # ветка ниже была ПОЛНОСТЬЮ мёртвой: внутри старого else
            # app_secret_key заведомо truthy, а "" успешно шифруется, поэтому
            # линк с пустым ключом уезжал в БД с нулевым «ключом».
            priv_enc = protect_secret(
                ln.get("wg_client_private_key") or None, app_key,
                allow_plaintext=allow_plaintext,
            )
            if priv_enc is None:
                print(
                    f"[warn] relay_exit_link {ln['jump']}→{ln['exit']}: "
                    f"private key empty — пропуск.",
                    file=sys.stderr,
                )
                skipped_links += 1
                continue
            lines.append(
                "INSERT INTO relay_exit_links "
                "(id, relay_node_id, exit_id, wg_interface_name, "
                "wg_client_private_key_enc, wg_client_public_key, "
                "wg_client_address_v4, created_at) "
                f"VALUES ({link_id}, {jump_id}, {exit_id}, "
                f"{sql_str(ln.get('wg_interface_name') or 'wg0')}, "
                f"{sql_str(priv_enc)}, "
                f"{sql_str(ln.get('wg_client_public_key'))}, "
                f"{sql_str(ln.get('wg_client_address_v4'))}, "
                f"NOW()) "
                "ON CONFLICT DO NOTHING;"
            )
            link_id += 1
        lines.append(
            "SELECT setval('relay_exit_links_id_seq', "
            "GREATEST((SELECT COALESCE(MAX(id),0) FROM relay_exit_links), 1));"
        )
        if skipped_links:
            print(
                f"[warn] relay_exit_links: skipped {skipped_links}",
                file=sys.stderr,
            )
        lines.append("")

    # ── users ────────────────────────────────────────────────────────
    # Три источника, в порядке убывания авторитетности:
    #   1. --admin-users-json (живая выгрузка таблицы users из админки) —
    #      даёт telegram_id, balance, created_at для всех ~38 юзеров.
    #   2. --telegram-map (CLI override) — добивает то, чего нет в admin-json.
    #   3. xray-inventory users.json — даёт id'ы юзеров, у кого есть active sub.
    # Объединение: xray ∪ admin ∪ telegram-map. Дубли по id мёрджатся,
    # priority на admin-json для colliding полей.
    admin_users: dict[int, dict] = {}
    if args.admin_users_json and args.admin_users_json.is_file():
        admin_rows = json.loads(args.admin_users_json.read_text())
        for row in admin_rows:
            uid = int(row["id"])
            admin_users[uid] = {
                "telegram_id": row.get("telegram_id") or None,
                "balance_kopecks": _parse_balance_kopecks(row.get("balance")),
                "created_at_iso": _parse_admin_created_at(row.get("created_at")),
                "email": row.get("email") if row.get("email") not in ("—", "") else None,
            }
        print(
            f"[info] admin_users_json: {len(admin_users)} users with "
            f"balance/telegram_id/created_at",
            file=sys.stderr,
        )

    lines.append("-- users")
    inventory_user_ids = {u["user_id"] for u in users}
    cli_tg_ids = set(tg_map.keys())
    admin_ids = set(admin_users.keys())
    # Placeholder для orphan-подписок: если credentials.json передан и
    # есть assigned-bundles — добавляем спец-юзера, на которого повесим
    # все orphan-Subscription'ы. admin-claim потом TRANSFER'ит их к
    # реальному юзеру (UPDATE subscriptions SET user_id=<real>).
    has_orphans = bool(
        args.credentials_json and args.credentials_json.is_file()
        and any(
            c.get("state") == "assigned"
            for c in json.loads(args.credentials_json.read_text())
        )
    )
    all_user_ids = sorted(inventory_user_ids | cli_tg_ids | admin_ids)
    if has_orphans:
        all_user_ids = sorted(set(all_user_ids) | {args.orphan_owner_id})

    only_in_admin = admin_ids - inventory_user_ids
    if only_in_admin:
        print(
            f"[info] добавляем {len(only_in_admin)} users из admin_users.json "
            f"без записи в xray (зарегистрировались, но не имеют active sub).",
            file=sys.stderr,
        )

    # Для известных активных юзеров проставляем trial_activated_at —
    # блокирует повторное получение бонуса 150₽ через "Забери пробный
    # месяц". Считаем "активным" = есть подписка ИЛИ положительный
    # balance_kopecks. Остальные ~25 неактивных юзеров оставляют trial
    # доступным (если когда-нибудь придут — пусть получат 150₽
    # компенсации за инцидент).
    trial_blocked_ids = set(inventory_user_ids) | {
        uid for uid, info in admin_users.items()
        if (info.get("balance_kopecks") or 0) > 0
    }

    for uid in all_user_ids:
        if uid == args.orphan_owner_id:
            # Spec placeholder. telegram_id отмечен sentinel-строкой,
            # чтобы oncall/админка могли отфильтровать "не настоящий"
            # юзер. Никогда не должно быть валидного TG-id у этой строки.
            lines.append(
                "INSERT INTO users (id, telegram_id, email, created_at) "
                f"VALUES ({uid}, '__recovery_orphans__', "
                "'recovery-orphans@local', NOW()) "
                "ON CONFLICT (id) DO NOTHING;"
            )
            continue
        admin = admin_users.get(uid, {})
        # priority: admin > tg_map > NULL
        tg = admin.get("telegram_id") or tg_map.get(uid)
        balance = admin.get("balance_kopecks", 0) or 0
        created_at_sql = (
            f"TIMESTAMPTZ {sql_str(admin['created_at_iso'])}"
            if admin.get("created_at_iso")
            else "NOW()"
        )
        email = admin.get("email")
        # trial_activated_at: для активных юзеров ставим = created_at
        # (т.е. "юзер активировал trial ровно в момент регистрации",
        # что блокирует UI-баннер "забери пробный месяц"). Для
        # неактивных оставляем NULL — баннер виден, могут активировать.
        if uid in trial_blocked_ids:
            trial_at_sql = created_at_sql
        else:
            trial_at_sql = "NULL"
        lines.append(
            "INSERT INTO users (id, telegram_id, email, created_at, "
            "balance_kopecks, trial_activated_at) "
            f"VALUES ({uid}, {sql_str(tg)}, {sql_str(email)}, "
            f"{created_at_sql}, {balance}, {trial_at_sql}) "
            "ON CONFLICT (id) DO NOTHING;"
        )
    lines.append(
        "SELECT setval('users_id_seq', "
        "GREATEST((SELECT COALESCE(MAX(id),0) FROM users), 1));"
    )
    lines.append("")

    # ── subscriptions ────────────────────────────────────────────────
    lines.append("-- subscriptions")
    # При коллизии (user_id, sub_id) на разных нодах (см. user 4/sub 18)
    # — берём самый свежий по max(timestamp в raw_emails). Если
    # raw_emails не парсятся — берём первый из users.json (он отсортирован
    # по node-name, недетерминированно к актуальности — но collision
    # это редкость и оператор сам разрулит).
    by_sub: dict[tuple[int, int], dict] = {}
    for u in users:
        key = (u["user_id"], u["subscription_id"])
        # Берём emails из devices[] (новый формат) c fallback на
        # raw_emails (legacy). Самый свежий timestamp решает, какую
        # ноду считать актуальной для дубля подписки.
        sub_emails = [
            d.get("email") for d in (u.get("devices") or [])
            if d.get("email")
        ]
        if not sub_emails:
            sub_emails = u.get("raw_emails") or []
        candidate_ts = _max_email_timestamp(sub_emails)
        existing = by_sub.get(key)
        if existing is None:
            by_sub[key] = {"record": u, "ts": candidate_ts}
            continue
        if candidate_ts and (existing["ts"] is None or candidate_ts > existing["ts"]):
            by_sub[key] = {"record": u, "ts": candidate_ts}

    for (uid, sid), wrap in sorted(by_sub.items()):
        rec = wrap["record"]
        node_id = assigned.get(rec["node"])
        if node_id is None:
            print(f"[warn] sub {sid}: node {rec['node']} unknown — skip", file=sys.stderr)
            continue
        sub_token = secrets.token_urlsafe(32)
        # expires_at = первый известный provisioning-epoch из всех
        # device-email'ов подписки + plan_duration_days. "Честная" дата
        # истечения. Если epoch вытащить не вышло — fallback default.
        emails = [
            d.get("email") for d in (rec.get("devices") or [])
            if d.get("email")
        ]
        # Backward-compat для старого формата users.json (с raw_emails).
        if not emails:
            emails = rec.get("raw_emails") or []
        first_epoch = _min_email_timestamp(emails)
        if first_epoch is not None:
            sub_expires = (
                datetime.fromtimestamp(first_epoch, tz=timezone.utc) + plan_duration
            ).strftime("%Y-%m-%d %H:%M:%S+00")
        else:
            sub_expires = default_expires_at
        # auto_renew=TRUE для known subs — иначе UI показывает "отменена"
        # (на самом деле статус active, но без auto-renew). Backend
        # сам спишет 150₽ с баланса когда наступит expires_at. Orphan
        # subs ниже остаются с auto_renew=FALSE — мы хотим, чтобы они
        # истекли через grace-период и юзер пришёл в поддержку.
        # extra_device_slots: для Solo (max_devices=1) каждое устройство
        # сверх первого = +100₽/мес. Считаем по числу device-emails в
        # текущей subscription'е. Для Family/Pro плана надо менять,
        # но дефолт сейчас Solo (--default-plan-id 1) и расчёт даёт
        # корректный baseline. Если кто-то на Pro — поправим вручную.
        device_count = len([
            d for d in (rec.get("devices") or []) if d.get("email")
        ]) or 1
        # max_devices Solo = 1, hardcode (не тащим Plan-метаданные сюда).
        # Если default_plan_id != 1, оператор корректирует SQL вручную.
        plan_max_devices = 1
        extra_slots = max(0, device_count - plan_max_devices)
        lines.append(
            "INSERT INTO subscriptions "
            "(id, user_id, plan_id, node_id, created_at, updated_at, "
            "expires_at, status, sub_token, auto_renew, extra_device_slots) "
            f"VALUES ({sid}, {uid}, {args.default_plan_id}, {node_id}, "
            f"NOW(), NOW(), {sql_str(sub_expires)}, 'active', "
            f"{sql_str(sub_token)}, TRUE, {extra_slots}) "
            "ON CONFLICT (id) DO NOTHING;"
        )
    lines.append(
        "SELECT setval('subscriptions_id_seq', "
        "GREATEST((SELECT COALESCE(MAX(id),0) FROM subscriptions), 1));"
    )
    lines.append("")

    # ── devices ──────────────────────────────────────────────────────
    # По одной строке на каждое xray.clients[]-устройство. Одна
    # подписка может иметь N устройств (юзер платит за extra_device_slots),
    # каждое со своим uuid и access_username. Также собираем
    # known_devices_meta — нужно для генерации credentials ниже.
    lines.append("-- devices (по одному на каждое xray.clients[] entry)")
    dev_id = 1
    known_devices_meta: list[dict] = []
    for (uid, sid), wrap in sorted(by_sub.items()):
        rec = wrap["record"]
        node_id = assigned.get(rec["node"])
        if node_id is None:
            continue
        config_id = first_cfg_per_node.get(rec["node"])
        devices = rec.get("devices") or []
        # Если по какой-то причине devices пуст (например, старый формат
        # users.json без поля) — fallback к legacy raw_emails/uuid.
        if not devices:
            legacy_email = (rec.get("raw_emails") or [None])[-1]
            devices = [{
                "email": legacy_email,
                "uuid": rec.get("uuid"),
                "flow": rec.get("flow"),
            }]
        for idx, dev in enumerate(devices):
            # name: 'primary' для первого, 'device-2'/'device-3'/...
            # для последующих. Юзер потом переименует через webapp.
            name = "primary" if idx == 0 else f"device-{idx + 1}"
            dev_sub_token = secrets.token_urlsafe(32)
            lines.append(
                "INSERT INTO devices "
                "(id, user_id, subscription_id, config_id, name, status, "
                "access_username, sub_token, created_at, updated_at) "
                f"VALUES ({dev_id}, {uid}, {sid}, {sql_int(config_id)}, "
                f"{sql_str(name)}, 'active', {sql_str(dev.get('email'))}, "
                f"{sql_str(dev_sub_token)}, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING;"
            )
            known_devices_meta.append({
                "device_id": dev_id,
                "subscription_id": sid,
                "user_id": uid,
                "node_name": rec["node"],
                "node_id": node_id,
                "uuid": dev.get("uuid"),
                "email": dev.get("email"),
            })
            dev_id += 1
    lines.append(
        "SELECT setval('devices_id_seq', "
        "GREATEST((SELECT COALESCE(MAX(id),0) FROM devices), 1));"
    )
    lines.append("")

    # ── known credentials (per device, per protocol of its node) ─────
    # /api/sub/{token} собирает VLESS-URL'ы юзеру через `device.credentials`.
    # Без этих строк endpoint возвращает 503 "no active endpoints" даже
    # при наличии Device row'а. Создаём по одному Credential на каждый
    # включённый протокол ноды устройства (обычно vless-reality +
    # vless-xhttp = 2 креда на девайс).
    lines.append("-- known credentials (linked to known devices)")
    known_cred_id = 1
    known_creds_emitted = 0
    for d in known_devices_meta:
        node_name = d["node_name"]
        # Перебираем все vpn_configs, которые мы создали для этой ноды.
        for (n_name, tag), cfg_info in cfg_per_node_protocol.items():
            if n_name != node_name:
                continue
            config_text = _build_vless_url_for_credential(
                cfg_info,
                host=(inventory.get(node_name) or {}).get("host") or "0.0.0.0",
                region=(inventory.get(node_name) or {}).get("region") or "",
                uuid=d["uuid"] or "",
            )
            # config_text — секрет: внутри UUID, которым клиент и авторизуется
            # на ноде. Весь живой код пишет эту колонку только через encrypt()
            # (warm_pool, provisioning), плейнтекст отсюда вернул бы в свежую
            # БД ровно то, что вычистил 223dd71.
            config_text_stored = protect_secret(
                config_text, app_key, allow_plaintext=allow_plaintext
            )
            lines.append(
                "INSERT INTO credentials "
                "(id, subscription_id, device_id, config_id, node_id, "
                "proto, config_text, access_username, pool_state, "
                "is_active, created_at, assigned_at) "
                f"VALUES ({known_cred_id}, {d['subscription_id']}, "
                f"{d['device_id']}, {sql_int(cfg_info['config_id'])}, "
                f"{d['node_id']}, {sql_str(cfg_info['protocol'])}, "
                f"{sql_str(config_text_stored)}, {sql_str(d['email'])}, "
                f"'assigned', TRUE, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING;"
            )
            known_cred_id += 1
            known_creds_emitted += 1
    if known_creds_emitted > 0:
        lines.append(
            "SELECT setval('credentials_id_seq', "
            "GREATEST((SELECT COALESCE(MAX(id),0) FROM credentials), 1));"
        )
    lines.append("")

    # ── orphan subscriptions + devices + credentials ─────────────────
    # На placeholder-юзера ({orphan_owner_id}) повесим по одной
    # Subscription + Device + N Credential rows на каждый warm-bundle
    # (assigned). Subscription.expires_at = NOW + grace, через grace
    # backend revoke'нёт девайс штатно, юзер заметит отвал,
    # обратится в поддержку → admin-claim TRANSFER'ит subscription
    # на его реальный user_id (UPDATE subscriptions SET user_id=<real>,
    # expires_at = NOW + N days).
    credentials_summary = ""
    if has_orphans:
        creds_data = json.loads(args.credentials_json.read_text())
        orphans = [c for c in creds_data if c.get("state") == "assigned"]
        # IDs orphan-сущностей начинаются с 10001, чтобы:
        # 1) не пересечься с уже использованными ID-ами user-* sub'ов
        #    (которые могут быть до 100+ судя по admin-скрину);
        # 2) визуально отличаться при ручном чтении базы.
        orphan_sub_id = 10001
        orphan_dev_id = 10001
        # Сдвигаем orphan-cred-id'ы за пределы known-cred диапазона.
        # known_cred_id здесь = "next id" после последнего known-cred'а.
        orphan_cred_id = max(known_cred_id, 10001)
        # Сортируем по (node, email) для предсказуемого вывода.
        orphans_sorted = sorted(
            orphans, key=lambda b: (b.get("node", ""), b.get("email", ""))
        )
        lines.append("-- orphan subscriptions (placeholder user, await admin-claim)")
        for bundle in orphans_sorted:
            node_name = bundle["node"]
            node_db_id = assigned.get(node_name)
            if node_db_id is None:
                print(
                    f"[warn] orphan {bundle['email']}: нет node {node_name} в map",
                    file=sys.stderr,
                )
                continue
            primary_cfg_id = first_cfg_per_node.get(node_name)
            sub_token = secrets.token_urlsafe(32)
            # expires_at для orphan'ов — NOW + grace, как ты и попросил.
            lines.append(
                "INSERT INTO subscriptions "
                "(id, user_id, plan_id, node_id, created_at, updated_at, "
                "expires_at, status, sub_token, auto_renew, notes) "
                f"VALUES ({orphan_sub_id}, {args.orphan_owner_id}, "
                f"{args.default_plan_id}, {node_db_id}, "
                f"NOW(), NOW(), {sql_str(default_expires_at)}, 'active', "
                f"{sql_str(sub_token)}, FALSE, "
                f"'recovery-orphan: warm={bundle['email']} await admin-claim') "
                "ON CONFLICT (id) DO NOTHING;"
            )
            dev_sub_token = secrets.token_urlsafe(32)
            lines.append(
                "INSERT INTO devices "
                "(id, user_id, subscription_id, config_id, name, status, "
                "access_username, sub_token, created_at, updated_at) "
                f"VALUES ({orphan_dev_id}, {args.orphan_owner_id}, "
                f"{orphan_sub_id}, {sql_int(primary_cfg_id)}, "
                f"'primary', 'active', {sql_str(bundle['email'])}, "
                f"{sql_str(dev_sub_token)}, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING;"
            )
            uuid = bundle.get("uuid") or ""
            for tag in (bundle.get("config_tags") or [bundle.get("config_tag")]):
                cfg_info = cfg_per_node_protocol.get((node_name, tag))
                if cfg_info is None:
                    continue
                config_text = _build_vless_url_for_credential(
                    cfg_info,
                    host=(inventory.get(node_name) or {}).get("host") or "0.0.0.0",
                    region=(inventory.get(node_name) or {}).get("region") or "",
                    uuid=uuid,
                )
                # Тот же секрет, что и у known-кредов (см. выше): шифруем.
                config_text_stored = protect_secret(
                    config_text, app_key, allow_plaintext=allow_plaintext
                )
                lines.append(
                    "INSERT INTO credentials "
                    "(id, subscription_id, device_id, config_id, node_id, "
                    "proto, config_text, access_username, pool_state, "
                    "is_active, created_at, assigned_at) "
                    f"VALUES ({orphan_cred_id}, {orphan_sub_id}, "
                    f"{orphan_dev_id}, {sql_int(cfg_info['config_id'])}, "
                    f"{node_db_id}, {sql_str(cfg_info['protocol'])}, "
                    f"{sql_str(config_text_stored)}, {sql_str(bundle['email'])}, "
                    f"'assigned', TRUE, NOW(), NOW()) "
                    "ON CONFLICT (id) DO NOTHING;"
                )
                orphan_cred_id += 1
            orphan_sub_id += 1
            orphan_dev_id += 1
        # setval'нем sequences тоже, чтобы новые регистрации не наехали.
        lines.append(
            "SELECT setval('subscriptions_id_seq', "
            "GREATEST((SELECT COALESCE(MAX(id),0) FROM subscriptions), 1));"
        )
        lines.append(
            "SELECT setval('devices_id_seq', "
            "GREATEST((SELECT COALESCE(MAX(id),0) FROM devices), 1));"
        )
        lines.append(
            "SELECT setval('credentials_id_seq', "
            "GREATEST((SELECT COALESCE(MAX(id),0) FROM credentials), 1));"
        )
        lines.append("")
        credentials_summary = (
            f" known_creds={known_creds_emitted}"
            f" orphan_subs={orphan_sub_id - 10001}"
            f" orphan_creds={orphan_cred_id - max(known_cred_id, 10001)}"
        )

    lines.append("COMMIT;")
    lines.append("")

    args.output.write_text("\n".join(lines))

    wg_summary = ""
    if args.wg_json and args.wg_json.is_file():
        wg = json.loads(args.wg_json.read_text())
        wg_summary = (
            f" wg_exits={len(wg.get('exits') or [])}"
            f" wg_links={len(wg.get('links') or [])}"
        )

    users_with_tg = sum(
        1 for uid in all_user_ids
        if admin_users.get(uid, {}).get("telegram_id") or tg_map.get(uid)
    )
    users_with_balance = sum(
        1 for uid in all_user_ids
        if (admin_users.get(uid, {}).get("balance_kopecks") or 0) > 0
    )

    print(
        f"nodes={len(nodes)} configs_emitted={cfg_id - 1} "
        f"users={len(all_user_ids)} (with_tg={users_with_tg}, "
        f"with_balance={users_with_balance}) "
        f"subscriptions={len(by_sub)} devices={dev_id - 1}"
        f"{credentials_summary}{wg_summary} "
        f"secrets={'encrypted:' + key_fingerprint(app_key) if app_key else 'PLAINTEXT'}"
        f" -> {args.output}",
        file=sys.stderr,
    )
    return 0


def _build_vless_url_for_credential(
    cfg: dict, host: str, region: str, uuid: str
) -> str:
    """Построить vless:// URL для credential.config_text.

    Лифт логики из backend/app/services/provisioning.py — _build_vless_*_credential.
    Если когда-нибудь там поменяют схему/параметры — сверь и тут.
    """
    from urllib.parse import quote as urlquote

    protocol = cfg.get("protocol") or ""
    port = cfg.get("port") or 443
    sni = cfg.get("sni") or ""
    settings = cfg.get("_settings_for_url") or {}
    region_tag = region or "unknown"

    if protocol == "vless-reality":
        params = {
            "encryption": "none",
            "security": "reality",
            "sni": sni,
            "pbk": settings.get("public_key") or "",
            "sid": settings.get("short_id") or "",
            "flow": "xtls-rprx-vision",
            "fp": "chrome",
            "type": "tcp",
        }
        q = "&".join(f"{k}={v}" for k, v in params.items() if v)
        return f"vless://{uuid}@{host}:{port}?{q}#reality-{region_tag}"

    if protocol == "vless-xhttp":
        domain = sni or host
        path = settings.get("xhttp_path") or "/xh"
        mode = settings.get("xhttp_mode") or "auto"
        params = {
            "encryption": "none",
            "security": "tls",
            "sni": domain,
            "fp": "chrome",
            "type": "xhttp",
            "host": domain,
            "path": urlquote(path),
            "mode": mode,
        }
        q = "&".join(f"{k}={v}" for k, v in params.items() if v)
        return f"vless://{uuid}@{domain}:{port}?{q}#xhttp-{region_tag}"

    if protocol == "vless-ws-cdn":
        cdn_domain = sni or host
        path = settings.get("ws_path") or "/ws"
        params = {
            "security": "tls",
            "sni": cdn_domain,
            "fp": "chrome",
            "type": "ws",
            "host": cdn_domain,
            "path": urlquote(path),
        }
        q = "&".join(f"{k}={v}" for k, v in params.items() if v)
        return f"vless://{uuid}@{cdn_domain}:{port}?{q}#ws-cdn-{region_tag}"

    # Unknown protocol — placeholder. Backend не должен использовать этот
    # credential для нового назначения (state=assigned). UUID оставляем прямо
    # в строке, и это не косметика: admin-claim ищет кред ТОЛЬКО по
    # config_text (api/admin_claim.py), других лукапов по UUID нет. В
    # access_username UUID не найдётся никогда — там xray-овый email
    # (`warm-<node_id>-<hex>` у warm-бандлов, `user-<uid>-<sid>-<epoch>-<nonce>`
    # у известных девайсов).
    return f"placeholder:warm-recovery:{uuid}"


def _parse_balance_kopecks(raw: str | None) -> int:
    """«99476.81 ₽» → 9947681. NULL/«0.00 ₽»/мусор → 0."""
    if not raw:
        return 0
    s = raw.replace("₽", "").replace(",", ".").replace(" ", "").strip()
    if not s:
        return 0
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return 0


def _parse_admin_created_at(raw: str | None) -> str | None:
    """«16.05.2026, 10:21» → ISO8601 для TIMESTAMPTZ.

    Время в админке отображается в локали юзера (по факту MSK = UTC+3).
    Здесь не пытаемся ребейзить в UTC: ставим naive timestamp, БД
    интерпретирует как локальное при чтении. Для аналитики плюс-минус
    три часа на старых регистрациях не критично, точнее — нет данных.
    """
    if not raw or raw == "—":
        return None
    s = raw.strip()
    # Снести запятую между датой и временем, если есть.
    s = s.replace(",", "")
    try:
        dt = datetime.strptime(s, "%d.%m.%Y %H:%M")
    except ValueError:
        return None
    # ISO без TZ — Postgres TIMESTAMPTZ интерпретирует как локальное.
    return dt.isoformat(sep=" ")


def _email_epochs(raw_emails: list[str]) -> list[int]:
    """Извлечь все epoch из строк ``user-N-M-{epoch}-{nonce}``.

    Пустой список, если ни одна не парсится.
    """
    out: list[int] = []
    for email in raw_emails:
        parts = email.split("-")
        if len(parts) >= 4 and parts[3].isdigit():
            out.append(int(parts[3]))
    return out


def _max_email_timestamp(raw_emails: list[str]) -> int | None:
    """Макс. epoch — для выбора самого свежего дубля подписки на нодах."""
    epochs = _email_epochs(raw_emails)
    return max(epochs) if epochs else None


def _min_email_timestamp(raw_emails: list[str]) -> int | None:
    """Мин. epoch — первое известное время провижининга подписки.

    Используется для расчёта expires_at: subscription стартовала не
    позже момента первого devices'а на ней.
    """
    epochs = _email_epochs(raw_emails)
    return min(epochs) if epochs else None


if __name__ == "__main__":
    raise SystemExit(main())
