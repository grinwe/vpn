"""Сбор версий софта с нод по SSH.

Зачем: до 2026-07-26 фактическая версия xray на ноде нигде не сохранялась — её
знал только ``xray-core-fetch.sh`` на самой ноде, а «где уже новое ядро, где
старое» приходилось выяснять руками. Тик ``tick-node-versions`` раз в час
снимает с каждой активной ноды две вещи:

* ``xray version`` — что реально стоит;
* ``/etc/vpn-node-release.json`` — какой версией НАШЕГО кода нода прошита
  (маркер пишет ``site.yml`` в post_tasks, только после успеха всех ролей).

Почему SSH, а не ansible: ansible-прогон идёт через семафор провижининга
(``MAX_CONCURRENT_ANSIBLE=3``), и периодический фанаут по 10 нодам выедал бы
слоты у горячего пути выдачи конфигов. Тот же приём уже используется сбором
трафика (``traffic_stats.collect_all_active_nodes``), у него и переиспользуем
загрузку ключа и SSH-хелпер: одна нода = одно короткое подключение, две команды.

Сбор — best-effort телеметрия: недоступная нода не роняет тик и не трогает
уже записанные версии (иначе временная сетевая проблема выглядела бы как
«версия пропала»), только ``versions_checked_at`` остаётся старым.
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from ..time_utils import utcnow
from .traffic_stats import (
    SSH_AUTH_TIMEOUT,
    SSH_BANNER_TIMEOUT,
    SSH_CONNECT_TIMEOUT,
    SSH_PORT_DEFAULT,
    SSH_USER,
    _load_provisioning_pkey,
    _resolve_provisioning_key_path,
    _ssh_run,
)

logger = logging.getLogger(__name__)

# `xray version` печатает: "Xray 26.3.27 (Xray, Penetrates Everything.) ..."
_XRAY_VERSION_RE = re.compile(r"^Xray\s+(\S+)", re.MULTILINE)
# `hysteria version` печатает многострочный блок с "Version\tv2.10.0".
_HYSTERIA_VERSION_RE = re.compile(r"^Version\s+(\S+)", re.MULTILINE)

XRAY_BIN = "/usr/local/bin/xray"
HYSTERIA_BIN = "/usr/local/bin/hysteria"
RELEASE_MARKER = "/etc/vpn-node-release.json"

# Разделитель секций вывода. Все команды идут одним exec: лишние round-trip'ы на
# ноду ради одной строки не нужны, а `|| true` не даёт отсутствующему бинарю
# (нода без hy2 или без vless) уронить весь вывод.
_SECTION = "---8<---"
_PROBE_COMMAND = (
    f"{XRAY_BIN} version 2>/dev/null | head -n1 || true; "
    f"echo '{_SECTION}'; "
    f"cat {RELEASE_MARKER} 2>/dev/null || true; "
    f"echo '{_SECTION}'; "
    f"{HYSTERIA_BIN} version 2>/dev/null | grep -E '^Version' || true"
)


@dataclass
class NodeVersions:
    """Что удалось снять с одной ноды. None = «не удалось прочитать»."""

    node_id: int
    xray_version: str | None = None
    release_version: str | None = None
    hysteria_version: str | None = None
    error: str | None = None


@dataclass
class _NodeRef:
    """Снимок полей ноды для потоков — ORM-объекты между потоками не носим."""

    id: int
    name: str
    host: str
    ssh_port: int | None


def _parse_probe_output(raw: str) -> tuple[str | None, str | None, str | None]:
    """Разобрать вывод ``_PROBE_COMMAND``.

    Возвращает ``(версия xray, версия нашего кода, версия hysteria)``. Каждая
    секция независима: отсутствие одного бинаря (нода без hy2, нода без vless)
    не должно стоить нам остальных версий.
    """
    sections = raw.split(_SECTION)
    match = _XRAY_VERSION_RE.search(sections[0] if sections else "")
    xray_version = match.group(1) if match else None

    hy2_match = _HYSTERIA_VERSION_RE.search(sections[2] if len(sections) > 2 else "")
    hysteria_version = hy2_match.group(1) if hy2_match else None

    release_version = None
    marker = (sections[1] if len(sections) > 1 else "").strip()
    if marker:
        try:
            payload = json.loads(marker)
        except ValueError:
            # Маркер битый (недописан, руками правили) — это не повод терять
            # версии бинарей, поэтому просто не заполняем поле.
            logger.warning("node-versions: битый %s: %r", RELEASE_MARKER, marker[:120])
        else:
            if isinstance(payload, dict):
                value = payload.get("version")
                release_version = str(value) if value else None
    return xray_version, release_version, hysteria_version


def collect_node_versions(node) -> NodeVersions:
    """SSH в ноду и снять версии. Исключения не пробрасываем — только error."""
    try:
        import paramiko  # noqa: WPS433 — держим вне API-контейнера
    except ImportError as exc:  # pragma: no cover — paramiko в requirements
        return NodeVersions(node_id=node.id, error=f"paramiko missing: {exc}")

    key_path = _resolve_provisioning_key_path()
    if not os.path.exists(key_path):
        return NodeVersions(node_id=node.id, error=f"ssh key not found at {key_path}")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=node.host,
            port=node.ssh_port or SSH_PORT_DEFAULT,
            username=SSH_USER,
            pkey=_load_provisioning_pkey(key_path),
            timeout=SSH_CONNECT_TIMEOUT,
            banner_timeout=SSH_BANNER_TIMEOUT,
            auth_timeout=SSH_AUTH_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        _rc, out, _err = _ssh_run(client, _PROBE_COMMAND)
    except Exception as exc:  # noqa: BLE001 — недоступная нода не роняет тик
        return NodeVersions(node_id=node.id, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    xray_version, release_version, hysteria_version = _parse_probe_output(out)
    return NodeVersions(
        node_id=node.id,
        xray_version=xray_version,
        release_version=release_version,
        hysteria_version=hysteria_version,
    )


def collect_all_nodes_versions(session, *, workers: int | None = None) -> list[NodeVersions]:
    """Снять версии со всех активных нод и записать в БД.

    Параллельно (пул потоков), потому что SSH блокирующий и ноды независимы.
    Запись в сессию — только из главного потока: ORM не потокобезопасен.
    """
    from .. import models

    nodes = (
        session.query(models.VPNNode)
        .filter(
            models.VPNNode.is_active.is_(True),
            models.VPNNode.status.in_(
                [models.VPNNodeStatus.active, models.VPNNodeStatus.draining]
            ),
        )
        .all()
    )
    if not nodes:
        return []

    key_path = _resolve_provisioning_key_path()
    if not os.path.exists(key_path):
        logger.error(
            "node-versions: provisioning ssh key not found at %s — сбор пропущен",
            key_path,
        )
        return []

    refs = [
        _NodeRef(id=n.id, name=n.name, host=n.host, ssh_port=n.ssh_port) for n in nodes
    ]
    by_id = {n.id: n for n in nodes}
    pool_size = workers or int(os.getenv("NODE_VERSIONS_SSH_WORKERS", "8"))

    results: list[NodeVersions] = []
    with ThreadPoolExecutor(max_workers=max(1, pool_size)) as pool:
        futures = {pool.submit(collect_node_versions, ref): ref for ref in refs}
        for future in as_completed(futures):
            ref = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = NodeVersions(node_id=ref.id, error=f"{type(exc).__name__}: {exc}")
            results.append(result)

            node = by_id.get(result.node_id)
            if node is None:
                continue
            if result.error:
                logger.warning(
                    "node-versions: %s (%s) — %s", ref.name, ref.host, result.error
                )
                continue
            # Пустое значение не затираем: временно недоступный бинарь не должен
            # выглядеть как «версии больше нет».
            if result.xray_version:
                node.xray_version = result.xray_version
            if result.release_version:
                node.release_version = result.release_version
            if result.hysteria_version:
                node.hysteria_version = result.hysteria_version
            node.versions_checked_at = utcnow()

    session.commit()
    return results
