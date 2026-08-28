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

Hysteria2 — наравне с остальными
───────────────────────────────
Xray 26.x умеет hy2 нативно (``proxy/hysteria``), а obfs живёт не в настройках
протокола, а в ``streamSettings.finalmask.udp`` как маска ``salamander`` —
наш ``obfs=salamander`` выражается полностью. Обкатка на живом устройстве
(2026-08-25) подтвердила, что ядро Happ это читает, а балансировщик пингует
hy2-леги наравне с vless: при мёртвых vless трафик уходит на hysteria.

Историческая заметка на случай регрессии: первая версия исключала hy2 с
обоснованием «формат не выражает obfs». Обоснование было ошибочным — вывод
делался по чужому конфигу, где obfs просто не включён. Если hy2 когда-нибудь
снова начнёт выпадать, ищите не в протоколе, а в ``finalmask``.

Гейт ``MIN_BALANCER_LEGS``: профиль автовыбора с одним легом не даёт ни выбора,
ни запасного варианта, и такому устройству честнее отдать плоский список.

**Роутинг.** Наш RU-split живёт на ноде (xray routing + hysteria acl), поэтому
базово клиенту правил не передаём: всё, кроме приватных сетей, уходит в
туннель — так же, как в плоском списке. Приватные подсети — напрямую, иначе
роутер и принтеры в локалке уезжают в туннель. CIDR перечислены явно, а не
``geoip:private``, чтобы не зависеть от наличия geoip.dat у клиента.

**Правило ``ru-direct`` (гейт ``SUB_XRAY_RU_DIRECT``).** Серверный split
работает только там, где у ноды есть WG-туннель к exit'у: РУ — с IP ноды,
остальное — в туннель. На direct-нодах без туннеля (vsin-nl-01, 4vds-dk-01)
разделять нечего — ВСЁ уходит с IP самой ноды, и когда клиент сидит на таком
леге, Wildberries/Яндекс/Тинькофф видят датский или голландский адрес и
ломаются. Это не редкий угол: диверс-раскладка кладёт лег на direct-ноду
43 из 52 активных устройств (срез 2026-08-29), а access-лог 4vds-dk-01 показал
137 соединений к WB, ушедших direct-local с датского IP. Балансировщик
``leastPing`` выбирает лег по пингу и о географии не знает.

Поэтому РУ-домены уводим ещё на КЛИЕНТЕ: тот же список зон и доменов, что в
роли ``ru_direct_list`` (копия в ``ru_direct_list.py``, паритет держит тест),
и outbound ``direct`` — то есть реальный IP человека мимо VPN, на любом леге,
включая hy2 (где per-user split на ноде и не существует). ``geoip:ru`` на
клиент не кладём: это зависимость от geoip.dat в клиентском ядре; IP-хвост
(прямые коннекты без SNI) по-прежнему ловят ноды своим ``geoip:ru`` там, где
у них есть туннель. Порядок несущий: private → ru-direct → балансировщик,
first-match-wins.

**DNS.** Секции ``dns`` нет намеренно: при ``domainStrategy: "AsIs"`` домен
уходит на сервер как есть и резолвит его нода — ровно то же поведение, что у
плоских ссылок. Для ``ru-direct`` это тоже нужно: РУ-домен матчится по имени
без резолва, клиент резолвит его системным резолвером и идёт напрямую. Своя
dns-секция здесь может только сломать резолв.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import parse_qsl, unquote, urlsplit

from .ru_direct_list import xray_client_domain_rules

logger = logging.getLogger(__name__)

# Клиенты, которые ПО ФОРМАТУ способны читать Xray-JSON вместо списка ссылок.
# Остальные (Hiddify, Streisand, v2rayNG, unknown) получают прежнее base64-тело.
XRAY_JSON_CLIENTS = ("happ", "v2raytun")


def _enabled_clients() -> tuple[str, ...]:
    """Кому из умеющих реально отдаём — ``SUB_XRAY_JSON_CLIENTS``.

    Отдельный рычаг от режима: «формат клиент понимает» и «мы проверили это на
    живом устройстве» — разные утверждения. На выкатке 2026-08-25 обкатан был
    только Happ, поэтому в проде список сужен до него; v2rayTun добавляется
    строкой в group_vars, когда кто-нибудь подтвердит его на своём телефоне.

    Пусто = все умеющие (поведение по умолчанию, если рычаг не трогали).
    """
    raw = (os.getenv("SUB_XRAY_JSON_CLIENTS") or "").strip().lower()
    if not raw:
        return XRAY_JSON_CLIENTS
    picked = tuple(c.strip() for c in raw.split(",") if c.strip() in XRAY_JSON_CLIENTS)
    return picked or XRAY_JSON_CLIENTS

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
    if client not in _enabled_clients():
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


def hy2_hop_enabled(token: str) -> bool:
    """Включать ли порт-хоппинг для hy2-легов этой подписки.

    ``off`` (дефолт) / ``all`` / CSV саб-токенов — та же форма, что у
    ``SUB_HAPP_AUTOCONNECT``. Отдельный гейт от самого JSON, потому что
    хоппинг упирается не в клиента, а в НОДУ: пакет на порт из диапазона
    доедет только там, где стоит DNAT-правило ``hy2-port-hopping``. На
    2026-08-25 оно есть не на всех нодах (на части флота не переживает
    ребут — не установлен netfilter-persistent), поэтому включать хоппинг
    вслепую значит менять рабочий hy2-лег на молчащий.
    """
    return _token_gate("SUB_XRAY_HY2_HOP", token)


def ru_direct_enabled(token: str) -> bool:
    """Класть ли в профили этой подписки клиентское правило ``ru-direct``.

    ``off`` (дефолт) / ``all`` / CSV саб-токенов — та же форма, что у
    ``SUB_XRAY_HY2_HOP``. Отдельный гейт, потому что правило меняет
    поведение, которое человек ВИДИТ: РУ-сайты начинают открываться с его
    домашнего IP вместо IP ноды. На relay-нодах это ничего не меняет (там
    split уже делала нода), а на direct-нодах и hy2 — чинит; но проверить
    надо на живом телефоне, а не выкатывать всем сразу. Подробно — «Правило
    ``ru-direct``» в модульном докстринге.
    """
    return _token_gate("SUB_XRAY_RU_DIRECT", token)


def _token_gate(env_name: str, token: str) -> bool:
    """Общий разбор пер-подписочных рычагов: off / all / CSV саб-токенов."""
    raw = (os.getenv(env_name) or "off").strip()
    low = raw.lower()
    if low in ("", "0", "off", "no", "false"):
        return False
    if low in ("all", "on", "true", "yes"):
        return True
    return bool(token) and token in {t.strip() for t in raw.split(",") if t.strip()}


# Как часто клиент перескакивает на другой порт. Xray требует минимум 5 с;
# разброс, а не фиксированное значение, чтобы смена портов не давала
# наблюдателю ровный период.
_HOP_INTERVAL = "10-30"


def _hysteria_outbound(
    u, q: dict, tag: str, hostname: str, port: int, *, hop: bool = False
) -> dict:
    """Наш ``hy2://`` → hysteria-outbound Xray.

    Аутентификация у нас — пара ``логин:пароль`` в userinfo (не UUID, как у
    большинства панелей), и в конфиг она уезжает одной строкой.

    obfs живёт НЕ в настройках протокола, а в ``finalmask.udp``: salamander —
    это маска UDP-пакетов, накладываемая поверх транспорта. Без неё сервер
    просто не ответит — на ноде obfs включён.
    """
    auth = unquote(u.netloc.split("@", 1)[0])
    tls: dict = {"serverName": q.get("sni") or hostname, "allowInsecure": False}
    # h3 обязателен: hysteria2 ходит поверх QUIC, и без явного alpn часть
    # серверных стеков рвёт хендшейк.
    tls["alpn"] = ["h3"]

    stream: dict = {
        "network": "hysteria",
        "hysteriaSettings": {"version": 2, "auth": auth},
        "security": "tls",
        "tlsSettings": tls,
    }
    obfs_password = q.get("obfs-password")
    final_mask: dict = {}
    if q.get("obfs") == "salamander" and obfs_password:
        final_mask["udp"] = [
            {"type": "salamander", "settings": {"password": obfs_password}}
        ]
    elif q.get("obfs"):
        # Незнакомая обфускация: выразить не можем, а без неё сервер не ответит.
        logger.warning(
            "xray-json: unsupported hy2 obfs %r (tag=%s) — leg dropped", q["obfs"], tag
        )
        return {}

    # Порт-хоппинг: клиент рассылает пакеты по случайным портам диапазона,
    # чтобы блокировка одного порта не убивала канал. На ноде это ловит
    # DNAT-правило ``hy2-port-hopping``; там, где правила нет, включение
    # хоппинга превращает рабочий лег в молчащий — отсюда отдельный гейт.
    if hop and q.get("mport"):
        final_mask["quicParams"] = {
            "udpHop": {"ports": q["mport"], "interval": _HOP_INTERVAL}
        }

    if final_mask:
        stream["finalmask"] = final_mask

    return {
        "tag": tag,
        "protocol": "hysteria",
        "settings": {
            "address": hostname, "port": port, "version": 2, "auth": auth,
        },
        "streamSettings": stream,
    }


def uri_to_outbound(uri: str, tag: str, *, hy2_hop: bool = False) -> dict | None:
    """Один наш URI → один outbound Xray, или None если лег не выразим.

    Разбираем то, что реально минтим (``provisioning.py``, секция Credential
    builders): reality поверх tcp, tls поверх xhttp и ws, плюс hysteria2 с
    salamander-обфускацией. Неизвестный транспорт — None: лучше потерять лег,
    чем собрать outbound наугад и отдать сервер, который не подключается.

    ``hy2_hop`` — добавлять ли порт-хоппинг hy2-легам (см. ``hy2_hop_enabled``).
    """
    # ``.port`` и ``.hostname`` у SplitResult — ленивые свойства: они бросают
    # ValueError уже ПОСЛЕ разбора, на кривом порту («:abc», «:99999»). Поэтому
    # в try завёрнут не только urlsplit, но и первое обращение к ним.
    try:
        u = urlsplit(uri)
        if u.scheme not in ("vless", "hy2") or not u.hostname or not u.username:
            return None
        port = u.port or 443
        hostname = u.hostname
    except ValueError:
        logger.warning("xray-json: unparsable uri (tag=%s) — leg dropped", tag)
        return None

    q = dict(parse_qsl(u.query))

    if u.scheme == "hy2":
        return _hysteria_outbound(u, q, tag, hostname, port, hop=hy2_hop) or None

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


def _ru_direct_rule() -> dict:
    """РУ-домены — мимо VPN, с реального IP человека (см. ``ru_direct_enabled``).

    Список — байт-в-байт тот же, что ноды рендерят из роли ``ru_direct_list``
    (``regexp:\\.<зона>$`` + ``domain:<домен>``). Матч по имени: для TLS/QUIC
    это SNI из sniffing на inbound'е, для HTTP — Host; резолв не нужен.
    """
    return {
        "type": "field",
        "ruleTag": "ru-direct",
        "domain": xray_client_domain_rules(),
        "outboundTag": "direct",
    }


def _head_rules(ru_direct: bool) -> list[dict]:
    """Правила ПЕРЕД catch-all, общие для обоих типов профилей.

    Порядок несущий (first-match-wins): приватные сети первыми — иначе
    ``ru-direct`` не при чём, но локалка утекает; ``ru-direct`` вторым —
    ниже catch-all оно никогда не сработает.
    """
    rules = [_private_rule()]
    if ru_direct:
        rules.append(_ru_direct_rule())
    return rules


def _single_profile(outbound: dict, remark: str, *, ru_direct: bool = False) -> dict:
    """Профиль на один лег — ручной выбор конкретного сервера, как сейчас."""
    proxy = dict(outbound, tag="proxy")
    return {
        "remarks": remark,
        "log": {"loglevel": "warning"},
        "inbounds": _inbounds(),
        "outbounds": [proxy] + _tail_outbounds(),
        "routing": {
            "domainStrategy": "AsIs",
            "rules": _head_rules(ru_direct) + [
                {"type": "field", "network": "tcp,udp", "outboundTag": "proxy"},
            ],
        },
    }


def _auto_profile(outbounds: list[dict], *, ru_direct: bool = False) -> dict:
    """Профиль автовыбора: ядро пингует все леги и берёт самый быстрый живой."""
    first = outbounds[0]["tag"]
    return {
        "remarks": _AUTO_REMARK,
        "log": {"loglevel": "warning"},
        "inbounds": _inbounds(),
        "outbounds": list(outbounds) + _tail_outbounds(),
        "routing": {
            "domainStrategy": "AsIs",
            "rules": _head_rules(ru_direct) + [
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


def build_profiles(configs, token: str = "") -> list[dict] | None:
    """Список профилей для клиента — или None, если JSON тут не уместен.

    ``configs`` — те же ``SubLinkConfig``, что идут в плоское тело, в том же
    порядке (reality → hy2 → xhttp → ws-cdn), с уже переписанными именами
    ролей во фрагменте.

    None означает «отдай обычную подписку»: столько vless-легов, что автовыбор
    не из чего собрать. Решение принимается ЗДЕСЬ, а не на вызывающей стороне,
    чтобы гейт нельзя было обойти мимо этой функции.
    """
    hop = hy2_hop_enabled(token)
    ru_direct = ru_direct_enabled(token)
    outbounds: list[dict] = []
    remarks: list[str] = []
    for cfg in configs:
        tag = _LEG_TAG if not outbounds else f"{_LEG_TAG}-{len(outbounds) + 1}"
        ob = uri_to_outbound(cfg.uri, tag, hy2_hop=hop)
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

    profiles = [_auto_profile(outbounds, ru_direct=ru_direct)]
    profiles += [
        _single_profile(ob, remark, ru_direct=ru_direct)
        for ob, remark in zip(outbounds, remarks)
    ]
    return profiles


def build_body(configs, token: str = "") -> str | None:
    """Готовое тело ответа — JSON-массив профилей. None = отдать плоский список."""
    profiles = build_profiles(configs, token)
    if profiles is None:
        return None
    return json.dumps(profiles, ensure_ascii=False)
