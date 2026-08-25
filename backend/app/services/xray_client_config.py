"""Сборка клиентского Xray-JSON из наших URI — подписка с автовыбором.

Зачем это вообще существует
───────────────────────────
Плоский список ссылок не даёт фейловера ВНУТРИ сессии: пока человек сам не
переподключится, он сидит на упавшей ноде. ``subscription-autoconnect-type:
lowestdelay`` (наш нынешний максимум) — это подсказка Happ на момент
подключения, а не живой url-test.

Xray-JSON закрывает ровно эту дыру без собственного клиента: ядро само
пингует каждый лег (``observatory``) и уводит трафик на самый быстрый живой
(``balancers`` + ``leastPing``). Happ и v2rayTun такой конфиг читают — вместе
это большинство базы. Разбор партнёрской подписки (Remnawave, 2026-08-25)
подтвердил формат на живом сервисе.

Что НЕ переносится и почему
───────────────────────────
**Hysteria2.** У Xray нет штатного hysteria-outbound; ядра, которые его
понимают, описывают лег как ``protocol: "hysteria"`` + ``hysteriaSettings``,
и в этой схеме НЕТ места для obfs. Наши hy2-леги поднимаются с
``obfs=salamander`` (см. ``node_spawner``: obfs-пароль генерится на ноду), то
есть перенос дал бы синтаксически валидный, но заведомо не подключающийся
outbound. Поэтому hy2 в JSON не идёт вовсе — ни в балансировщик, ни отдельным
профилем: молча отдать нерабочий сервер хуже, чем не отдать его совсем.

Практическое следствие: пока лег-набор не переедет на hy2 без obfs, JSON-режим
показывает МЕНЬШЕ серверов, чем плоский список. Ради этого гейт
``MIN_BALANCER_LEGS``: профиль с одним vless-легом не даёт ни автовыбора, ни
запасного варианта, и такому устройству честнее отдать обычную подписку.

**Роутинг.** Наш RU-split живёт на ноде (xray routing + hysteria acl), поэтому
клиенту правил не передаём: всё, кроме приватных сетей, уходит в туннель — так
же, как в плоском списке. Единственное правило — приватные подсети напрямую,
иначе роутер и принтеры в локалке уезжают в туннель. CIDR перечислены явно, а
не ``geoip:private``, чтобы не зависеть от наличия geoip.dat у клиента.

**DNS.** Секции ``dns`` нет намеренно: при ``domainStrategy: "AsIs"`` домен
уходит на сервер как есть и резолвит его нода — ровно то же поведение, что у
плоских ссылок. Своя dns-секция здесь может только сломать резолв.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import parse_qsl, unquote, urlsplit

logger = logging.getLogger(__name__)

# Клиенты, которые читают Xray-JSON вместо списка ссылок. Остальные (Hiddify,
# Streisand, v2rayNG, unknown) получают прежнее base64-тело.
XRAY_JSON_CLIENTS = ("happ", "v2raytun")

# Балансировать нечего, если лег один: автовыбор из одного элемента — это
# просто тот же сервер, зато без запасного. Гейт защищает от «включили флаг и
# у части людей подписка стала беднее».
MIN_BALANCER_LEGS = 2

# Префикс тегов легов. Селектор балансировщика в Xray матчит по ПРЕФИКСУ,
# поэтому один "main" в selector накрывает main / main-2 / main-3.
_LEG_TAG = "main"

# Приватные и служебные диапазоны — мимо туннеля. Список тот же, что у
# отраслевых шаблонов: RFC1918 + loopback + link-local + CGNAT + multicast и
# их IPv6-аналоги.
_PRIVATE_NETS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "224.0.0.0/4",
    "255.255.255.255/32",
    "0.0.0.0/8",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)

# Пинг-проба балансировщика. 204-эндпоинт Google отвечает пустым телом и живёт
# без блокировок в тех странах, куда мы вообще ставим ноды.
_PROBE_URL = "https://www.gstatic.com/generate_204"

# Интервал пробы — это не бесплатный таймер, а трафик, и притом ДВАЖДЫ наш:
# с ``enableConcurrency`` ядро дёргает КАЖДЫЙ лег каждые N секунд, и каждая
# проба — полноценный TLS-хендшейк, уходящий через ноду. При 120 с и трёх
# легах набегает порядка полугигабайта в месяц на устройство: заметно и для
# мобильного пакета человека, и для наших нод.
#
# 300 с выбраны как компромисс: столько же стоит url-test в sing-box у
# сервисов, живущих на этом формате. Платим за это задержкой реакции —
# упавший лег замечается за время до одной пробы, а не мгновенно.
_PROBE_INTERVAL_DEFAULT_S = 300


def _probe_interval() -> str:
    """Период пробы в формате Xray (``"300s"``). Ниже 30 с не опускаем: там
    трафик пробы начинает соперничать с полезным."""
    raw = (os.getenv("SUB_XRAY_PROBE_INTERVAL_S") or "").strip()
    try:
        seconds = int(raw) if raw else _PROBE_INTERVAL_DEFAULT_S
    except ValueError:
        logger.warning(
            "xray-json: bad SUB_XRAY_PROBE_INTERVAL_S=%r — using %ds",
            raw, _PROBE_INTERVAL_DEFAULT_S,
        )
        seconds = _PROBE_INTERVAL_DEFAULT_S
    return f"{max(seconds, 30)}s"

_AUTO_REMARK = "🎯 Автовыбор"


def xray_json_mode() -> str:
    """``off`` / ``allowlist`` / ``on`` — кому отдаём JSON вместо ссылок.

    Флагом, а не выкаткой на всех сразу: формат меняет ВЕСЬ профиль в клиенте,
    и первую проверку надо делать на конкретных устройствах. ``allowlist`` —
    режим обкатки: JSON едет только токенам из ``SUB_XRAY_JSON_TOKENS``.
    """
    mode = (os.getenv("SUB_XRAY_JSON") or "off").strip().lower()
    return mode if mode in ("off", "allowlist", "on") else "off"


def _allowed_tokens() -> set[str]:
    raw = os.getenv("SUB_XRAY_JSON_TOKENS") or ""
    return {t.strip() for t in raw.split(",") if t.strip()}


def wants_xray_json(client: str, token: str) -> bool:
    """Отдавать ли этому запросу Xray-JSON.

    Три условия и все обязательны: клиент умеет читать такой конфиг, режим
    включён, и (в режиме обкатки) токен в списке. Любое несовпадение —
    прежнее поведение до байта.
    """
    if client not in XRAY_JSON_CLIENTS:
        return False
    mode = xray_json_mode()
    if mode == "on":
        return True
    if mode == "allowlist":
        return bool(token) and token in _allowed_tokens()
    return False


def _tls_settings(sni: str, fingerprint: str) -> dict:
    out: dict = {"serverName": sni, "allowInsecure": False}
    if fingerprint:
        out["fingerprint"] = fingerprint
    return out


def uri_to_outbound(uri: str, tag: str) -> dict | None:
    """Один наш ``vless://`` → один outbound Xray. Не-vless → None.

    Разбираем только то, что реально минтим (``provisioning.py``, секция
    Credential builders): reality поверх tcp, tls поверх xhttp и tls поверх ws.
    Неизвестный транспорт — тоже None: лучше потерять лег, чем собрать
    outbound наугад и получить профиль, который не подключается.
    """
    # ``.port`` и ``.hostname`` у SplitResult — ленивые свойства: они бросают
    # ValueError уже ПОСЛЕ разбора, на кривом порту («:abc», «:99999»). Поэтому
    # в try завёрнут не только urlsplit, но и первое обращение к ним.
    try:
        u = urlsplit(uri)
        if u.scheme != "vless" or not u.hostname or not u.username:
            return None
        port = u.port or 443
        hostname = u.hostname
    except ValueError:
        logger.warning("xray-json: unparsable uri (tag=%s) — leg dropped", tag)
        return None

    q = dict(parse_qsl(u.query))
    security = q.get("security", "")
    network = q.get("type", "")
    sni = q.get("sni") or hostname
    fingerprint = q.get("fp", "")

    user: dict = {"id": u.username, "encryption": q.get("encryption", "none")}
    # flow есть только у reality-легов (vision поверх tcp); у tls-флаворов его
    # нет, и ставить пустую строку нельзя — часть ядер такой профиль отвергает.
    if q.get("flow"):
        user["flow"] = q["flow"]

    stream: dict = {"network": network}

    if network == "tcp":
        stream["tcpSettings"] = {}
    elif network == "ws":
        ws: dict = {"path": unquote(q.get("path", "/"))}
        host = q.get("host")
        if host:
            # Host дублируется намеренно, в двух формах сразу. Xray ≥ 26 ждёт
            # независимое поле ``host`` и ругается на ``headers.Host`` как на
            # устаревшее, но ядра постарше знают только ``headers``. Обе формы
            # вместе конфигом принимаются (проверено на 26.3.27), а потеря
            # Host — это не деградация, а мёртвый лег: nginx на ноде разводит
            # ws-запросы именно по нему.
            ws["host"] = host
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "xhttp":
        xh: dict = {"path": unquote(q.get("path", "/"))}
        if q.get("host"):
            xh["host"] = q["host"]
        if q.get("mode"):
            xh["mode"] = q["mode"]
        stream["xhttpSettings"] = xh
    else:
        logger.warning(
            "xray-json: unsupported transport %r (tag=%s) — leg dropped", network, tag
        )
        return None

    if security == "reality":
        pbk = q.get("pbk")
        if not pbk:
            logger.warning("xray-json: reality leg without pbk (tag=%s)", tag)
            return None
        reality: dict = {"serverName": sni, "publicKey": pbk}
        if q.get("sid"):
            reality["shortId"] = q["sid"]
        if fingerprint:
            reality["fingerprint"] = fingerprint
        if q.get("spx"):
            reality["spiderX"] = q["spx"]
        stream["security"] = "reality"
        stream["realitySettings"] = reality
    elif security == "tls":
        stream["security"] = "tls"
        stream["tlsSettings"] = _tls_settings(sni, fingerprint)
    else:
        logger.warning(
            "xray-json: unsupported security %r (tag=%s) — leg dropped", security, tag
        )
        return None

    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [{"address": hostname, "port": port, "users": [user]}]
        },
        "streamSettings": stream,
    }


def _remark_of(uri: str, fallback: str) -> str:
    """Имя профиля берём из ``#fragment`` — там уже стоит роль от ``_relabel_uri``."""
    frag = uri.split("#", 1)[1] if "#" in uri else ""
    return unquote(frag).strip() or fallback


def _inbounds() -> list[dict]:
    """Локальные входы клиента. Мобильные клиенты обычно ставят свои, но без
    секции часть ядер отказывается стартовать."""
    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic"]}
    return [
        {
            "tag": "socks",
            "port": 10808,
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"udp": True, "auth": "noauth"},
            "sniffing": sniffing,
        },
        {
            "tag": "http",
            "port": 10809,
            "listen": "127.0.0.1",
            "protocol": "http",
            "settings": {"allowTransparent": False},
            "sniffing": dict(sniffing),
        },
    ]


def _tail_outbounds() -> list[dict]:
    return [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
    ]


def _private_rule() -> dict:
    return {
        "type": "field",
        "ruleTag": "private-direct",
        "ip": list(_PRIVATE_NETS),
        "outboundTag": "direct",
    }


def _single_profile(outbound: dict, remark: str) -> dict:
    """Профиль на один лег — ручной выбор конкретного сервера, как сейчас."""
    proxy = dict(outbound, tag="proxy")
    return {
        "remarks": remark,
        "log": {"loglevel": "warning"},
        "inbounds": _inbounds(),
        "outbounds": [proxy] + _tail_outbounds(),
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                _private_rule(),
                {"type": "field", "network": "tcp,udp", "outboundTag": "proxy"},
            ],
        },
    }


def _auto_profile(outbounds: list[dict]) -> dict:
    """Профиль автовыбора: ядро пингует все леги и берёт самый быстрый живой."""
    first = outbounds[0]["tag"]
    return {
        "remarks": _AUTO_REMARK,
        "log": {"loglevel": "warning"},
        "inbounds": _inbounds(),
        "outbounds": list(outbounds) + _tail_outbounds(),
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                _private_rule(),
                {"type": "field", "network": "tcp,udp", "balancerTag": "auto"},
            ],
            "balancers": [
                {
                    "tag": "auto",
                    # Префиксный матч: один "main" накрывает все леги набора.
                    "selector": [_LEG_TAG],
                    "strategy": {"type": "leastPing"},
                    # Пока проба не прошла ни по одному легу (первые секунды
                    # после старта), трафик идёт сюда, а не в никуда.
                    "fallbackTag": first,
                }
            ],
        },
        "observatory": {
            "probeUrl": _PROBE_URL,
            "probeInterval": _probe_interval(),
            "subjectSelector": [_LEG_TAG],
            "enableConcurrency": True,
        },
    }


def build_profiles(configs) -> list[dict] | None:
    """Список профилей для клиента — или None, если JSON тут не уместен.

    ``configs`` — те же ``SubLinkConfig``, что идут в плоское тело, в том же
    порядке (reality → hy2 → xhttp → ws-cdn), с уже переписанными именами
    ролей во фрагменте.

    None означает «отдай обычную подписку»: столько vless-легов, что автовыбор
    не из чего собрать. Решение принимается ЗДЕСЬ, а не на вызывающей стороне,
    чтобы гейт нельзя было обойти мимо этой функции.
    """
    outbounds: list[dict] = []
    remarks: list[str] = []
    for cfg in configs:
        tag = _LEG_TAG if not outbounds else f"{_LEG_TAG}-{len(outbounds) + 1}"
        ob = uri_to_outbound(cfg.uri, tag)
        if ob is None:
            continue
        outbounds.append(ob)
        remarks.append(_remark_of(cfg.uri, tag))

    if len(outbounds) < MIN_BALANCER_LEGS:
        logger.info(
            "xray-json: only %d usable leg(s) of %d — falling back to plain list",
            len(outbounds), len(configs),
        )
        return None

    profiles = [_auto_profile(outbounds)]
    profiles += [
        _single_profile(ob, remark) for ob, remark in zip(outbounds, remarks)
    ]
    return profiles


def build_body(configs) -> str | None:
    """Готовое тело ответа — JSON-массив профилей. None = отдать плоский список."""
    profiles = build_profiles(configs)
    if profiles is None:
        return None
    return json.dumps(profiles, ensure_ascii=False)
