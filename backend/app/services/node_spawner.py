"""Orchestration for automated VPN node creation via cloud providers.

Flow:
    1. Read CloudProvider record, instantiate driver.
    2. Ask driver to create a server (blocks until running).
    3. Record VPNNode with provider metadata, status=registering.
    4. Enqueue a provisioning task that runs the Ansible ``site.yml`` on the
       new host. Once that succeeds the health checker will flip it to active.

This is intentionally synchronous with respect to cloud-API polling: creating
a single VM is a 30-90s operation and an upstream ARQ/Celery worker is the
right place to call this from so we don't block HTTP requests. For now the
API route calls it in a background thread, mirroring the existing
provisioning pattern.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..db import SessionLocal
from ..security import decrypt, encrypt
from ..time_utils import utcnow
from .ansible_runner import (
    InvalidNodeIdentity,
    validate_node_identity_fields,
    validate_node_name,
)
from .cloud import DriverError, get_driver
from .provisioning import ProvisioningOrchestrator
from .shadowtls import (
    DEFAULT_HANDSHAKE_DOMAIN as SHADOWTLS_DEFAULT_SNI,
    DEFAULT_PORT as SHADOWTLS_DEFAULT_PORT,
    generate_shadowtls_password,
    generate_ss_password,
)
from .vless import (
    generate_reality_keypair,
    generate_short_id,
    generate_wireguard_keypair,
)

# Reality's "borrowed" SNI. Must be real TLS 1.3 host, НЕ заблокированный
# в целевом рынке — иначе Reality handshake перестаёт выглядеть легитимным.
# Пул RU-популярных доменов, распределяется по нодам через
# ``_pick_reality_sni`` — per-node рандомизация снижает blast radius
# RKN-события по одному SNI (июнь 2025: RKN душит connections с SNI вне
# whitelist после ~15-20KB). ``REALITY_SNI`` env-override форсит один
# SNI для всех новых нод (dev/test). ``REALITY_DEST`` нацеливается
# на ``<sni>:443`` — меняйте только вместе с sni.
# Региональные пулы reality-dest: dest выбирается по стране ДЦ ноды (geo-плотный
# + плаузибл — немецкая нода фронтит немецкий сайт, а не yandex, и латентность
# хендшейк-миррора низкая). Все домены проверены на TLS1.3+HTTP/2 (обязательно
# для reality), 2026-06-17. Фолбэк (неизвестный регион / старые ноды) — РУ-пул
# (релеи в основном РУ). dest = <sni>:443 — меняешь sni, меняй вместе.
REALITY_DEST_POOLS: dict[str, tuple[str, ...]] = {
    # 2026-07-22: ротация RU-пула на свежие домены — старые (yandex/vk/mail.ru/
    # rutube/lenta) могли попасть в DPI-сигнатуру Центрального ФО (Яр/Тула, см.
    # docs/operations/regional_blocking_diag_2026_07.md). Набор пере-верифицирован
    # TLS1.3+HTTP/2 с РУ-ноды; dzen отпал (HTTP/1.1), госсайты исключены.
    # [0] = DEFAULT_REALITY_SNI.
    "ru": ("www.ozon.ru", "ya.ru", "www.wildberries.ru", "www.kinopoisk.ru", "www.avito.ru", "www.rbc.ru", "www.yandex.ru"),
    "de": ("www.bmw.de", "www.mercedes-benz.com", "www.zalando.de"),
    "nl": ("www.bol.com", "www.philips.com", "www.adyen.com"),
    "fr": ("www.louisvuitton.com", "www.decathlon.fr", "www.sncf-connect.com"),
    "cz": ("www.seznam.cz", "www.alza.cz"),
    "fi": ("www.nokia.com", "www.kone.com", "www.fortum.com"),
    "se": ("www.ikea.com", "www.volvocars.com"),
    "gb": ("www.bbc.co.uk", "www.gov.uk", "www.bt.com"),
    "es": ("www.zara.com", "www.bbva.es", "www.iberia.com"),
    "at": ("www.redbull.com", "www.swarovski.com", "www.erstegroup.com"),
    "pl": ("www.allegro.pl", "www.onet.pl"),
    "ch": ("www.nestle.com", "www.swatch.com"),
    "it": ("www.ferrari.com", "www.eni.com", "www.unicredit.it"),
}
# Плоский фолбэк-пул (РУ) — pick_reality_sni при неизвестном регионе +
# импортируется refresh_reality_dest-роутом.
REALITY_DEST_POOL: tuple[str, ...] = REALITY_DEST_POOLS["ru"]
DEFAULT_REALITY_SNI = os.getenv("REALITY_SNI") or REALITY_DEST_POOL[0]
DEFAULT_REALITY_DEST = os.getenv("REALITY_DEST", f"{DEFAULT_REALITY_SNI}:443")
DEFAULT_REALITY_PORT = int(os.getenv("REALITY_PORT", "443"))
# Порт, на котором reality слушает LOOPBACK, когда нода унифицирована на :443
# (перед ним nginx stream ssl_preread). Наружу такая нода отдаёт 443 —
# см. ``public_port`` в ensure_reality_config. На этом порту стоит весь
# текущий флот, менять без миграции существующих нод нельзя.
UNIFIED_REALITY_LISTEN_PORT = int(os.getenv("REALITY_UNIFIED_LISTEN_PORT", "9443"))
_REALITY_SNI_ENV_OVERRIDE: str | None = os.getenv("REALITY_SNI") or None

# Hysteria2 (UDP/QUIC) defaults — used by ``ensure_hysteria2_config``.
DEFAULT_HYSTERIA2_PORT = int(os.getenv("HYSTERIA2_PORT", "443"))
DEFAULT_HYSTERIA2_MBPS = int(os.getenv("HYSTERIA2_MBPS", "200"))
# Port-hopping: клиент прыгает по UDP-диапазону, роль ставит iptables DNAT
# range→hysteria2_port. Пусто = одно-портовый hy2 на самом порту.
DEFAULT_HYSTERIA2_PORT_HOPPING_RANGE = os.getenv(
    "HYSTERIA2_PORT_HOPPING_RANGE", "20000-40000"
)

logger = logging.getLogger(__name__)


class NodeSpawnError(RuntimeError):
    pass


# Заглушка host для ноды, заказанной через spawn_node_async, пока реальный IP
# не выдан хостером. Валидный IPv4 (проходит inventory-regex), SSH к нему падает
# мгновенно. Нода держится is_active=False (вне choose_node) до проставления
# настоящего IP — placeholder в credentials не попадает.
SPAWN_PLACEHOLDER_HOST = "0.0.0.0"

# Алфавит без спецсимволов — некоторые хостеры (4vps) валидируют рут-пароль и
# отклоняют спецсимволы; длина с запасом.
_PW_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_root_password(length: int = 20) -> str:
    """Рут-пароль для reinstall (провайдер сбрасывает его; нам нужно знать
    новый, чтобы зайти по паролю и переустановить provisioning-ключ)."""
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


def _display_region(driver, region_id: str) -> str:
    """Человекочитаемое имя региона по id датацентра хостера — для
    ``VPNNode.region`` (сырой id остаётся в ``provider_region``). Без этого в
    админке светилась голая «3» (id ДЦ VDSina/UFO) вместо «Russia». Резолвим
    через ``list_datacenters`` (id→country/name); драйверы без каталога ДЦ
    (hetzner и т.п.) или офлайн-сбой → фолбэк на сам id, как было раньше."""
    lister = getattr(driver, "list_datacenters", None)
    if not callable(lister):
        return str(region_id)
    try:
        for dc in lister():
            if str(dc.get("id")) == str(region_id):
                return str(dc.get("country") or dc.get("name") or region_id)
    except Exception:  # noqa: BLE001 — оффлайн-резолв имени не должен ронять заказ
        pass
    return str(region_id)


# Короткие префиксы хостеров для авто-имени ноды. billmgr — общий kind на
# UFO/AdminVPS/DataCheap → префикс берём из хоста base_url (см. _hoster_prefix).
_KIND_PREFIX = {
    "vdsina": "vdsina", "vdsina_ru": "vdsina", "4vps": "4vps", "aeza": "aeza",
    "hetzner": "hz", "vultr": "vultr", "digitalocean": "do",
}
# Страна (из _display_region) → 2-буквенный код для имени. Фолбэк — первые 2
# буквы региона (непокрытые страны).
_COUNTRY_CC = {
    "russia": "ru", "россия": "ru", "netherlands": "nl", "нидерланды": "nl",
    "denmark": "dk", "дания": "dk", "germany": "de", "германия": "de",
    "finland": "fi", "финляндия": "fi", "india": "in", "индия": "in",
    "usa": "us", "united states": "us", "сша": "us",
    "france": "fr", "франция": "fr", "poland": "pl", "польша": "pl",
    "sweden": "se", "швеция": "se", "kazakhstan": "kz", "казахстан": "kz",
    "turkey": "tr", "турция": "tr",
    "czech": "cz", "czechia": "cz", "czech republic": "cz", "чехия": "cz",
    "united kingdom": "gb", "great britain": "gb", "britain": "gb",
    "uk": "gb", "england": "gb", "великобритания": "gb",
    "spain": "es", "испания": "es", "austria": "at", "австрия": "at",
    "switzerland": "ch", "швейцария": "ch", "italy": "it", "италия": "it",
    "ireland": "ie", "ирландия": "ie", "belgium": "be", "бельгия": "be",
    "norway": "no", "норвегия": "no", "greece": "gr", "греция": "gr",
    "portugal": "pt", "португалия": "pt", "lithuania": "lt", "литва": "lt",
    "latvia": "lv", "латвия": "lv", "estonia": "ee", "эстония": "ee",
}


def _slug(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _hoster_prefix(provider) -> str:
    """Короткий префикс хостера для имени ноды. По ``kind``; для billmgr — из
    хоста ``base_url`` (``bill.ufo.hosting`` → ``ufo``); фолбэк — слаг имени
    провайдера."""
    kind = getattr(provider.kind, "value", str(provider.kind))
    if kind in _KIND_PREFIX:
        return _KIND_PREFIX[kind]
    if kind == "billmgr":
        try:
            tok = json.loads(decrypt(provider.api_token_enc) or "{}")
            host = (tok.get("base_url") or "").split("//")[-1].split("/")[0]
            parts = [
                p for p in host.split(".")
                if p not in ("bill", "www", "my", "cp", "panel", "billing")
            ]
            if parts:
                return _slug(parts[0]) or "vds"
        except Exception:  # noqa: BLE001 — не смогли распарсить → слаг имени
            pass
    return _slug(provider.name)[:8] or "node"


def _country_cc(region: str) -> str:
    r = (region or "").strip().lower()
    if r in _COUNTRY_CC:
        return _COUNTRY_CC[r]
    letters = "".join(ch for ch in r if ch.isalpha())
    return letters[:2] or "xx"


def _auto_node_name(db: Session, provider, region_display: str) -> str:
    """``<хостер>-<cc>-<NN>`` по конвенции. NN — следующий номер по ОБЕИМ
    таблицам (relay+exit), чтобы имена нод не сталкивались."""
    base = f"{_hoster_prefix(provider)}-{_country_cc(region_display)}-"
    nums: list[int] = []
    for model in (models.VPNNode, models.WGExitNode):
        for (nm,) in db.query(model.name).filter(model.name.like(base + "%")).all():
            tail = (nm or "")[len(base):]
            if tail.isdigit():
                nums.append(int(tail))
    nn = (max(nums) + 1) if nums else 1
    return f"{base}{nn:02d}"


def resolve_spawn_name(
    db: Session, provider_id: int, region: str, name: str | None = None
) -> str:
    """Имя ноды для заказа: явное от админа, иначе авто ``<хостер>-<cc>-<NN>``.
    Зовётся спавн-роутами перед заказом (autoscale-тик передаёт имя сам)."""
    if name and name.strip():
        return name.strip()
    provider = db.get(models.CloudProvider, provider_id)
    if not provider:
        raise NodeSpawnError(f"CloudProvider {provider_id} not found")
    return _auto_node_name(
        db, provider, _display_region(get_driver(provider), region)
    )


def _wait_for_ssh(
    host: str, port: int = 22, *, timeout_s: int | None = None, interval: int = 10
) -> bool:
    """Поллим TCP-доступность ``host:port``, пока не поднимется sshd.

    Свежий VPS (после заказа) и нода после reinstall грузятся несколько минут —
    bootstrap, стартовавший раньше, падает на ``No route to host`` /
    ``Connection refused``. Ждём ЗДЕСЬ, в фоновом daemon-потоке backend'а (нет
    RQ-таймаута), ПЕРЕД постановкой bootstrap-таски — тогда воркер запускает
    site.yml уже по доступной ноде и укладывается в RQ job_timeout (900s).

    Возвращает True, если порт открылся; False по таймауту (bootstrap всё равно
    ставим — key-inject/ansible-ретраи получат последний шанс).
    Окно настраивается через ``NODE_SSH_WAIT_TIMEOUT`` (сек, дефолт 480 = 8 мин).
    """
    if timeout_s is None:
        timeout_s = int(os.getenv("NODE_SSH_WAIT_TIMEOUT", "480"))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except OSError:
            time.sleep(interval)
    return False


def pick_reality_sni(db: Session, region: str | None = None) -> str:
    """Выбор reality-SNI: наименее используемый из пула СТРАНЫ ДЦ ноды
    (``REALITY_DEST_POOLS`` по cc региона; неизвестный регион → РУ-фолбэк).
    Geo-привязка убирает палево (немецкая нода не фронтит yandex) и режет
    латентность хендшейк-миррора. Разносим по SNI, чтобы RKN-событие по одному
    домену не клало весь флот. Env ``REALITY_SNI`` форсит один SNI (dev/test)."""
    if _REALITY_SNI_ENV_OVERRIDE:
        return _REALITY_SNI_ENV_OVERRIDE
    cc = _country_cc(region) if region else None
    pool = REALITY_DEST_POOLS.get(cc, REALITY_DEST_POOL) if cc else REALITY_DEST_POOL

    if cc:
        # #1-b (anti-RKN): used считаем ПО СТРАНЕ ноды (join VPNNode), а не
        # глобально, и сначала отдаём SNI, ещё НЕ занятые в этом cc. Иначе при
        # >3 параллельных TLS-хендшейках к одному SNI из одного региона (diverse-
        # саб) можно словить «сибирскую» 120s-деградацию РКН. См.
        # docs/operations/anti_rkn_upgrades_plan.md #1-b.
        rows = (
            db.query(models.VPNConfig.sni, models.VPNNode.region)
            .join(models.VPNNode, models.VPNConfig.node_id == models.VPNNode.id)
            .filter(
                models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality
            )
            .all()
        )
        used: dict[str, int] = {}
        for sni, node_region in rows:
            if _country_cc(node_region) == cc:
                used[sni] = used.get(sni, 0) + 1
        free = [s for s in pool if s not in used]
        return min(free or pool, key=lambda s: used.get(s, 0))

    # Неизвестный регион → глобальный least-used (прежнее поведение).
    used = dict(
        db.query(models.VPNConfig.sni, func.count(models.VPNConfig.id))
        .filter(models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality)
        .group_by(models.VPNConfig.sni)
        .all()
    )
    return min(pool, key=lambda s: used.get(s, 0))


def ensure_reality_config(
    db: Session,
    node: models.VPNNode,
    *,
    port: int | None = None,
    sni: str | None = None,
    dest: str | None = None,
) -> models.VPNConfig:
    """Create a VLESS Reality VPNConfig for ``node`` if it has none yet.

    Used both by the auto-spawner and by the admin endpoint that bootstraps
    Reality on manually-registered nodes. Keys are generated here so every
    caller goes through the same authoritative path (see services/vless.py).
    Idempotent: if the node already has a Reality config, returns it as-is.

    When ``sni`` не передан — выбираем из ``REALITY_DEST_POOL`` наименее
    используемый домен (per-node рандомизация). Явный ``sni`` имеет
    приоритет.
    """
    existing = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality,
        )
        .first()
    )
    if existing is not None:
        return existing

    public_key, private_key = generate_reality_keypair()
    short_id = generate_short_id()
    sni_value = sni or pick_reality_sni(db, node.region)
    # audit #81 — REALITY_DEST env-ручка раньше читалась в DEFAULT_REALITY_DEST,
    # но нигде не применялась (dest всегда уходил на sni:443) — обманчивый конфиг-
    # контракт. Теперь при активном REALITY_SNI-override (dev/test форсит единый
    # SNI) honor и REALITY_DEST: dest = DEFAULT_REALITY_DEST. Явный аргумент dest
    # имеет приоритет; в проде (без env-override) поведение прежнее — sni:443.
    if dest:
        dest_value = dest
    elif _REALITY_SNI_ENV_OVERRIDE:
        dest_value = DEFAULT_REALITY_DEST
    else:
        dest_value = f"{sni_value}:443"
    settings: dict[str, object] = {
        "private_key_enc": encrypt(private_key),
        "short_id": short_id,
        "dest": dest_value,
    }
    # 443-унификация. Если на ноде уже есть TCP-фронт (xhttp/ws-cdn), значит
    # там стоит nginx, и reality должен встать за stream ssl_preread: xray
    # слушает loopback:<port>, клиент ходит на :443, stream разводит по SNI.
    # ``public_port`` — единственный признак этого режима: по нему
    # provisioning выставляет ``reality_stream_unify`` роли и подставляет 443
    # в клиентский URI (см. provisioning.py:319, :731).
    #
    # Раньше признак не проставлял НИКТО — весь флот унифицировали разовым
    # скриптом, а нода, созданная через админку, молча получалась
    # неунифицированной: reality торчал на 9443 наружу, мимо общего :443.
    # Нода без nginx-фронта (шаблон «Reality only») остаётся в обычном режиме
    # — вешать туда stream не на чем.
    has_tcp_front = any(
        c.is_enabled
        and c.protocol
        in (
            models.VPNConfigProtocol.vless_xhttp,
            models.VPNConfigProtocol.vless_ws_cdn,
        )
        for c in node.configs
    )
    if has_tcp_front:
        settings["public_port"] = 443

    # Внутренний порт xray. В unify-режиме он ОБЯЗАН отличаться от 443:
    # nginx слушает 0.0.0.0:443, и 127.0.0.1:443 для xray — это тот же сокет,
    # то есть кто стартанул вторым, не поднимется. 9443 — то, на чём стоит
    # весь текущий флот. Явно переданный порт уважаем как есть.
    listen_port = port or (
        UNIFIED_REALITY_LISTEN_PORT if has_tcp_front else DEFAULT_REALITY_PORT
    )

    cfg = models.VPNConfig(
        node_id=node.id,
        name=f"{node.name}-vless-reality",
        protocol=models.VPNConfigProtocol.vless_reality,
        port=listen_port,
        sni=sni_value,
        public_key=public_key,
        fallback=dest_value,
        settings=settings,
        is_enabled=True,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def maybe_enable_reality_unify(db: Session, node: models.VPNNode) -> bool:
    """Догнать 443-унификацию у reality, если TCP-фронт появился ПОЗЖЕ него.

    ``ensure_reality_config`` включает режим, только когда фронт (xhttp/ws-cdn)
    уже есть на ноде. Но reality часто создаётся первым: автоспавн заводит
    ровно его и ничего больше, а протоколы дозаливают потом. Без этого хелпера
    такая нода навсегда оставалась бы с reality наружу на своём порту — мимо
    общего :443, в отличие от всего остального флота.

    Возвращает True, если что-то поменяли.

    ⚠️ Только пока у reality НЕТ выданных кредов. Смена порта меняет то, что
    вшито в клиентские URI: при переходе с публичного 9443 на loopback+443
    клиентский порт станет другим, а ``config_text`` уже розданных кредов
    сам не перепишется. Нода со своими юзерами — случай для миграции, не для
    молчаливой правки на лету.
    """
    reality = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality,
            models.VPNConfig.is_enabled.is_(True),
        )
        .first()
    )
    if reality is None or (reality.settings or {}).get("public_port"):
        return False

    has_tcp_front = any(
        c.is_enabled
        and c.protocol
        in (
            models.VPNConfigProtocol.vless_xhttp,
            models.VPNConfigProtocol.vless_ws_cdn,
        )
        for c in node.configs
    )
    if not has_tcp_front:
        return False

    issued = (
        db.query(func.count(models.Credential.id))
        .filter(
            models.Credential.config_id == reality.id,
            models.Credential.is_active.is_(True),
        )
        .scalar()
    ) or 0
    if issued:
        logger.warning(
            "node %s: reality не унифицирован на :443, но у него уже %d активных "
            "кредов — не трогаем автоматически (смена порта разошлась бы с "
            "config_text уже розданных клиентов). Нужна миграция.",
            node.id, issued,
        )
        return False

    settings = dict(reality.settings or {})
    settings["public_port"] = 443
    reality.settings = settings
    reality.port = UNIFIED_REALITY_LISTEN_PORT
    db.add(reality)
    db.commit()
    logger.info(
        "node %s: reality догнал 443-унификацию (listen %s, наружу 443)",
        node.id, UNIFIED_REALITY_LISTEN_PORT,
    )
    return True


def ensure_shadowtls_config(
    db: Session,
    node: models.VPNNode,
    *,
    port: int | None = None,
    handshake_domain: str | None = None,
    name: str | None = None,
) -> models.VPNConfig:
    """Create a ShadowTLS+SS VPNConfig for ``node`` if it has none yet.

    Mirrors :func:`ensure_reality_config`: generates both the outer
    shadow-tls password and the inner ss-rust PSK, stores them
    encrypted in ``settings``, and exposes only the handshake domain
    + port via the plain columns. The ansible role decrypts them via
    ``_collect_site_extra_vars`` on every site.yml run.
    """
    existing = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.shadowtls_ss,
        )
        .first()
    )
    if existing is not None:
        return existing

    ss_password = generate_ss_password()
    stls_password = generate_shadowtls_password()
    cfg = models.VPNConfig(
        node_id=node.id,
        name=name or f"{node.name}-shadowtls",
        protocol=models.VPNConfigProtocol.shadowtls_ss,
        port=port or SHADOWTLS_DEFAULT_PORT,
        sni=handshake_domain or SHADOWTLS_DEFAULT_SNI,
        public_key=None,
        fallback=None,
        settings={
            "ss_password_enc": encrypt(ss_password),
            "shadowtls_password_enc": encrypt(stls_password),
        },
        is_enabled=True,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def ensure_hysteria2_config(
    db: Session,
    node: models.VPNNode,
    *,
    port: int | None = None,
    sni: str | None = None,
    name: str | None = None,
) -> models.VPNConfig:
    """Create a Hysteria2 (UDP/QUIC) VPNConfig for ``node`` if it has none yet.

    Unlike reality/shadowtls, hysteria2 has NO config-level secret — auth is
    **per-user** (each Credential carries its own password, pushed to the
    node's ``auth.userpass`` via ``manage_hy2_user.sh``). So this helper only
    fills the server-side transport defaults: salamander obfs (auto-generated
    password, kept as a *pair* — obfs без obfs_password рассинхронит скрамблинг),
    bandwidth caps, UDP port-hopping range, and — crucially — the TLS cert.

    **Cert reuse (design choice).** The role's empty-``cert_path`` branch falls
    back to hysteria's *built-in* ACME, which binds :80/:443 TCP for the
    challenge — that collides with nginx on our unified combo nodes → ACME
    fails. Rather than mint a fresh ``*.wgse`` domain and bolt certbot onto the
    hy2 role, we REUSE the node's existing xhttp (or ws-cdn) Let's Encrypt cert:
    point ``cert_path``/``key_path`` at it and set ``sni`` to that same domain.
    The cert is already issued + auto-renewed by the xhttp/ws-cdn role's
    certbot, hysteria serves QUIC/TLS for that domain on UDP, and the client
    verifies a real LE chain (no ``insecure``/pin needed). No new domain, no new
    cert, no ACME↔nginx port clash.

    If the node has no cert-bearing vless-front (xhttp/ws-cdn) config, cert_path
    is left empty → hysteria's own ACME (only viable on nodes WITHOUT nginx on
    :80/:443); we log a loud warning so the operator notices.
    """
    existing = (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.hysteria2,
        )
        .first()
    )
    if existing is not None:
        return existing

    # Reuse an existing LE cert-bearing front domain (xhttp preferred, then
    # ws-cdn). Both carry a real Let's Encrypt cert issued+renewed by their
    # role's certbot on this node; hy2 borrows it for its QUIC/TLS terminus.
    cert_domain = sni
    if not cert_domain:
        for proto in (
            models.VPNConfigProtocol.vless_xhttp,
            models.VPNConfigProtocol.vless_ws_cdn,
        ):
            front = next(
                (
                    c
                    for c in node.configs
                    if c.protocol == proto and c.is_enabled and c.sni
                    # Skip a CF Origin-CA xhttp front: a non-empty cert_path means
                    # its cert lives at /etc/nginx/ssl/xhttp-origin.crt, NOT under
                    # /etc/letsencrypt/live/ — the hy2 cert_path we derive below
                    # would point at a nonexistent LE file and hysteria wouldn't
                    # start. ws-cdn is always DNS-only LE (never has cert_path),
                    # so the loop falls through to it.
                    and not (
                        proto == models.VPNConfigProtocol.vless_xhttp
                        and (c.settings or {}).get("cert_path")
                    )
                ),
                None,
            )
            if front is not None:
                cert_domain = front.sni
                break

    settings: dict[str, object] = {
        "obfs": "salamander",
        "obfs_password": secrets.token_urlsafe(16),
        "up_mbps": DEFAULT_HYSTERIA2_MBPS,
        "down_mbps": DEFAULT_HYSTERIA2_MBPS,
        "port_hopping_range": DEFAULT_HYSTERIA2_PORT_HOPPING_RANGE,
    }
    if cert_domain:
        settings["cert_path"] = f"/etc/letsencrypt/live/{cert_domain}/fullchain.pem"
        settings["key_path"] = f"/etc/letsencrypt/live/{cert_domain}/privkey.pem"
    else:
        logger.warning(
            "ensure_hysteria2_config: node %s has no cert-bearing xhttp/ws-cdn "
            "front — cert_path left empty (hysteria built-in ACME will fail if "
            "nginx owns :80/:443)",
            node.id,
        )

    cfg = models.VPNConfig(
        node_id=node.id,
        name=name or f"{node.name}-hysteria2",
        protocol=models.VPNConfigProtocol.hysteria2,
        port=port or DEFAULT_HYSTERIA2_PORT,
        sni=cert_domain or "",
        public_key=None,
        fallback=None,
        settings=settings,
        is_enabled=True,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def spawn_node(
    db: Session,
    *,
    provider_id: int,
    name: str,
    region: str,
    plan: str,
    image: str | None = None,
    ssh_key_ids: list[str] | None = None,
    pool_id: int | None = None,
    user_data: str | None = None,
    notes: str | None = None,
    reality_sni: str | None = None,
    reality_dest: str | None = None,
) -> tuple[models.VPNNode, models.ProvisioningTask]:
    provider = db.get(models.CloudProvider, provider_id)
    if not provider or not provider.is_active:
        raise NodeSpawnError(f"CloudProvider {provider_id} not found or inactive")

    # #55 — name validation runs *before* we pay for the cloud VM. If
    # the caller (autoscale, /nodes/spawn, admin script) constructed a
    # name that wouldn't be safe in an ansible inventory, fail fast —
    # renting a machine we can't provision is strictly worse than a
    # clear rejection.
    try:
        validate_node_name(name)
    except InvalidNodeIdentity as exc:
        raise NodeSpawnError(str(exc)) from exc

    driver = get_driver(provider)
    image = image or provider.default_image or "ubuntu-22.04"
    ssh_key_ids = ssh_key_ids if ssh_key_ids is not None else (provider.ssh_key_ids or [])

    # #69 — фиксируем намерение покупки в БД ДО списания денег. Строка VPNNode
    # (placeholder host, is_active=False) коммитится ПЕРЕД create_server: любой
    # сбой в окне после оплаты (упавшая валидация IP, обрыв соединения с БД,
    # убитая по таймауту RQ-джоба посреди поллинга create_server) больше не
    # оставляет оплаченный сервер-сироту — след остаётся в vpn_nodes, застрявшую
    # строку подберёт sweep_stuck_spawns. Занятость имени проверяем тоже ДО
    # оплаты: name unique=True, и IntegrityError ПОСЛЕ create_server терял бы
    # уже оплаченный сервер.
    if db.query(models.VPNNode).filter(models.VPNNode.name == name).first():
        raise NodeSpawnError(f"node name {name!r} is already taken")

    node = models.VPNNode(
        name=name,
        region=_display_region(driver, region),
        host=SPAWN_PLACEHOLDER_HOST,  # реальный IP появится после create_server
        status=models.VPNNodeStatus.registering,
        is_active=False,  # вне choose_node, пока сервер не заказан и нет IP
        pool_id=pool_id,
        provider_id=provider.id,
        provider_region=region,
        provider_plan=plan,
        notes=notes,
        health_score=100,
        last_health_check_at=utcnow(),
    )
    db.add(node)
    try:
        db.commit()
    except IntegrityError as exc:
        # Гонка по unique-имени (два спавна взяли одинаковый NN из
        # _auto_node_name). Денег ещё не потратили — безопасно отказать.
        db.rollback()
        raise NodeSpawnError(f"node name {name!r} is already taken") from exc
    db.refresh(node)

    logger.info("Spawning node %s via %s in %s", name, provider.name, region)
    try:
        server = driver.create_server(
            name=name,
            region=region,
            plan=plan,
            image=image,
            ssh_key_ids=[str(x) for x in ssh_key_ids] or None,
            user_data=user_data,
        )
    except DriverError as exc:
        logger.exception("Failed to spawn node via %s", provider.name)
        # Сервер мог быть оплачен до сбоя (например, buyServer прошёл, а
        # поллинг IP упал) — строку не удаляем, а помечаем error, чтобы
        # оператор проверил панель хостера.
        _mark_spawn_error(db, node, reason=f"create_server failed: {exc}")
        raise NodeSpawnError(str(exc)) from exc

    # #69 — привязываем external_id отдельным коротким коммитом СРАЗУ после
    # ответа драйвера, ДО валидации host: оплаченный сервер отслеживается,
    # даже если дальше что-то упадёт.
    node.provider_external_id = server.external_id
    node.provider_region = server.region
    node.provider_plan = server.plan
    node.monthly_cost = server.monthly_cost
    # Хостеры без инъекции SSH-ключа (4vps) отдают рут-пароль при заказе —
    # храним зашифрованным для SSH-bootstrap'а (см. эпик, Фаза 1.5).
    if server.root_password:
        node.provider_root_password_enc = encrypt(server.root_password)
    node.updated_at = utcnow()
    db.add(node)
    db.commit()

    # #55 — host+port re-validation once the cloud driver returns.
    # ``server.ipv4`` should always be a real IPv4 string, but any
    # future driver that returns a malformed value (or we add IPv6
    # support and forget to update one path) will still be caught
    # before the host is committed or the inventory is rendered.
    try:
        validate_node_identity_fields(name, server.ipv4, 22)
    except InvalidNodeIdentity as exc:
        # #69 — не бросаем без следа: нода уходит в error с сохранённым
        # external_id (оператор может снести её через destroy_node).
        _mark_spawn_error(db, node, reason=f"driver returned invalid host: {exc}")
        raise NodeSpawnError(
            f"cloud driver {provider.name} returned invalid host: {exc}"
        ) from exc

    node.host = server.ipv4
    node.is_active = True  # реальный IP есть → нода доступна для choose_node
    node.updated_at = utcnow()
    db.add(node)
    db.commit()
    db.refresh(node)

    # Авто-продление на стороне провайдера: чтобы заказанный VPS не удалился в
    # конце оплаченного периода (флот живёт без ручного continueServer).
    # Best-effort — не все провайдеры умеют, сбой не должен валить спавн.
    if hasattr(driver, "set_autoprolong"):
        try:
            driver.set_autoprolong(server.external_id, True)
            logger.info("autoprolong enabled for node %s (%s)", node.id, node.name)
        except Exception:  # noqa: BLE001
            logger.warning(
                "autoprolong enable failed for node %s (ignored)", node.id
            )

    # Auto-provision a VLESS+Reality VPNConfig for this node via the shared
    # helper so manual and automated registration go through the same path.
    ensure_reality_config(db, node, sni=reality_sni, dest=reality_dest)

    orchestrator = ProvisioningOrchestrator(db)
    # Свежеподнятая spawn-нода — bootstrap нужен немедленно (как в _finalize_spawn
    # ниже), НЕ дефёрим в reconciler-debounce.
    task, _created = orchestrator.create_or_coalesce_node_bootstrap(
        node, {"pool_id": pool_id, "auto_spawn": True}, defer_to_reconciler=False
    )
    db.commit()
    # #77 — bootstrap НЕ запускаем сразу: свежий VPS грузится несколько минут,
    # ранний site.yml падает на 'No route to host', нода уходит в error, а
    # автоскейл (исключающий error-ноды из cap) в следующий тик покупает ЕЩЁ
    # один сервер — цикл «купить→упасть→купить». Раньше _wait_for_ssh был только
    # в _finalize_spawn (путь spawn_node_async), а синхронный spawn_node
    # (автоскейл) стартовал bootstrap без ожидания. Дожидаемся SSH и запускаем
    # таску в фоновом daemon-потоке со своей сессией (как _finalize_spawn) —
    # синхронный контракт spawn_node (возврат готовой таски) сохраняется, а
    # долгое ожидание не держит вызывающую сессию/соединение из пула (#74).
    if _created:
        threading.Thread(
            target=_deferred_bootstrap_after_ssh,
            args=(node.id, task.id),
            daemon=True,
        ).start()
    return node, task


def _deferred_bootstrap_after_ssh(node_id: int, task_id: int) -> None:
    """Дождаться SSH на свежеспавненной ноде и запустить готовую bootstrap-таску.

    Крутится в daemon-потоке backend'а со своей сессией (как :func:`_finalize_spawn`).
    Таска уже создана и закоммичена вызывающим :func:`spawn_node` — здесь только
    ждём доступности SSH (#77) и запускаем исполнение. Транзакцию на время
    ожидания не держим (#74)."""
    session = SessionLocal()
    try:
        node = session.get(models.VPNNode, node_id)
        if not node:
            logger.error("deferred bootstrap: node %s vanished", node_id)
            return
        host = node.host
        ssh_port = node.ssh_port or 22
        # #74 — освобождаем соединение из пула ДО ожидания SSH (до 480s).
        session.rollback()

        if _wait_for_ssh(host, ssh_port):
            logger.info("spawn_node deferred bootstrap: SSH up on %s", host)
        else:
            logger.warning(
                "spawn_node deferred bootstrap: SSH on %s not up within wait "
                "window — enqueuing bootstrap anyway (key-inject/ansible will "
                "retry)", host,
            )

        node = session.get(models.VPNNode, node_id)
        task = session.get(models.ProvisioningTask, task_id)
        if not node or not task:
            logger.error(
                "deferred bootstrap: node %s / task %s vanished before enqueue",
                node_id, task_id,
            )
            return
        orchestrator = ProvisioningOrchestrator(session)
        orchestrator.run_task_async(task, node=node)
        logger.info(
            "spawn_node deferred bootstrap: node %s bootstrap enqueued", node_id
        )
    except Exception:  # noqa: BLE001
        logger.exception("deferred bootstrap crashed for node %s", node_id)
        if session.is_active:
            session.rollback()
    finally:
        session.close()


def spawn_node_async(
    db: Session,
    *,
    provider_id: int,
    name: str,
    region: str,
    plan: str,
    image: str | None = None,
    ssh_key_ids: list[str] | None = None,
    pool_id: int | None = None,
    user_data: str | None = None,
    notes: str | None = None,
    reality_sni: str | None = None,
    reality_dest: str | None = None,
) -> models.VPNNode:
    """Неблокирующий спавн для HTTP-роута ``POST /nodes/spawn``.

    ``create_server`` у всех драйверов БЛОКИРУЕТ до выдачи IP (4vps — поллинг
    /myservers до 600s). Прямой вызов в запросе убивал uvicorn-воркер по nginx
    ``proxy_read_timeout`` (60s) → CF 502; хуже того, ``buyServer`` оплачивал
    VPS ДО создания строки ``VPNNode``, и убитый запрос оставлял осиротевший,
    неотслеживаемый сервер (каждый ретрай — ещё один заказ).

    Здесь СИНХРОННО выполняем: сначала фиксируем строку ``VPNNode``
    (placeholder host, ``is_active=False``) — #69: намерение покупки в БД ДО
    оплаты, — затем быстрый ``order_server`` (buyServer, ~секунды) и сразу
    отдельным коротким коммитом привязываем ``provider_external_id`` — сервер
    привязан к строке с момента заказа, сирот нет. Долгий поллинг IP +
    bootstrap уходят в фоновый daemon-поток (:func:`_finalize_spawn`). Нода
    становится ``is_active=True`` (видимой для ``choose_node``) только когда
    проставлен реальный IP — до этого юзеры на неё не назначаются (placeholder
    не попадает в credentials).

    Драйверы без ``order_server`` (hetzner/vultr/…): здесь строка создаётся без
    ``external_id``, а полный (блокирующий) ``create_server`` уходит целиком в
    тот же фоновый поток. (Их основной путь — autoscale-тик в RQ-воркере, без
    HTTP-таймаута, через :func:`spawn_node`.)
    """
    provider = db.get(models.CloudProvider, provider_id)
    if not provider or not provider.is_active:
        raise NodeSpawnError(f"CloudProvider {provider_id} not found or inactive")

    try:
        validate_node_name(name)
    except InvalidNodeIdentity as exc:
        raise NodeSpawnError(str(exc)) from exc

    driver = get_driver(provider)
    image = image or provider.default_image or "ubuntu-22.04"
    ssh_key_ids = ssh_key_ids if ssh_key_ids is not None else (provider.ssh_key_ids or [])

    # #69 — как в spawn_node: строка-намерение коммитится ДО оплаты, занятость
    # имени проверяем до заказа (name unique=True — раньше гонка по авто-имени
    # роняла commit УЖЕ ПОСЛЕ оплаченного order_server, теряя external_id).
    if db.query(models.VPNNode).filter(models.VPNNode.name == name).first():
        raise NodeSpawnError(f"node name {name!r} is already taken")

    node = models.VPNNode(
        name=name,
        region=_display_region(driver, region),
        host=SPAWN_PLACEHOLDER_HOST,  # реальный IP проставит _finalize_spawn
        status=models.VPNNodeStatus.registering,
        is_active=False,  # вне choose_node, пока нет настоящего IP
        pool_id=pool_id,
        provider_id=provider.id,
        provider_region=region,
        provider_plan=plan,
        notes=notes,
        health_score=100,
        last_health_check_at=utcnow(),
    )
    db.add(node)
    try:
        db.commit()
    except IntegrityError as exc:
        # Гонка по unique-имени — денег ещё не потратили, безопасно отказать.
        db.rollback()
        raise NodeSpawnError(f"node name {name!r} is already taken") from exc
    db.refresh(node)

    external_id: str | None = None
    root_password: str | None = None
    # Быстрый заказ (buyServer, ~секунды) — external_id привязывается отдельным
    # коротким коммитом сразу после ответа драйвера. Capability-проверка: 4vps
    # умеет order_server.
    if hasattr(driver, "order_server"):
        logger.info("Ordering node %s via %s in %s", name, provider.name, region)
        try:
            external_id, root_password = driver.order_server(
                name=name,
                region=region,
                plan=plan,
                image=image,
                ssh_key_ids=[str(x) for x in ssh_key_ids] or None,
                user_data=user_data,
            )
        except DriverError as exc:
            logger.exception("Failed to order node via %s", provider.name)
            # Заказ мог пройти на стороне хостера до сбоя — оставляем след
            # в error, оператору: проверить панель хостера.
            _mark_spawn_error(db, node, reason=f"order_server failed: {exc}")
            raise NodeSpawnError(str(exc)) from exc
        node.provider_external_id = external_id
        if root_password:
            node.provider_root_password_enc = encrypt(root_password)
        node.updated_at = utcnow()
        db.add(node)
        db.commit()
        db.refresh(node)

    # Reality-config можно создать сразу — он node-scoped и не требует host
    # (host вшивается в credentials позже, при выдаче). Нода уже несёт целевой
    # протокол в админ-UI.
    ensure_reality_config(db, node, sni=reality_sni, dest=reality_dest)

    threading.Thread(
        target=_finalize_spawn,
        args=(node.id,),
        kwargs={
            "name": name,
            "region": region,
            "plan": plan,
            "image": image,
            "ssh_key_ids": list(ssh_key_ids) if ssh_key_ids else None,
            "user_data": user_data,
            "reality_sni": reality_sni,
            "reality_dest": reality_dest,
        },
        daemon=True,
    ).start()
    return node


def _mark_spawn_error(
    session: Session, node: models.VPNNode, reason: str | None = None
) -> None:
    """Пометить ноду error+inactive (заказ не достроился). external_id уже в
    строке → оператор может снести/переустановить, сирот не остаётся.
    ``reason`` дописывается в notes — оператору видно, на чём упал спавн."""
    node.status = models.VPNNodeStatus.error
    node.is_active = False
    if reason:
        stamp = f"[spawn-error {utcnow().isoformat()}] {reason}"
        node.notes = f"{node.notes}\n{stamp}" if node.notes else stamp
    node.updated_at = utcnow()
    session.add(node)
    session.commit()


def _finalize_spawn(
    node_id: int,
    *,
    name: str,
    region: str,
    plan: str,
    image: str,
    ssh_key_ids: list[str] | None,
    user_data: str | None,
    reality_sni: str | None,
    reality_dest: str | None,
) -> None:
    """Фоновая достройка ноды, заказанной через :func:`spawn_node_async`.

    Дожидается IP (``wait_for_ipv4`` — драйверы с ``order_server``) либо
    выполняет полный блокирующий ``create_server`` (драйверы без него),
    проставляет host + ``is_active=True`` и запускает bootstrap (site.yml).
    Крутится в daemon-потоке backend'а со своей сессией. При провале помечает
    ноду error (см. :func:`_mark_spawn_error`)."""
    session = SessionLocal()
    try:
        node = session.get(models.VPNNode, node_id)
        if not node:
            logger.error("spawn finalize: node %s vanished", node_id)
            return
        provider = session.get(models.CloudProvider, node.provider_id)
        if not provider:
            logger.error("spawn finalize: provider for node %s missing", node_id)
            _mark_spawn_error(session, node)
            return
        driver = get_driver(provider)
        # #74 — считываем нужные поля и ЗАКРЫВАЕМ транзакцию ДО долгого сетевого
        # ожидания. wait_for_ipv4 у 4vps поллит до 600s: держать всё это время
        # idle-in-transaction соединение из пула SQLAlchemy нельзя (несколько
        # параллельных спавнов исчерпали бы пул и подвесили HTTP-запросы, а
        # долгоживущий snapshot xmin блокирует autovacuum в Postgres). Драйвер
        # уже держит расшифрованный токен (get_driver не хранит ORM-ссылку), так
        # что ожидание идёт без сессии; ноду перечитываем свежей транзакцией.
        external_id = node.provider_external_id
        session.rollback()

        root_password: str | None = None
        try:
            if external_id and hasattr(driver, "wait_for_ipv4"):
                ipv4, monthly_cost, _raw = driver.wait_for_ipv4(external_id)
            else:
                # Драйвер без order_server — полный (блокирующий) заказ тут, в фоне.
                server = driver.create_server(
                    name=name,
                    region=region,
                    plan=plan,
                    image=image,
                    ssh_key_ids=[str(x) for x in (ssh_key_ids or [])] or None,
                    user_data=user_data,
                )
                ipv4 = server.ipv4
                monthly_cost = server.monthly_cost
                external_id = server.external_id
                root_password = server.root_password
        except DriverError:
            logger.exception("spawn finalize: driver failed for node %s", node_id)
            node = session.get(models.VPNNode, node_id)
            if node:
                _mark_spawn_error(session, node)
            return

        try:
            validate_node_identity_fields(name, ipv4, 22)
        except InvalidNodeIdentity:
            logger.exception("spawn finalize: invalid IPv4 %r for node %s", ipv4, node_id)
            node = session.get(models.VPNNode, node_id)
            if node:
                _mark_spawn_error(session, node)
            return

        # Свежая транзакция — перечитываем ноду и фиксируем host/external_id.
        node = session.get(models.VPNNode, node_id)
        if not node:
            logger.error("spawn finalize: node %s vanished mid-flight", node_id)
            return
        node.host = ipv4
        node.is_active = True  # реальный IP есть → нода доступна для choose_node
        node.provider_external_id = external_id
        if root_password:
            node.provider_root_password_enc = encrypt(root_password)
        if monthly_cost is not None:
            node.monthly_cost = monthly_cost
        node.updated_at = utcnow()
        session.add(node)
        session.commit()

        # Авто-продление на стороне провайдера (best-effort) — теперь точно есть
        # external_id. Сбой не должен валить достройку.
        if external_id and hasattr(driver, "set_autoprolong"):
            try:
                driver.set_autoprolong(external_id, True)
                logger.info("autoprolong enabled for node %s (%s)", node.id, node.name)
            except Exception:  # noqa: BLE001
                logger.warning("autoprolong enable failed for node %s (ignored)", node.id)

        # #74 — снова считываем host/port и завершаем транзакцию ДО ожидания SSH
        # (до NODE_SSH_WAIT_TIMEOUT=480s): держать соединение из пула всё это
        # время нельзя (тот же idle-in-transaction, что и на wait_for_ipv4 выше).
        host = node.host
        ssh_port = node.ssh_port or 22
        session.commit()

        # Ждём, пока на свежем VPS поднимется SSH, ПЕРЕД bootstrap'ом — иначе
        # site.yml стартует слишком рано и падает на 'No route to host'. Ждём
        # тут (фоновый поток, без RQ-таймаута), не в воркере.
        if _wait_for_ssh(host, ssh_port):
            logger.info("spawn finalize: SSH up on %s", host)
        else:
            logger.warning(
                "spawn finalize: SSH on %s not up within wait window — enqueuing "
                "bootstrap anyway (key-inject/ansible will retry)", host,
            )

        # Свежая транзакция для постановки bootstrap-таски.
        node = session.get(models.VPNNode, node_id)
        if not node:
            logger.error("spawn finalize: node %s vanished before bootstrap", node_id)
            return
        orchestrator = ProvisioningOrchestrator(session)
        # defer_to_reconciler=False — нода только что поднялась, bootstrap нужен
        # сразу (как в reinstall_node), не ждём reconcile-тик.
        task, created = orchestrator.create_or_coalesce_node_bootstrap(
            node, {"pool_id": node.pool_id, "auto_spawn": True},
            defer_to_reconciler=False,
        )
        session.commit()
        if created:
            orchestrator.run_task_async(task, node=node)
        logger.info(
            "spawn finalize: node %s got IP %s, bootstrap enqueued", node_id, ipv4
        )
    except Exception:  # noqa: BLE001
        logger.exception("spawn finalize crashed for node %s", node_id)
        if session.is_active:
            session.rollback()
        try:
            node = session.get(models.VPNNode, node_id)
            if node and node.status == models.VPNNodeStatus.registering:
                _mark_spawn_error(session, node)
        except Exception:  # noqa: BLE001
            session.rollback()
    finally:
        session.close()


def destroy_node(db: Session, node: models.VPNNode) -> None:
    # Снести сервер у провайдера МОЖНО только если есть и провайдер, и его
    # external_id. У упавших заказов (нода ушла в error до выдачи external_id),
    # ручных нод и нод без провайдера сносить на стороне провайдера нечего —
    # просто гасим локальную строку (раньше тут был raise → такие ноды
    # вообще не удалялись из админки, см. баг с error-нодами).
    provider = (
        db.get(models.CloudProvider, node.provider_id) if node.provider_id else None
    )
    if node.provider_external_id and provider:
        driver = get_driver(provider)
        try:
            driver.destroy_server(node.provider_external_id)
        except DriverError as exc:
            raise NodeSpawnError(str(exc)) from exc
    else:
        logger.info(
            "destroy_node %s: нет provider_external_id/провайдера — гашу локально "
            "(ничего сносить у хостера)", node.id,
        )

    node.is_active = False
    node.status = models.VPNNodeStatus.disabled
    node.updated_at = utcnow()
    db.add(node)
    db.commit()


def _reboot_target(
    db: Session, provider_id: int | None, external_id: str | None,
    host: str, ssh_port: int | None,
) -> str:
    """Перезагрузка cloud/ручной ноды без захода в панель хостера: сначала
    hard-reboot через API провайдера (работает даже когда нода зависла), при
    отсутствии cloud-API или его сбое — graceful по SSH через provisioning-ключ.
    Возвращает использованный метод (``api``|``ssh``). NodeSpawnError, если оба
    пути недоступны (нет API и SSH не отвечает)."""
    from .ssh_bootstrap import reboot_via_ssh

    if provider_id and external_id:
        provider = db.get(models.CloudProvider, provider_id)
        if provider:
            driver = get_driver(provider)
            if hasattr(driver, "reboot_server"):
                try:
                    driver.reboot_server(external_id)
                    return "api"
                except DriverError as exc:
                    logger.warning(
                        "reboot: API reboot failed for %s (%s) — SSH fallback",
                        host, exc,
                    )
    if reboot_via_ssh(host, port=ssh_port or 22):
        return "ssh"
    raise NodeSpawnError(
        "reboot failed: no cloud-API reboot available and node SSH unreachable"
    )


def reboot_node(db: Session, node: models.VPNNode) -> str:
    """Перезагрузить relay-ноду (API hard-reboot → SSH-фолбэк)."""
    return _reboot_target(
        db, node.provider_id, node.provider_external_id,
        node.host, getattr(node, "ssh_port", None),
    )


def reboot_exit(db: Session, exit_node: models.WGExitNode) -> str:
    """Перезагрузить exit-ноду (API hard-reboot → SSH-фолбэк)."""
    return _reboot_target(
        db, exit_node.provider_id, exit_node.provider_external_id,
        exit_node.host, getattr(exit_node, "ssh_port", None),
    )


def _warn_lost_hysteria2_users(db: Session, node: models.VPNNode) -> list[str]:
    """Собрать пер-юзерные hysteria2-учётки на ноде, которые авто-resync после
    reinstall НЕ восстановит (#78), и залогировать warning для оператора.

    hysteria2 использует per-user auth (userpass): manage_hy2_user.sh правит
    конфиг на диске ноды, а resync_node_clients покрывает только vless-семейство
    — после стирания диска эти пользователи молча пропадают. ShadowTLS сюда НЕ
    входит: там общий node-wide пароль из VPNConfig.settings, который site.yml
    восстанавливает на каждом прогоне (manage_vpn_user.sh — noop).

    Возвращает отсортированный список ``access_username`` (пусто — таких нет)."""
    hy2 = models.VPNConfigProtocol.hysteria2.value
    usernames: set[str] = set()
    # Активные учётки — через подписку, привязанную к ноде (как в resync).
    assigned = (
        db.query(models.Credential.access_username)
        .join(
            models.Subscription,
            models.Subscription.id == models.Credential.subscription_id,
        )
        .filter(
            models.Subscription.node_id == node.id,
            models.Credential.proto == hy2,
            models.Credential.is_active.is_(True),
        )
        .all()
    )
    # Warm-бандлы — через Credential.node_id, ещё без подписки.
    warm = (
        db.query(models.Credential.access_username)
        .filter(
            models.Credential.node_id == node.id,
            models.Credential.subscription_id.is_(None),
            models.Credential.proto == hy2,
        )
        .all()
    )
    for (uname,) in list(assigned) + list(warm):
        if uname:
            usernames.add(uname)
    if usernames:
        logger.warning(
            "reinstall node %s (%s): %d пер-юзерных hysteria2-учёток НЕ будут "
            "восстановлены авто-resync'ом (#78) — переспровижиньте вручную: %s",
            node.id, node.name, len(usernames), sorted(usernames),
        )
    return sorted(usernames)


def reinstall_node(
    db: Session, node: models.VPNNode, *, image: str | None = None,
    password: str | None = None,
) -> tuple[models.VPNNode, models.ProvisioningTask | None]:
    """Переустановить ОС на ноде через API провайдера, затем заново
    прокатить site.yml. IP сохраняется, поэтому VPNConfig (reality-ключи,
    sub-токены) и так валидны — нода вернётся той же.

    Reinstall стирает диск — пер-юзерных учёток на ноде не остаётся. После
    успешного bootstrap авто-resync возвращает их: vless-семейство
    (reality/xhttp/ws_cdn) через ``resync_node_clients``, а пер-юзерные
    hysteria2-учётки (auth=userpass) — через ``resync_node_hysteria2_clients``,
    который бэкенд запускает в ``_handle_task_outcome`` на reinstall-bootstrap'е
    (#78, флаг RESTORE_HY2_AFTER_REINSTALL, safe-default=вкл). ShadowTLS НЕ
    затронут: там общий node-wide пароль из VPNConfig.settings, который site.yml
    восстанавливает сам. Ниже всё равно логируем warning со списком hysteria2-
    пользователей — если авто-restore частично не сработает (нет пароля/битая
    строка), оператор увидит, кого переспровижинить вручную. Warm-пул ноды
    ИНВАЛИДИРУЕТСЯ целиком (audit #78, хвост): его строки остались бы
    ``pool_state=warm`` в БД при стёртых с диска учётках, и DB-only
    ``try_assign_bundle`` позже выдал бы юзеру бандл с мёртвым hy2-легом.
    Revoked-строки подчистит revoke-sweep, свежие бандлы наминтит refill-тик.

    Провайдер обязан уметь ``reinstall_server`` (capability-проверка через
    hasattr; напр. 4vps умеет, manual — нет)."""
    if not node.provider_id or not node.provider_external_id:
        raise NodeSpawnError("Node has no attached cloud provider; cannot reinstall")
    provider = db.get(models.CloudProvider, node.provider_id)
    if not provider:
        raise NodeSpawnError("CloudProvider record missing")
    driver = get_driver(provider)
    if not hasattr(driver, "reinstall_server"):
        raise NodeSpawnError(
            f"Provider {provider.kind} driver does not support OS reinstall"
        )
    # #78 — предупреждаем оператора о пер-юзерных hysteria2-учётках, которые
    # авто-resync после reinstall НЕ восстановит (shadowtls вернёт site.yml).
    _warn_lost_hysteria2_users(db, node)
    img = image or provider.default_image or "ubuntu-22.04"
    # Reinstall СБРАСЫВАЕТ root-пароль. Генерим его сами (а не отдаём драйверу
    # на самогенерацию) и СОХРАНЯЕМ — иначе после переустановки мы не сможем
    # зайти по паролю и переустановить provisioning-ключ (4vps ключ не
    # инжектит), и bootstrap снова упадёт на Permission denied.
    if password is None:
        password = _gen_root_password()
    try:
        driver.reinstall_server(node.provider_external_id, img, password=password)
    except DriverError as exc:
        raise NodeSpawnError(str(exc)) from exc

    # audit #78 (хвост): диск стёрт → warm-бандлы ноды больше не существуют
    # на боксе, но строки в БД остались pool_state=warm — DB-only
    # try_assign_bundle выдал бы юзеру бандл с мёртвым hy2-легом (vless-леги
    # resync восстановит, hy2 warm — нет). Инвалидируем ПОСЛЕ успешного
    # reinstall_server: упавший API-вызов диск не трогает, пул ещё валиден.
    from .warm_pool import invalidate_node_warm_pool

    invalidate_node_warm_pool(db, node.id, reason="node reinstall — диск стёрт")

    # Свежая ОС → нода ещё не настроена. Сохраняем новый пароль и возвращаем в
    # registering. Bootstrap НЕ пинаем сразу — нода уходит в ребут на несколько
    # минут, и ранний site.yml падает на 'No route to host'. Ждём SSH в фоне
    # (_reinstall_finalize), потом ставим bootstrap (по паролю положит ключ).
    node.provider_root_password_enc = encrypt(password)
    node.status = models.VPNNodeStatus.registering
    node.updated_at = utcnow()
    db.add(node)
    db.commit()
    db.refresh(node)

    threading.Thread(
        target=_reinstall_finalize, args=(node.id, img), daemon=True
    ).start()
    return node, None


def _reinstall_finalize(node_id: int, img: str) -> None:
    """Фоновая достройка после reinstall: дождаться, пока нода переедет в ребут
    и поднимет SSH, затем поставить bootstrap. Своя сессия (daemon-поток)."""
    session = SessionLocal()
    try:
        node = session.get(models.VPNNode, node_id)
        if not node:
            logger.error("reinstall finalize: node %s vanished", node_id)
            return
        if _wait_for_ssh(node.host, node.ssh_port or 22):
            logger.info("reinstall finalize: SSH up on %s", node.host)
        else:
            logger.warning(
                "reinstall finalize: SSH on %s not up within wait window — "
                "enqueuing bootstrap anyway", node.host,
            )
        orchestrator = ProvisioningOrchestrator(session)
        task, created = orchestrator.create_or_coalesce_node_bootstrap(
            node, {"reinstall": True, "image": img}, defer_to_reconciler=False
        )
        session.commit()
        if created:
            orchestrator.run_task_async(task, node=node)
    except Exception:  # noqa: BLE001
        logger.exception("reinstall finalize crashed for node %s", node_id)
        if session.is_active:
            session.rollback()
    finally:
        session.close()


def renew_node(db: Session, node: models.VPNNode) -> None:
    """Продлить аренду cloud-ноды через API провайдера (continueServer у 4vps).
    Списывает с баланса. Авто-продление обычно включается при заказе
    (set_autoprolong), это — ручной/принудительный путь."""
    if not node.provider_id or not node.provider_external_id:
        raise NodeSpawnError("Node has no attached cloud provider; cannot renew")
    provider = db.get(models.CloudProvider, node.provider_id)
    if not provider:
        raise NodeSpawnError("CloudProvider record missing")
    driver = get_driver(provider)
    if not hasattr(driver, "renew_server"):
        raise NodeSpawnError(
            f"Provider {provider.kind} driver does not support renewal"
        )
    try:
        driver.renew_server(node.provider_external_id)
    except DriverError as exc:
        raise NodeSpawnError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Cloud-spawned WG EXIT nodes (зеркало spawn_node_async для зарубежных exit'ов)
# ---------------------------------------------------------------------------


def spawn_exit_async(
    db: Session,
    *,
    provider_id: int,
    name: str,
    region: str,
    plan: str,
    image: str | None = None,
    ssh_key_ids: list[str] | None = None,
    user_data: str | None = None,
    notes: str | None = None,
) -> models.WGExitNode:
    """Неблокирующий заказ облачной EXIT-ноды — зеркало :func:`spawn_node_async`.

    Зарубежный VPS заводится как WG-exit ЗА РУ-relay: клиент его IP не видит,
    обходя DPI-троттлинг прямого зарубежного endpoint'а (см. диагноз в эпике).
    Синхронно — быстрый ``order_server`` + сразу фиксируем ``WGExitNode``
    (is_active=False, placeholder host, external_id, root-пароль, сгенерённый
    WG-keypair); поллинг IP + ``bootstrap_exit`` уходят в фоновый поток
    (:func:`_finalize_exit_spawn`). Драйверы без ``order_server`` — заказ
    целиком в фоне.
    """
    provider = db.get(models.CloudProvider, provider_id)
    if not provider or not provider.is_active:
        raise NodeSpawnError(f"CloudProvider {provider_id} not found or inactive")
    try:
        validate_node_name(name)
    except InvalidNodeIdentity as exc:
        raise NodeSpawnError(str(exc)) from exc
    if db.query(models.WGExitNode).filter(models.WGExitNode.name == name).first():
        raise NodeSpawnError(f"Exit node {name!r} already exists")

    driver = get_driver(provider)
    image = image or provider.default_image or "ubuntu-22.04"
    ssh_key_ids = ssh_key_ids if ssh_key_ids is not None else (provider.ssh_key_ids or [])

    external_id: str | None = None
    root_password: str | None = None
    if hasattr(driver, "order_server"):
        logger.info("Ordering exit %s via %s in %s", name, provider.name, region)
        try:
            external_id, root_password = driver.order_server(
                name=name,
                region=region,
                plan=plan,
                image=image,
                ssh_key_ids=[str(x) for x in ssh_key_ids] or None,
                user_data=user_data,
            )
        except DriverError as exc:
            logger.exception("Failed to order exit via %s", provider.name)
            raise NodeSpawnError(str(exc)) from exc

    public_key, private_key = generate_wireguard_keypair()
    exit_node = models.WGExitNode(
        name=name,
        region=_display_region(driver, region),
        host=SPAWN_PLACEHOLDER_HOST,  # реальный IP проставит _finalize_exit_spawn
        status=models.WGExitNodeStatus.registering,
        is_active=False,  # вне привязки, пока нет реального IP
        provider_id=provider.id,
        provider_external_id=external_id,
        provider_region=region,
        provider_root_password_enc=(
            encrypt(root_password) if root_password else None
        ),
        wg_public_key=public_key,
        wg_private_key_enc=encrypt(private_key),
        notes=notes,
    )
    db.add(exit_node)
    db.commit()
    db.refresh(exit_node)

    threading.Thread(
        target=_finalize_exit_spawn,
        args=(exit_node.id,),
        kwargs={
            "name": name,
            "region": region,
            "plan": plan,
            "image": image,
            "ssh_key_ids": list(ssh_key_ids) if ssh_key_ids else None,
            "user_data": user_data,
        },
        daemon=True,
    ).start()
    return exit_node


def _mark_exit_error(
    session: Session, exit_node: models.WGExitNode, reason: str | None = None
) -> None:
    """Пометить exit error+inactive (заказ не достроился). external_id уже в
    строке → оператор может снести/переустановить, сирот нет.
    ``reason`` дописывается в notes (зеркалит :func:`_mark_spawn_error`)."""
    exit_node.status = models.WGExitNodeStatus.error
    exit_node.is_active = False
    if reason:
        stamp = f"[spawn-error {utcnow().isoformat()}] {reason}"
        exit_node.notes = f"{exit_node.notes}\n{stamp}" if exit_node.notes else stamp
    exit_node.updated_at = utcnow()
    session.add(exit_node)
    session.commit()


def _finalize_exit_spawn(
    exit_id: int,
    *,
    name: str,
    region: str,
    plan: str,
    image: str,
    ssh_key_ids: list[str] | None,
    user_data: str | None,
) -> None:
    """Фоновая достройка облачной exit-ноды: дождаться IP, проставить host +
    is_active, дождаться SSH и запустить ``bootstrap_exit.yml``. Зеркало
    :func:`_finalize_spawn`."""
    session = SessionLocal()
    try:
        exit_node = session.get(models.WGExitNode, exit_id)
        if not exit_node:
            logger.error("exit spawn finalize: exit %s vanished", exit_id)
            return
        provider = session.get(models.CloudProvider, exit_node.provider_id)
        if not provider:
            logger.error("exit spawn finalize: provider for exit %s missing", exit_id)
            _mark_exit_error(session, exit_node)
            return
        driver = get_driver(provider)
        # #74 — закрываем транзакцию ДО долгого ожидания IP (wait_for_ipv4 до
        # 600s): не держим idle-in-transaction соединение из пула. Драйвер уже
        # несёт токен; ноду перечитываем свежей транзакцией после ожидания.
        external_id = exit_node.provider_external_id
        session.rollback()

        root_password: str | None = None
        try:
            if external_id and hasattr(driver, "wait_for_ipv4"):
                ipv4, _cost, _raw = driver.wait_for_ipv4(external_id)
            else:
                server = driver.create_server(
                    name=name,
                    region=region,
                    plan=plan,
                    image=image,
                    ssh_key_ids=[str(x) for x in (ssh_key_ids or [])] or None,
                    user_data=user_data,
                )
                ipv4 = server.ipv4
                external_id = server.external_id
                root_password = server.root_password
        except DriverError:
            logger.exception("exit spawn finalize: driver failed for exit %s", exit_id)
            exit_node = session.get(models.WGExitNode, exit_id)
            if exit_node:
                _mark_exit_error(session, exit_node)
            return

        try:
            validate_node_identity_fields(name, ipv4, 22)
        except InvalidNodeIdentity:
            logger.exception(
                "exit spawn finalize: invalid IPv4 %r for exit %s", ipv4, exit_id
            )
            exit_node = session.get(models.WGExitNode, exit_id)
            if exit_node:
                _mark_exit_error(session, exit_node)
            return

        # Свежая транзакция — перечитываем exit и фиксируем host/external_id.
        exit_node = session.get(models.WGExitNode, exit_id)
        if not exit_node:
            logger.error("exit spawn finalize: exit %s vanished mid-flight", exit_id)
            return
        exit_node.host = ipv4
        exit_node.is_active = True
        exit_node.provider_external_id = external_id
        if root_password:
            exit_node.provider_root_password_enc = encrypt(root_password)
        exit_node.updated_at = utcnow()
        session.add(exit_node)
        session.commit()

        if external_id and hasattr(driver, "set_autoprolong"):
            try:
                driver.set_autoprolong(external_id, True)
                logger.info("autoprolong enabled for exit %s (%s)", exit_node.id, name)
            except Exception:  # noqa: BLE001
                logger.warning("autoprolong enable failed for exit %s (ignored)", exit_id)

        # #74 — завершаем транзакцию ДО ожидания SSH (до 480s): соединение из
        # пула не держим.
        host = exit_node.host
        ssh_port = exit_node.ssh_port or 22
        session.commit()

        if _wait_for_ssh(host, ssh_port):
            logger.info("exit spawn finalize: SSH up on %s", host)
        else:
            logger.warning(
                "exit spawn finalize: SSH on %s not up within wait window — "
                "enqueuing bootstrap anyway", host,
            )

        # Свежая транзакция для постановки bootstrap-таски.
        exit_node = session.get(models.WGExitNode, exit_id)
        if not exit_node:
            logger.error("exit spawn finalize: exit %s vanished before bootstrap", exit_id)
            return
        orchestrator = ProvisioningOrchestrator(session)
        task = orchestrator.create_task("exit", exit_node.id, "bootstrap", {})
        session.commit()
        orchestrator.run_task_async(task)
        logger.info(
            "exit spawn finalize: exit %s got IP %s, bootstrap enqueued", exit_id, ipv4
        )
    except Exception:  # noqa: BLE001
        logger.exception("exit spawn finalize crashed for exit %s", exit_id)
        if session.is_active:
            session.rollback()
        try:
            exit_node = session.get(models.WGExitNode, exit_id)
            if exit_node and exit_node.status == models.WGExitNodeStatus.registering:
                _mark_exit_error(session, exit_node)
        except Exception:  # noqa: BLE001
            session.rollback()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# #70 — подбор спавнов, застрявших из-за смерти daemon-потока финализации
# ---------------------------------------------------------------------------


def _spawn_stuck_threshold_min() -> int:
    """Возраст (мин) registering-ноды с placeholder-host, после которого она
    считается застрявшей. Должен быть больше worst-case финализации:
    wait_for_ipv4 (до 600s у 4vps) + ожидание SSH (NODE_SSH_WAIT_TIMEOUT,
    дефолт 480s) ≈ 18 мин → дефолт 30."""
    return int(os.getenv("NODE_SPAWN_STUCK_MINUTES", "30"))


def sweep_stuck_spawns(
    db: Session,
    *,
    enqueue=None,
) -> dict[str, int]:
    """Подбирает ноды/exit'ы, застрявшие в ``registering`` с placeholder-host.

    Достройка спавна (:func:`_finalize_spawn` / :func:`_finalize_exit_spawn`)
    крутится в daemon-потоке процесса backend — рестарт/деплой контейнера
    убивает поток молча, и оплаченный сервер навсегда остаётся невидимым
    (status=registering, host=0.0.0.0, is_active=False), при этом продолжая
    списывать деньги у хостера. Эту функцию зовёт периодический
    ``run_spawn_sweep_tick`` воркера (finding #70):

      * есть ``provider_external_id`` и драйвер умеет ``wait_for_ipv4`` —
        перезапускаем финализацию (шаги идемпотентны: wait_for_ipv4 — чистое
        чтение состояния заказа, bootstrap ставится через create_or_coalesce);
      * иначе повторный заказ невозможен без риска двойной оплаты — помечаем
        error с пометкой в notes (оператору: проверить панель хостера).

    ``enqueue`` — колбэк ``(kind, entity_id) -> job_id | None``
    (``queue.enqueue_spawn_finalize``). Обязателен при вызове из worker-тика:
    RQ work-horse завершает процесс сразу после return тика и убил бы
    daemon-поток на середине wait_for_ipv4 — достройка должна ехать отдельной
    персистентной RQ-джобой. Без колбэка (вызов из долгоживущего backend'а,
    напр. на startup) финализация стартует потоком, как в spawn_node_async.

    Возвращает счётчики для метрик тика.
    """
    threshold = utcnow() - timedelta(minutes=_spawn_stuck_threshold_min())
    out = {
        "relay_resumed": 0, "relay_errored": 0,
        "exit_resumed": 0, "exit_errored": 0,
        "enqueue_failed": 0,
    }

    def _driver_for(provider_id: int | None):
        provider = (
            db.get(models.CloudProvider, provider_id) if provider_id else None
        )
        if not provider:
            return None, None
        try:
            return provider, get_driver(provider)
        except DriverError:
            logger.exception(
                "sweep_stuck_spawns: driver init failed for provider %s",
                provider_id,
            )
            return provider, None

    # ── Relay-ноды (VPNNode) ────────────────────────────────────────────
    stuck_nodes = (
        db.query(models.VPNNode)
        .filter(
            models.VPNNode.status == models.VPNNodeStatus.registering,
            models.VPNNode.host == SPAWN_PLACEHOLDER_HOST,
            models.VPNNode.updated_at < threshold,
        )
        .all()
    )
    for node in stuck_nodes:
        provider, driver = _driver_for(node.provider_id)
        if (
            node.provider_external_id
            and driver is not None
            and hasattr(driver, "wait_for_ipv4")
        ):
            if enqueue is not None and enqueue("node", node.id) is None:
                # Очередь недоступна — updated_at НЕ двигаем: нода останется
                # «застрявшей», и следующий тик попробует enqueue снова.
                out["enqueue_failed"] += 1
                continue
            # Отодвигаем updated_at ДО запуска достройки — следующий тик не
            # должен запустить вторую финализацию параллельно этой (в
            # queue-режиме от дублей страхует ещё и детерминированный job_id).
            node.updated_at = utcnow()
            db.add(node)
            db.commit()
            logger.warning(
                "sweep_stuck_spawns: resuming stuck spawn for node %s (%s)%s",
                node.id, node.name,
                " via RQ job" if enqueue is not None else "",
            )
            if enqueue is None:
                threading.Thread(
                    target=_finalize_spawn,
                    args=(node.id,),
                    kwargs={
                        # kwargs нужны только create_server-ветке; при наличии
                        # external_id+wait_for_ipv4 она недостижима (нового
                        # заказа/двойной оплаты не будет).
                        "name": node.name,
                        "region": node.provider_region or node.region,
                        "plan": node.provider_plan or "",
                        "image": (
                            (provider.default_image if provider else None)
                            or "ubuntu-22.04"
                        ),
                        "ssh_key_ids": (
                            list(provider.ssh_key_ids or []) if provider else None
                        ),
                        "user_data": None,
                        "reality_sni": None,
                        "reality_dest": None,
                    },
                    daemon=True,
                ).start()
            out["relay_resumed"] += 1
        else:
            logger.error(
                "sweep_stuck_spawns: node %s (%s) застряла в registering без "
                "возобновляемого заказа (external_id=%r) — помечаю error; "
                "проверьте панель хостера вручную",
                node.id, node.name, node.provider_external_id,
            )
            _mark_spawn_error(
                db, node,
                reason="spawn застрял в registering (поток финализации умер); "
                "повторный заказ небезопасен — проверьте панель хостера",
            )
            out["relay_errored"] += 1

    # ── Exit-ноды (WGExitNode) — зеркало relay-ветки ────────────────────
    stuck_exits = (
        db.query(models.WGExitNode)
        .filter(
            models.WGExitNode.status == models.WGExitNodeStatus.registering,
            models.WGExitNode.host == SPAWN_PLACEHOLDER_HOST,
            models.WGExitNode.updated_at < threshold,
        )
        .all()
    )
    for exit_node in stuck_exits:
        provider, driver = _driver_for(exit_node.provider_id)
        if (
            exit_node.provider_external_id
            and driver is not None
            and hasattr(driver, "wait_for_ipv4")
        ):
            if enqueue is not None and enqueue("exit", exit_node.id) is None:
                out["enqueue_failed"] += 1
                continue
            exit_node.updated_at = utcnow()
            db.add(exit_node)
            db.commit()
            logger.warning(
                "sweep_stuck_spawns: resuming stuck spawn for exit %s (%s)%s",
                exit_node.id, exit_node.name,
                " via RQ job" if enqueue is not None else "",
            )
            if enqueue is None:
                threading.Thread(
                    target=_finalize_exit_spawn,
                    args=(exit_node.id,),
                    kwargs={
                        "name": exit_node.name,
                        "region": exit_node.provider_region or exit_node.region,
                        "plan": "",
                        "image": (
                            (provider.default_image if provider else None)
                            or "ubuntu-22.04"
                        ),
                        "ssh_key_ids": (
                            list(provider.ssh_key_ids or []) if provider else None
                        ),
                        "user_data": None,
                    },
                    daemon=True,
                ).start()
            out["exit_resumed"] += 1
        else:
            logger.error(
                "sweep_stuck_spawns: exit %s (%s) застрял в registering без "
                "возобновляемого заказа (external_id=%r) — помечаю error; "
                "проверьте панель хостера вручную",
                exit_node.id, exit_node.name, exit_node.provider_external_id,
            )
            _mark_exit_error(
                db, exit_node,
                reason="spawn застрял в registering (поток финализации умер); "
                "повторный заказ небезопасен — проверьте панель хостера",
            )
            out["exit_errored"] += 1

    return out


def resume_stuck_spawn(kind: str, entity_id: int) -> dict:
    """Синхронная достройка ОДНОГО застрявшего спавна — тело RQ-джобы
    ``app.worker.run_spawn_finalize`` (finding #70).

    В отличие от потока из ``spawn_node_async``, живёт в RQ work-horse
    провижининг-очереди: переживает деплой backend'а, виден в failed-registry
    при краше. Guard идемпотентности: если нода уже финализирована (host
    проставлен / статус не registering) или заказ невозобновляем — no-op;
    повторный запуск джобы безопасен.

    Долгая часть (``wait_for_ipv4`` + bootstrap) выполняется ВНЕ короткой
    guard-сессии — не держим connection из пула на минуты ожидания IP.
    """
    model = models.VPNNode if kind == "node" else models.WGExitNode
    kwargs: dict | None = None
    db = SessionLocal()
    try:
        entity = db.get(model, entity_id)
        if entity is None:
            return {"resumed": False, "reason": "not_found"}
        if (
            entity.status.value != "registering"
            or entity.host != SPAWN_PLACEHOLDER_HOST
        ):
            # Уже финализирована (гонка sweep/дубль джобы) — no-op.
            return {"resumed": False, "reason": "already_finalized"}
        provider = (
            db.get(models.CloudProvider, entity.provider_id)
            if entity.provider_id else None
        )
        if not entity.provider_external_id or provider is None:
            return {"resumed": False, "reason": "not_resumable"}
        try:
            driver = get_driver(provider)
        except DriverError:
            logger.exception(
                "resume_stuck_spawn: driver init failed for %s %s",
                kind, entity_id,
            )
            return {"resumed": False, "reason": "driver_error"}
        if not hasattr(driver, "wait_for_ipv4"):
            return {"resumed": False, "reason": "not_resumable"}
        # kwargs нужны только недостижимой create_server-ветке финализации
        # (см. sweep_stuck_spawns) — собираем их из тех же полей.
        kwargs = {
            "name": entity.name,
            "region": entity.provider_region or entity.region,
            "plan": (getattr(entity, "provider_plan", None) or "")
            if kind == "node" else "",
            "image": provider.default_image or "ubuntu-22.04",
            "ssh_key_ids": list(provider.ssh_key_ids or []) or None,
            "user_data": None,
        }
        if kind == "node":
            kwargs["reality_sni"] = None
            kwargs["reality_dest"] = None
    finally:
        db.close()

    logger.warning(
        "resume_stuck_spawn: finalizing stuck %s %s synchronously",
        kind, entity_id,
    )
    if kind == "node":
        _finalize_spawn(entity_id, **kwargs)
    else:
        _finalize_exit_spawn(entity_id, **kwargs)
    return {"resumed": True, "reason": "finalized"}
