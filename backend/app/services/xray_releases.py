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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..time_utils import utcnow

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = int(os.getenv("XRAY_RELEASES_TIMEOUT", "15"))


@dataclass(frozen=True)
class ProductSpec:
    """Один отслеживаемый продукт: где upstream, где наш пин, как звать в UI.

    Два продукта вместо одного появились не сразу: три VLESS-протокола держит
    один бинарь xray, а hysteria2 — отдельный демон со своим релиз-циклом, и до
    2026-07-26 его версия не пинилась и не собиралась вообще.
    """

    key: str            # ключ в software_releases.name и в JSON-ответах API
    label: str          # как показывать человеку
    api_url: str        # GitHub releases/latest
    pin_file: str       # путь внутри ansible-дерева до defaults роли
    pin_var: str        # имя переменной пина в этом файле
    # Префикс тега, который upstream добавляет, а мы в пине не храним: у
    # apernet/hysteria релизы называются `app/vX.Y.Z` (монорепа app+core), и без
    # срезания префикса сравнение с пином давало бы вечный ложный дрейф.
    tag_prefix: str = ""
    # Как обновлять: подсказка в тексте пуша админу.
    upgrade_hint: str = ""


PRODUCTS: tuple[ProductSpec, ...] = (
    ProductSpec(
        key="xray-core",
        label="Xray-core",
        api_url=os.getenv(
            "XRAY_RELEASES_URL",
            "https://api.github.com/repos/XTLS/Xray-core/releases/latest",
        ),
        pin_file="roles/xray_core/defaults/main.yml",
        pin_var="xray_core_version",
        upgrade_hint=(
            "Бамп — руками: xray_core_version + xray_core_sha256 в паре "
            "(иначе установка упадёт на несовпадении хэша), затем деплой."
        ),
    ),
    ProductSpec(
        key="hysteria",
        label="Hysteria2",
        api_url=os.getenv(
            "HYSTERIA_RELEASES_URL",
            "https://api.github.com/repos/apernet/hysteria/releases/latest",
        ),
        pin_file="roles/install_hysteria2/defaults/main.yml",
        pin_var="hysteria2_version",
        tag_prefix="app/",
        upgrade_hint=(
            "Бамп — руками: hysteria2_version + hysteria2_sha256 в паре (хэш "
            "берётся из hashes.txt релиза), плюс HYSTERIA_VERSIONS в "
            "refresh-assets.sh для зеркала, затем деплой."
        ),
    ),
)

PRODUCTS_BY_KEY = {p.key: p for p in PRODUCTS}

# Историческое имя: модуль начинался как «только xray». Оставлено, потому что на
# него ссылаются тесты и вызовы API/воркера.
RELEASE_NAME = "xray-core"


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


def pinned_version(product: ProductSpec | None = None) -> str | None:
    """Версия, зашитая в роль продукта (``<product>_version`` в её defaults)."""
    spec = product or PRODUCTS_BY_KEY[RELEASE_NAME]
    root = _ansible_root()
    if root is None:
        logger.warning("releases: ansible-дерево не найдено — пин неизвестен")
        return None
    defaults = root / spec.pin_file
    try:
        text = defaults.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("releases: не прочитать пин из %s: %s", defaults, exc)
        return None
    pattern = re.compile(
        rf"^{re.escape(spec.pin_var)}:\s*[\"']?(?P<version>[^\"'\s]+)", re.MULTILINE
    )
    match = pattern.search(text)
    return match.group("version") if match else None


def _normalize(version: str | None, *, tag_prefix: str = "") -> str | None:
    """Привести версию к сравнимому виду.

    Роль пинит с ``v``, ``xray version`` печатает без; у hysteria upstream-тег
    ещё и с префиксом ``app/``. Без нормализации любое сравнение давало бы
    вечный ложный «дрейф».
    """
    if not version:
        return None
    value = version.strip()
    if tag_prefix and value.startswith(tag_prefix):
        value = value[len(tag_prefix):]
    return value[1:] if value.startswith("v") else value


def fetch_latest_release(product: ProductSpec | None = None) -> dict[str, Any]:
    """Сходить на GitHub за последним релизом продукта.

    Возвращает ``{"version": ..., "published_at": ..., "html_url": ...}`` либо
    ``{"error": ...}``. Наружу не бросаем: недоступный GitHub — рядовая
    ситуация, тик из-за неё падать не должен.
    """
    spec = product or PRODUCTS_BY_KEY[RELEASE_NAME]
    request = urllib.request.Request(
        spec.api_url,
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


def _release_row(session, product: ProductSpec | None = None):
    from .. import models

    spec = product or PRODUCTS_BY_KEY[RELEASE_NAME]
    row = (
        session.query(models.SoftwareRelease)
        .filter(models.SoftwareRelease.name == spec.key)
        .one_or_none()
    )
    if row is None:
        row = models.SoftwareRelease(name=spec.key)
        session.add(row)
        # Флашим СРАЗУ: SessionLocal создан с autoflush=False, поэтому pending
        # INSERT не виден последующим SELECT'ам, и в одном прогоне тика строка
        # успевала создаться дважды (version_overview трогает оба продукта, а
        # следом их же обходит цикл проверки) → на commit прилетал
        # UniqueViolation по uq_software_releases_name, и вся проверка
        # откатывалась: hysteria так и не появлялась в БД.
        session.flush()
    return row


def _product_state(session, spec: ProductSpec) -> dict[str, Any]:
    """Кэш релиза + пин по одному продукту, в форме для API."""
    row = _release_row(session, spec)
    # Файл роли виден только воркеру (ansible-дерево лежит в его образе), а этот
    # код зовёт ещё и API-контейнер. Поэтому: сначала файл, иначе — то, что
    # воркер записал в БД на последней проверке. Иначе сводка врала бы
    # `pinned: null` и «дрейф» не считался вовсе.
    pin = pinned_version(spec) or row.pinned_version
    latest = row.latest_version
    return {
        "label": spec.label,
        "latest": latest,
        "pinned": pin,
        "pin_behind_upstream": bool(
            latest
            and pin
            and _normalize(latest, tag_prefix=spec.tag_prefix)
            != _normalize(pin, tag_prefix=spec.tag_prefix)
        ),
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "html_url": row.html_url,
        "last_error": row.last_error,
    }


def version_overview(session) -> dict[str, Any]:
    """Сводка «upstream vs пин vs ноды» для админки и уведомлений.

    Ключ ``xray`` сохранён отдельно от ``products`` намеренно: на него уже
    смотрит фронт, и ломать его форму ради симметрии нет смысла.
    """
    from .. import models
    from ..version import app_version

    xray_spec = PRODUCTS_BY_KEY["xray-core"]
    hy2_spec = PRODUCTS_BY_KEY["hysteria"]
    xray_state = _product_state(session, xray_spec)
    hy2_state = _product_state(session, hy2_spec)

    nodes = (
        session.query(models.VPNNode)
        .filter(models.VPNNode.is_active.is_(True))
        .order_by(models.VPNNode.name)
        .all()
    )
    our_version = app_version()

    def _drift(attr: str, pin: str | None, spec: ProductSpec) -> list[str]:
        # Без известного пина сравнивать не с чем: раньше при пустом пине
        # ВСЕ ноды попадали в «отстают» (версия != None), и сводка звала
        # обновлять флот, который на самом деле в порядке.
        if not pin:
            return []
        return [
            n.name
            for n in nodes
            if getattr(n, attr)
            and _normalize(getattr(n, attr), tag_prefix=spec.tag_prefix)
            != _normalize(pin, tag_prefix=spec.tag_prefix)
        ]

    return {
        "app_version": our_version,
        "xray": xray_state,
        "hysteria": hy2_state,
        "nodes_total": len(nodes),
        "nodes_outdated_xray": _drift("xray_version", xray_state["pinned"], xray_spec),
        "nodes_outdated_hysteria": _drift(
            "hysteria_version", hy2_state["pinned"], hy2_spec
        ),
        "nodes_outdated_release": [
            n.name for n in nodes if n.release_version and n.release_version != our_version
        ],
        "nodes_version_unknown": [n.name for n in nodes if not n.xray_version],
        # Отдельный список: нода без hy2-конфига бинаря и не имеет — это не
        # «не смогли опросить», а норма, поэтому в один список с xray не мешаем.
        "nodes_hysteria_unknown": [n.name for n in nodes if not n.hysteria_version],
    }


def check_product_and_notify(session, spec: ProductSpec) -> dict[str, Any]:
    """Обновить кэш релиза одного продукта и разбудить админа при дрейфе."""
    from .admin_notify import notify_admins

    row = _release_row(session, spec)
    result = fetch_latest_release(spec)

    if "error" in result:
        row.last_error = result["error"][:500]
        # Пин читается локально и от доступности GitHub не зависит — обновляем
        # даже на неудачной проверке, иначе после бампа роли сводка показывала бы
        # старый пин до первого успешного похода наружу.
        row.pinned_version = pinned_version(spec) or row.pinned_version
        session.commit()
        logger.warning("releases[%s]: %s", spec.key, result["error"])
        return {
            "product": spec.key,
            "latest": row.latest_version,
            "notified": False,
            "error": result["error"],
        }

    row.latest_version = result["version"]
    # Тик всегда идёт в воркере, где ansible-дерево есть — фиксируем пин для
    # API-контейнера.
    row.pinned_version = pinned_version(spec) or row.pinned_version
    row.published_at = result["published_at"]
    row.html_url = result["html_url"]
    row.checked_at = utcnow()
    row.last_error = None
    session.commit()

    overview = version_overview(session)
    state = overview["xray"] if spec.key == "xray-core" else overview["hysteria"]
    pin = state["pinned"]
    latest = state["latest"]
    outdated_nodes = (
        overview["nodes_outdated_xray"]
        if spec.key == "xray-core"
        else overview["nodes_outdated_hysteria"]
    )

    lines: list[str] = []
    if state["pin_behind_upstream"]:
        lines.append(
            f"🆕 {spec.label}: вышла {latest}, у нас в роли пин {pin}.\n{spec.upgrade_hint}"
        )
    if outdated_nodes:
        shown = ", ".join(outdated_nodes[:10])
        tail = f" и ещё {len(outdated_nodes) - 10}" if len(outdated_nodes) > 10 else ""
        lines.append(
            f"📦 {spec.label}: ноды не на пине {pin}: {shown}{tail}. "
            "Обновить можно точечно кнопкой в админке (страница «Версии»)."
        )

    if not lines:
        return {
            "product": spec.key,
            "latest": latest,
            "notified": False,
            "outdated": 0,
        }

    notify_admins(
        session,
        kind="xray_version_drift",
        text="\n\n".join(lines),
        # Ключ дедупа — тройка (продукт, upstream, пин): пока не вышел новый
        # релиз и не сменился пин, повторных пушей нет. Список отставших нод в
        # ключ НЕ входит: он меняется по мере апгрейда, и каждая обновлённая
        # нода порождала бы новый пуш про оставшиеся.
        dedup_key={"product": spec.key, "latest": latest, "pinned": pin},
        extra={"outdated_nodes": outdated_nodes},
        window_sec=int(os.getenv("XRAY_DRIFT_DEDUP_WINDOW_SEC", str(7 * 86400))),
        autocommit=True,
    )
    return {
        "product": spec.key,
        "latest": latest,
        "notified": True,
        "outdated": len(outdated_nodes),
    }


def check_upstream_and_notify(session) -> dict[str, Any]:
    """Прогнать проверку по ВСЕМ отслеживаемым продуктам.

    Один тик на оба: поход к GitHub занимает доли секунды, а два расписания
    ради этого пришлось бы держать в синхроне.
    """
    results = [check_product_and_notify(session, spec) for spec in PRODUCTS]
    xray = next((r for r in results if r["product"] == "xray-core"), {})
    return {
        # Плоские ключи по xray — на них смотрят существующие тесты и логи тика.
        "latest": xray.get("latest"),
        "notified": any(r.get("notified") for r in results),
        "products": results,
    }
