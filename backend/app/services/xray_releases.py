"""Upstream-версия Xray-core: проверка релизов и сводка дрейфа версий.

Три версии, которые нужно держать в голове одновременно:

* **upstream** — последний релиз XTLS/Xray-core на GitHub (кэшируем в
  ``software_releases``);
* **пин** — ``xray_core_version`` в ``roles/xray_core/defaults/main.yml``, то,
  что раскатывается при бутстрапе;
* **фактическая** — что реально стоит на каждой ноде (собирает
  ``tick-node-versions``).

Апгрейд не автоматизируем сознательно: ``xray_core_sha256`` пинится ПАРОЙ к
версии, и подмена версии без пересчёта хэша положила бы установку на всём флоте
— fetch-скрипт отвергает источник при несовпадении sha. Поэтому тик только
сообщает админу, а бамп пина остаётся человеческим решением (коммит + деплой).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..time_utils import utcnow

logger = logging.getLogger(__name__)

RELEASE_NAME = "xray-core"

GITHUB_LATEST_URL = os.getenv(
    "XRAY_RELEASES_URL",
    "https://api.github.com/repos/XTLS/Xray-core/releases/latest",
)
HTTP_TIMEOUT = int(os.getenv("XRAY_RELEASES_TIMEOUT", "15"))

_PIN_RE = re.compile(r"^xray_core_version:\s*[\"']?(?P<version>[^\"'\s]+)", re.MULTILINE)


def _ansible_root() -> Path | None:
    """Корень ansible-дерева ВНУТРИ контейнера воркера.

    Важно: роли применяются из образа (``ANSIBLE_ROOT=/app/infra/ansible``), а не
    из ``/opt/vpn/infra`` на диске хоста. Пин читаем оттуда же, откуда он реально
    раскатывается, иначе при пропущенном ребилде показали бы версию, которой на
    самом деле никто не ставит.

    Без env перебираем кандидатов: в репозитории пакет лежит как
    ``backend/app/services``, в образе — как ``/app/app/services``, и глубина до
    корня разная. Возвращаем None, если дерева нет вообще (API-контейнер).
    """
    root = os.getenv("ANSIBLE_ROOT")
    if root:
        return Path(root)
    here = Path(__file__).resolve()
    for depth in (3, 2):
        candidate = here.parents[depth] / "infra" / "ansible"
        if candidate.is_dir():
            return candidate
    return None


def pinned_version() -> str | None:
    """Версия xray, зашитая в роль (``xray_core_version``)."""
    root = _ansible_root()
    if root is None:
        logger.warning("xray-releases: ansible-дерево не найдено — пин неизвестен")
        return None
    defaults = root / "roles" / "xray_core" / "defaults" / "main.yml"
    try:
        text = defaults.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("xray-releases: не прочитать пин из %s: %s", defaults, exc)
        return None
    match = _PIN_RE.search(text)
    return match.group("version") if match else None


def _normalize(version: str | None) -> str | None:
    """``v26.3.27`` и ``26.3.27`` — одна и та же версия.

    Роль пинит с ``v``, ``xray version`` печатает без — без нормализации любое
    сравнение давало бы вечный «дрейф».
    """
    if not version:
        return None
    value = version.strip()
    return value[1:] if value.startswith("v") else value


def fetch_latest_release() -> dict[str, Any]:
    """Сходить на GitHub за последним релизом.

    Возвращает ``{"version": ..., "published_at": ..., "html_url": ...}`` либо
    ``{"error": ...}``. Наружу не бросаем: недоступный GitHub — рядовая
    ситуация, тик из-за неё падать не должен.
    """
    request = urllib.request.Request(
        GITHUB_LATEST_URL,
        headers={
            "Accept": "application/vnd.github+json",
            # GitHub отвечает 403 на запросы без User-Agent.
            "User-Agent": "vpn-backend-release-check",
        },
    )
    token = (os.getenv("GITHUB_TOKEN") or "").strip()
    if token:
        # Не обязателен: анонимный лимит 60 запросов/час на IP с запасом хватает
        # для проверки раз в 6 часов. Нужен, только если IP делят с другими.
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    tag = (payload.get("tag_name") or "").strip()
    if not tag:
        return {"error": "в ответе GitHub нет tag_name"}

    published_raw = payload.get("published_at")
    published_at = None
    if published_raw:
        try:
            published_at = datetime.fromisoformat(
                published_raw.replace("Z", "+00:00")
            ).astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            published_at = None

    return {
        "version": tag,
        "published_at": published_at,
        "html_url": payload.get("html_url"),
    }


def _release_row(session):
    from .. import models

    row = (
        session.query(models.SoftwareRelease)
        .filter(models.SoftwareRelease.name == RELEASE_NAME)
        .one_or_none()
    )
    if row is None:
        row = models.SoftwareRelease(name=RELEASE_NAME)
        session.add(row)
    return row


def version_overview(session) -> dict[str, Any]:
    """Сводка «upstream vs пин vs ноды» для админки и уведомления."""
    from .. import models
    from ..version import app_version

    row = _release_row(session)
    pin = pinned_version()
    latest = row.latest_version

    nodes = (
        session.query(models.VPNNode)
        .filter(models.VPNNode.is_active.is_(True))
        .order_by(models.VPNNode.name)
        .all()
    )
    our_version = app_version()
    outdated_xray = [
        n for n in nodes
        if n.xray_version and _normalize(n.xray_version) != _normalize(pin)
    ]
    outdated_release = [
        n for n in nodes
        if n.release_version and n.release_version != our_version
    ]
    unknown = [n for n in nodes if not n.xray_version]

    return {
        "app_version": our_version,
        "xray": {
            "latest": latest,
            "pinned": pin,
            "pin_behind_upstream": bool(
                latest and pin and _normalize(latest) != _normalize(pin)
            ),
            "checked_at": row.checked_at.isoformat() if row.checked_at else None,
            "html_url": row.html_url,
            "last_error": row.last_error,
        },
        "nodes_total": len(nodes),
        "nodes_outdated_xray": [n.name for n in outdated_xray],
        "nodes_outdated_release": [n.name for n in outdated_release],
        "nodes_version_unknown": [n.name for n in unknown],
    }


def check_upstream_and_notify(session) -> dict[str, Any]:
    """Обновить кэш релиза и разбудить админа, если версии разъехались."""
    from .admin_notify import notify_admins

    row = _release_row(session)
    result = fetch_latest_release()

    if "error" in result:
        row.last_error = result["error"][:500]
        session.commit()
        logger.warning("xray-upstream: %s", result["error"])
        return {"latest": row.latest_version, "notified": False, "error": result["error"]}

    row.latest_version = result["version"]
    row.published_at = result["published_at"]
    row.html_url = result["html_url"]
    row.checked_at = utcnow()
    row.last_error = None
    session.commit()

    overview = version_overview(session)
    pin = overview["xray"]["pinned"]
    latest = overview["xray"]["latest"]
    outdated_nodes = overview["nodes_outdated_xray"]

    lines: list[str] = []
    if overview["xray"]["pin_behind_upstream"]:
        lines.append(
            f"🆕 Xray-core: вышла {latest}, у нас в роли пин {pin}.\n"
            "Бамп — руками: xray_core_version + xray_core_sha256 в паре "
            "(иначе установка упадёт на несовпадении хэша), затем деплой."
        )
    if outdated_nodes:
        shown = ", ".join(outdated_nodes[:10])
        tail = f" и ещё {len(outdated_nodes) - 10}" if len(outdated_nodes) > 10 else ""
        lines.append(
            f"📦 Ноды не на пине {pin}: {shown}{tail}. "
            "Обновить можно точечно кнопкой «Обновить xray» в админке."
        )

    if not lines:
        return {"latest": latest, "notified": False, "outdated": 0}

    notify_admins(
        session,
        kind="xray_version_drift",
        text="\n\n".join(lines),
        # Ключ дедупа — пара (upstream, пин): пока не вышел новый релиз и не
        # сменился пин, повторных пушей нет. Список отставших нод в ключ НЕ
        # входит: он меняется по мере апгрейда, и каждая обновлённая нода
        # порождала бы новый пуш про оставшиеся.
        dedup_key={"latest": latest, "pinned": pin},
        extra={"outdated_nodes": outdated_nodes},
        window_sec=int(os.getenv("XRAY_DRIFT_DEDUP_WINDOW_SEC", str(7 * 86400))),
        autocommit=True,
    )
    return {"latest": latest, "notified": True, "outdated": len(outdated_nodes)}
