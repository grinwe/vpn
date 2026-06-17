"""Ops-планировщик (AI_AGENT_ROADMAP Phase 2 — dry-run, БЕЗ выполнения).

Берёт команду на естественном языке («закажи 2 ноды в Германии, подними туннель,
перевези юзеров с ноды 19») и через READ-ONLY tool-слой собирает актуальное
состояние флота (ноды, exit'ы, провайдеры, offerings с ценами, баланс, нагрузка),
после чего ОБЯЗАН вызвать терминальный тул ``submit_plan`` со структурированным
планом: пошагово, с оценкой стоимости (₽) и влияния (сколько юзеров затронем) и
разметкой риска (read/reversible/costly/destructive).

Гардрейлы как у триаж-агента: kill switch ``AGENT_ENABLED``, отдельный
``ANTHROPIC_API_KEY``, кап итераций, ТОЛЬКО read-тулы + терминальный submit_plan.
**Ничего не выполняет** — это чистый dry-run планировщик. Выполнение плана (за одно
подтверждение) — отдельная фаза, поверх этого же плана.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from . import ops_tools

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 8192

# ── Анти-DoS бюджеты (env-override, дефолты безопасны) ──
# Каждый /ops — полный агентный прогон Claude. Без этих границ зависший вызов
# пережил бы 120с-таймаут бота (бэкенд продолжает крутить и биллить), а burst
# параллельных прогонов забил бы sync-threadpool и застопорил весь app.
_REQUEST_TIMEOUT_S = float(os.getenv("AGENT_REQUEST_TIMEOUT", "60"))  # на один вызов Claude
# Дефолт 0: один вызов капится timeout'ом (60с), без ретрая он не растягивается
# до 2×60с=120с — иначе один зависший вызов упёрся бы в 120с-таймаут бота, а
# межитерационный дедлайн одиночный вызов не прерывает.
_MAX_RETRIES = max(0, int(os.getenv("AGENT_MAX_RETRIES", "0")))
_DEADLINE_S = float(os.getenv("AGENT_DEADLINE_S", "100"))  # на весь цикл (< 120с бота)
_MAX_CONCURRENCY = max(1, int(os.getenv("AGENT_MAX_CONCURRENCY", "3")))
_run_slots = threading.BoundedSemaphore(_MAX_CONCURRENCY)

_SYSTEM_PROMPT = """\
Ты — ops-планировщик VPN-as-a-service. Тебе дают команду оператора на естественном
языке. Твоя задача — собрать актуальное состояние флота через READ-ONLY инструменты
и построить ПОШАГОВЫЙ план её выполнения. ТЫ НИЧЕГО НЕ ВЫПОЛНЯЕШЬ И НЕ МЕНЯЕШЬ —
только планируешь; выполнять будет оператор после подтверждения.

Архитектура, которую надо учитывать:
- Обычная нода (VPNNode) — точка входа клиента; РУ-ноды это расходники.
- Зарубежный сервер заводится как EXIT (WGExitNode) за РУ-relay, НЕ прямой нодой:
  прямой зарубежный endpoint душит DPI. «Сделай туннель» = relay→exit link.
- Заказ ноды/exit тратит деньги (списывается с баланса провайдера). Миграция
  юзеров трогает живых клиентов. Это costly/destructive шаги.

Подход:
1. Разбери намерение. Стяни ровно нужный фактаж: list_providers (+ provider_balance,
   provider_offerings для цен/локаций), list_nodes / list_exits, node_load (сколько
   юзеров на ноде — для оценки миграции), list_pools.
2. Считай деньги: стоимость = цена тарифа (из provider_offerings) × кол-во. Локацию/
   тариф/ОС бери реальными id из offerings (не выдумывай).
3. Размечай tier каждого шага: read | reversible | costly | destructive.
   Деньги (заказ/продление) = costly; снос/reinstall/миграция = destructive.
4. В КОНЦЕ ОБЯЗАТЕЛЬНО вызови submit_plan со структурой плана. Не давай финальный
   текст вместо submit_plan.

Будь консервативен и честен: если команда непонятна, невозможна или не хватает
данных (нет провайдера/баланса/ноды) — всё равно вызови submit_plan с feasible=false
и blocked_reason. Не выдумывай id, цены и количества.
"""


class AgentError(RuntimeError):
    """Агент выключен, не сконфигурирован, или вызов LLM упал."""


def _enabled() -> bool:
    return os.getenv("AGENT_ENABLED", "").lower() in ("1", "true", "yes", "on")


_SUBMIT_PLAN_SCHEMA: dict[str, Any] = {
    "name": "submit_plan",
    "description": (
        "Завершить планирование: вернуть итоговый пошаговый план. Вызывай РОВНО "
        "один раз в самом конце, когда собрал достаточно фактов. Ничего не "
        "выполняется — это предложение оператору."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Одна строка: что план делает в сумме.",
            },
            "feasible": {
                "type": "boolean",
                "description": "false, если команду нельзя выполнить / не хватает данных.",
            },
            "blocked_reason": {
                "type": ["string", "null"],
                "description": "Если feasible=false — почему (что мешает/чего не хватает).",
            },
            "steps": {
                "type": "array",
                "description": "Упорядоченные шаги выполнения.",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": (
                                "order_node | order_exit | attach_tunnel | "
                                "migrate_users | reinstall | destroy | set_active | other"
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": "Человекочитаемо (RU), что делает шаг.",
                        },
                        "params": {
                            "type": "object",
                            "description": "Конкретика: provider_id, region, plan, image, count, node_id, exit_id и т.п.",
                            "additionalProperties": True,
                        },
                        "tier": {
                            "type": "string",
                            "enum": ["read", "reversible", "costly", "destructive"],
                        },
                        "est_cost_rub": {"type": ["number", "null"]},
                        "est_users_affected": {"type": ["integer", "null"]},
                        "reversible": {"type": "boolean"},
                        "warnings": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["kind", "description", "tier", "reversible"],
                },
            },
            "total_est_cost_rub": {"type": ["number", "null"]},
            "total_users_affected": {"type": ["integer", "null"]},
            "needs_confirmation": {
                "type": "boolean",
                "description": "true, если есть хоть один costly/destructive шаг.",
            },
            "notes": {
                "type": "string",
                "description": "Оговорки, допущения, чего не хватило.",
            },
        },
        "required": ["summary", "feasible", "steps", "needs_confirmation"],
    },
}


def plan_ops(db: Session, command: str, *, model: str | None = None) -> dict:
    """Построить dry-run ops-план по NL-команде. Возвращает
    ``{command, model, plan, iterations, tool_calls}`` где ``plan`` — структура
    из submit_plan. Бросает :class:`AgentError`, если агент выключен / нет ключа /
    LLM-сбой / план не собрался. НИЧЕГО НЕ ВЫПОЛНЯЕТ."""
    if not _enabled():
        raise AgentError("AI-агент выключен (включается флагом AGENT_ENABLED=1)")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise AgentError("ANTHROPIC_API_KEY не задан — агенту нечем ходить в Claude")
    command = (command or "").strip()
    if not command:
        raise AgentError("пустая команда")

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise AgentError("пакет anthropic не установлен в образе") from exc

    model = model or os.getenv("AGENT_MODEL", _DEFAULT_MODEL)
    max_iter = max(1, int(os.getenv("AGENT_MAX_ITERATIONS", "12")))
    client = anthropic.Anthropic(timeout=_REQUEST_TIMEOUT_S, max_retries=_MAX_RETRIES)
    tools = [*ops_tools.tool_schemas(), _SUBMIT_PLAN_SCHEMA]

    messages: list[dict] = [
        {"role": "user", "content": f"Команда оператора:\n{command}"}
    ]
    tool_calls: list[str] = []

    # Семафор: burst /ops не должен выжрать sync-threadpool и застопорить app.
    if not _run_slots.acquire(blocking=False):
        raise AgentError(
            f"агент занят: уже идёт {_MAX_CONCURRENCY} планирований "
            "(AGENT_MAX_CONCURRENCY) — повтори через минуту"
        )
    deadline = time.monotonic() + _DEADLINE_S
    try:
        for iteration in range(max_iter):
            # Wall-clock дедлайн на весь цикл: бэкенд не должен пережить
            # 120с-таймаут бота, продолжая крутить и биллить.
            if time.monotonic() > deadline:
                raise AgentError(
                    f"план не собрался за отведённое время (~{int(_DEADLINE_S)}с) — упрости команду"
                )
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=_MAX_TOKENS,
                    system=_SYSTEM_PROMPT,
                    thinking={"type": "adaptive"},
                    tools=tools,
                    messages=messages,
                )
            except anthropic.APIError as exc:
                raise AgentError(f"Claude API error: {exc}") from exc

            if resp.stop_reason != "tool_use":
                # Финал без submit_plan — модель не дала план. Возвращаем как
                # неуспех планирования (не выдумываем).
                text = "\n".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                ).strip()
                raise AgentError(
                    f"планировщик не вызвал submit_plan (вернул текст): {text[:300]}"
                )

            messages.append({"role": "assistant", "content": resp.content})

            results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls.append(block.name)

                # Терминальный тул — захватываем план и выходим (НИЧЕГО не выполняем).
                if block.name == "submit_plan":
                    plan = dict(block.input or {})
                    return {
                        "command": command,
                        "model": model,
                        "plan": plan,
                        "iterations": iteration + 1,
                        "tool_calls": tool_calls,
                    }

                entry = ops_tools.TOOL_REGISTRY.get(block.name)
                if not entry:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"unknown tool {block.name}",
                            "is_error": True,
                        }
                    )
                    continue
                try:
                    out = entry["fn"](db, **(block.input or {}))
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(out, ensure_ascii=False),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("ops planner tool %s failed", block.name)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"tool error: {exc}",
                            "is_error": True,
                        }
                    )
            messages.append({"role": "user", "content": results})

        raise AgentError(
            f"план не собрался за {max_iter} итераций (увеличь AGENT_MAX_ITERATIONS)"
        )
    finally:
        _run_slots.release()
