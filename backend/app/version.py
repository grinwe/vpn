"""Версия выкаченного кода.

До 2026-07-26 на проде не было НИКАКОГО следа ревизии: ``.git`` вырезается при
rsync (roles/deploy_app_stack), образы собираются на самом хосте без тегов, и на
вопрос «что сейчас крутится» ответить было нечем. Отсюда единственный источник
правды — файл ``VERSION`` в корне репозитория: одна семверная строка на весь
проект, она же версия бэкенда, ansible-ролей и фронтов. Бампается руками перед
выкатом (в отличие от git sha она читается человеком и переживает то, что деплой
катает рабочее дерево, а не коммит).

Как значение доезжает до рантайма:
  * ``deploy_app_stack`` читает ``VERSION`` на контроллере и кладёт в ``.env``
    как ``APP_VERSION``;
  * фронты получают ту же строку build-arg'ом ``VITE_APP_VERSION``;
  * ноды получают её в extra_vars как ``vpn_release_version`` и записывают в
    ``/etc/vpn-node-release.json`` при бутстрапе — так становится видно, какая
    нода какой версией кода прошита.

Локально (без деплоя) env пуст, поэтому есть fallback на чтение файла и, в
последнюю очередь, ``0.0.0-dev``: версия — телеметрия, из-за её отсутствия
процесс падать не должен.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

FALLBACK_VERSION = "0.0.0-dev"

_STARTED_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

# backend/app/version.py → backend/app → backend → корень репозитория.
_REPO_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"
# В образе backend'а корень репозитория не копируется целиком; Dockerfile кладёт
# VERSION рядом с пакетом, поэтому проверяем и /app/VERSION.
_IMAGE_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


def _read_version_file() -> str | None:
    for path in (_IMAGE_VERSION_FILE, _REPO_VERSION_FILE):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return None


@lru_cache(maxsize=1)
def app_version() -> str:
    """Семверная версия выкаченного кода."""
    env_value = (os.getenv("APP_VERSION") or "").strip()
    if env_value:
        return env_value
    return _read_version_file() or FALLBACK_VERSION


def started_at() -> str:
    """ISO-время старта процесса.

    Стоит вместо «времени выката»: деплой пересоздаёт контейнеры, поэтому старт
    процесса и есть момент выката. Писать дату прямо в ``.env`` нельзя — файл
    менялся бы на КАЖДОМ прогоне ansible и дёргал recreate всего стека даже
    тогда, когда ничего не поменялось.
    """
    return _STARTED_AT


def version_info() -> dict[str, str | None]:
    """Сводка для ``GET /api/version`` и для маркера на ноде."""
    return {
        "version": app_version(),
        "started_at": started_at(),
        # Версия ролей намеренно та же, что у приложения: ansible-дерево едет
        # на прод тем же rsync'ом, отдельная нумерация только рассинхронилась
        # бы с кодом, который эти роли вызывает.
        "roles_version": app_version(),
    }
