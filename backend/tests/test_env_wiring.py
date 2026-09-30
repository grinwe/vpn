"""Паритет переменных подписки между ansible-шаблоном и docker-compose.

Зачем отдельный тест. У сервиса ``backend`` в docker-compose.yml НЕТ
``env_file`` — переменные попадают в контейнер только через явный список
``environment:``. Переменная, добавленная в ``env.j2`` и в group_vars, но
забытая здесь, ведёт себя хуже, чем несуществующая: на хосте ``.env`` её
содержит, деплой выглядит настроенным, а процесс её не видит.

Так уже было: ``SUB_HAPP_AUTOCONNECT`` пролежал мёртвым пять недель при
group_var ``all`` (зафиксировано в docs/operations/env-reference.md). Тест
ловит именно этот класс — расхождение двух списков, а не сам факт наличия.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENV_TEMPLATE = REPO / "infra/ansible/roles/deploy_app_stack/templates/env.j2"
COMPOSE = REPO / "docker-compose.yml"

# Префиксы, для которых паритет обязателен. Семейство подписки целиком
# читается backend'ом, и именно в нём набиты шишки. TRIAL_ — с триала на 3
# дня (2026-09-30): до него эти переменные не были проброшены никуда и
# работали только дефолтами из кода.
GUARDED_PREFIXES = ("SUB_", "TRIAL_")


def _template_keys(prefix: str) -> set[str]:
    text = ENV_TEMPLATE.read_text(encoding="utf-8")
    return set(re.findall(rf"^({prefix}[A-Z0-9_]+)=", text, re.MULTILINE))


def _compose_keys(prefix: str) -> set[str]:
    text = COMPOSE.read_text(encoding="utf-8")
    return set(re.findall(rf"^\s+({prefix}[A-Z0-9_]+):", text, re.MULTILINE))


@pytest.mark.skipif(
    not ENV_TEMPLATE.exists() or not COMPOSE.exists(),
    reason="запуск вне полного дерева репозитория",
)
@pytest.mark.parametrize("prefix", GUARDED_PREFIXES)
def test_sub_env_vars_reach_the_container(prefix):
    missing = sorted(_template_keys(prefix) - _compose_keys(prefix))
    assert not missing, (
        "Эти переменные есть в env.j2, но не проброшены в docker-compose.yml "
        f"(у backend нет env_file — они молча не доедут до процесса): {missing}"
    )


@pytest.mark.skipif(
    not ENV_TEMPLATE.exists() or not COMPOSE.exists(),
    reason="запуск вне полного дерева репозитория",
)
@pytest.mark.parametrize("prefix", GUARDED_PREFIXES)
def test_no_orphan_sub_vars_in_compose(prefix):
    """Обратная сторона: проброшенная, но никем не рендеримая переменная —
    это мёртвый рубильник, который в инциденте будут крутить впустую."""
    orphans = sorted(_compose_keys(prefix) - _template_keys(prefix))
    assert not orphans, (
        "Эти переменные проброшены в docker-compose.yml, но env.j2 их не "
        f"рендерит — в .env они не появятся: {orphans}"
    )
