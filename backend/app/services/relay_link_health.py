"""WireGuard tunnel health collector для relay_exit_links.

Почему это не в traffic_stats.py
---------------------------------
traffic_stats читает xray gRPC stats с каждой active VPNNode —
её source of truth это per-user email counters из xray StatsService.
relay-туннели это другой слой (WG между relay и exit), у которого
xray ничего про handshake не знает: это ядерный счётчик WG peer'а.

Поэтому отдельный тик и отдельный коллектор. SSH-транспорт +
ключ из ``ANSIBLE_PRIVATE_KEY_FILE`` переиспользуются через
``traffic_stats._ssh_run`` + тот же lazy-import paramiko pattern.

Что читаем
----------
На каждом relay запускаем ``wg show all dump`` — одна команда
отдаёт все интерфейсы (wg0, wg1, ...) с peer-строками. Формат
(tab-separated, по одной строке на peer, плюс interface-строка
сверху каждого интерфейса):

    IFACE  PRIV  PUB  LISTEN_PORT  FWMARK                            # interface
    IFACE  PEER_PUB  PRESHARED  ENDPOINT  ALLOWED_IPS  HANDSHAKE  RX  TX  KEEPALIVE  # peer

``HANDSHAKE`` — unix-epoch последнего handshake'а (``0`` если
ни разу не было). RX/TX — байты. Мы ищем peer-строку по
``(iface == link.wg_interface_name, peer_pub == exit.wg_public_key)``
и пишем в БД.

Ошибки per-relay не валят весь тик: SSH-фейл → просто пропускаем
этот relay, ``last_observed_at`` у его links не обновляется, UI
показывает stale-цвет (красный после >15min).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Reuse constants/policy из traffic_stats, чтобы не дублировать.
from .traffic_stats import (
    SSH_PORT_DEFAULT,
    SSH_USER,
    SSH_CONNECT_TIMEOUT,
    _ssh_run,
)

WG_DUMP_CMD = "wg show all dump"


def _parse_wg_dump(raw: str) -> dict[tuple[str, str], dict[str, int]]:
    """Parse ``wg show all dump`` → {(iface, peer_pub): {handshake, rx, tx}}.

    Строки interface (5 tab-separated полей) пропускаются — нам нужны
    только peer-строки (9 полей). Невалидные строки (empty, короче
    ожидаемого) игнорируются тихо, потому что wg иногда выдаёт пустую
    строку между интерфейсами.
    """
    out: dict[tuple[str, str], dict[str, int]] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        # Interface-строка = 5 полей, peer-строка = 9. Любая другая
        # длина — что-то сломалось в wg, лучше пропустить чем крашить.
        if len(parts) != 9:
            continue
        iface = parts[0]
        peer_pub = parts[1]
        try:
            handshake = int(parts[5])
            rx = int(parts[6])
            tx = int(parts[7])
        except (ValueError, IndexError):
            continue
        out[(iface, peer_pub)] = {
            "handshake": handshake,
            "rx": rx,
            "tx": tx,
        }
    return out


def _collect_relay_wg_state(relay) -> dict[tuple[str, str], dict[str, int]]:
    """SSH into ``relay`` и верни dump-map. На любой транспортной ошибке
    подымает исключение, чтобы caller залоггировал и пропустил relay.
    """
    try:
        import paramiko  # noqa: WPS433 — lazy import, как в traffic_stats
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("paramiko not installed in the worker container") from exc

    key_path = (
        os.getenv("ANSIBLE_PRIVATE_KEY_FILE")
        or os.getenv("PROVISIONING_SSH_KEY")
        or "/run/secrets/provisioning_key"
    )
    if not os.path.exists(key_path):
        raise RuntimeError(f"provisioning ssh key not found at {key_path}")

    pkey = paramiko.Ed25519Key.from_private_key_file(key_path)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=relay.host,
            port=relay.ssh_port or SSH_PORT_DEFAULT,
            username=SSH_USER,
            pkey=pkey,
            timeout=SSH_CONNECT_TIMEOUT,
            banner_timeout=SSH_CONNECT_TIMEOUT,
            auth_timeout=SSH_CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        exit_status, stdout, stderr = _ssh_run(client, WG_DUMP_CMD)
        if exit_status != 0:
            # wg не установлен / нет интерфейсов / rc!=0 — пусто вместо
            # raise, чтобы не сыпать Exception на свежих relay, где
            # ansible ещё не накатил wg-quick.
            logger.info(
                "relay_link_health: wg show on relay %s rc=%d stderr=%s",
                relay.id, exit_status, stderr.strip()[:200],
            )
            return {}
        return _parse_wg_dump(stdout)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def collect_all_relay_links(session) -> dict[str, Any]:
    """Walk каждый relay с links, SSH, обнови health-колонки у links.

    Возвращает summary для worker-тика:
      - relays_total: сколько relay обошли
      - relays_ssh_failed: SSH упал или wg ошибку вернул для N relay
      - links_updated: у скольких links мы заполнили last_observed_at
      - links_no_match: у скольких links peer не нашёлся в dump
        (интерфейс down / wg-quick не запущен / ключи расходятся)
    """
    from .. import models
    from ..time_utils import utcnow

    links = (
        session.query(models.RelayExitLink)
        .join(models.VPNNode, models.VPNNode.id == models.RelayExitLink.relay_node_id)
        .all()
    )
    # Группируем по relay_node_id, чтобы один SSH per relay.
    links_by_relay: dict[int, list[models.RelayExitLink]] = {}
    for link in links:
        links_by_relay.setdefault(link.relay_node_id, []).append(link)

    stats = {
        "relays_total": len(links_by_relay),
        "relays_ssh_failed": 0,
        "links_updated": 0,
        "links_no_match": 0,
    }
    now = utcnow()

    for relay_id, relay_links in links_by_relay.items():
        relay = session.get(models.VPNNode, relay_id)
        if relay is None:
            continue
        try:
            dump = _collect_relay_wg_state(relay)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "relay_link_health: ssh failed for relay %s (%s): %s",
                relay.id, relay.name, exc,
            )
            stats["relays_ssh_failed"] += 1
            continue
        logger.info(
            "relay_link_health: relay=%s links=%d dump_peers=%d "
            "dump_sample=%s",
            relay.name, len(relay_links), len(dump),
            # Первые 2 ключа (iface, peer_pub_prefix) для отладки
            # «почему мой линк не матчится» — полный ключ длинный.
            [(i, p[:12] + "…" if p else p) for (i, p) in list(dump)[:2]],
        )

        for link in relay_links:
            exit_pub = link.exit_node.wg_public_key if link.exit_node else None
            if not exit_pub:
                # Exit без pubkey — обычно significant: exit удалён
                # каскадом ORM FK, но link остался, либо keygen не
                # прогоняли. last_observed_at всё равно выставляем,
                # чтобы UI отличал «SSH up, но exit сломан» от «тик
                # никогда не проходил». links_no_match подсветит
                # количество таких в логах.
                link.last_observed_at = now
                stats["links_no_match"] += 1
                logger.warning(
                    "relay_link_health: link=%s exit=%s has no wg_public_key",
                    link.wg_interface_name,
                    link.exit_node.name if link.exit_node else "<none>",
                )
                continue
            row = dump.get((link.wg_interface_name, exit_pub))
            if row is None:
                # peer не нашёлся — интерфейс down или ключи
                # разошлись. Пишем last_observed_at=now но не трогаем
                # last_handshake_at, чтобы UI мог показать «SSH ok, но
                # peer отсутствует» через сравнение observed_at и
                # handshake_at (последний останется старым/NULL).
                link.last_observed_at = now
                stats["links_no_match"] += 1
                logger.info(
                    "relay_link_health: no peer match on %s: "
                    "iface=%s exit_pub=%s…",
                    relay.name, link.wg_interface_name, exit_pub[:12],
                )
                continue
            handshake = row["handshake"]
            link.last_handshake_at = (
                datetime.utcfromtimestamp(handshake) if handshake > 0 else None
            )
            link.last_rx_bytes = row["rx"]
            link.last_tx_bytes = row["tx"]
            link.last_observed_at = now
            stats["links_updated"] += 1

    session.commit()
    logger.info("relay_link_health: summary=%s", stats)
    return stats
