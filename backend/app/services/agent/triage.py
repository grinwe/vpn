"""Diagnostic triage agent (AI_AGENT_ROADMAP Phase 1 — read-only).

Берёт node_id, через read-only tool-слой (tools.py) собирает
overview/health/traffic/configs/provisioning, коррелирует Claude tool-use'ом и
выдаёт человекочитаемый root-cause + рекомендованное действие — БЕЗ выполнения.

Гардрейлы: kill switch AGENT_ENABLED (по умолчанию off), отдельный LLM-ключ
ANTHROPIC_API_KEY (не мастер-ключ системы), кап итераций, только read-тулы.
Модель — claude-sonnet-4-6 по умолчанию (AGENT_MODEL override), adaptive
thinking. Никаких мутаций на этой фазе.
"""
from __future__ import annotations

import json
import logging
import os
import time

from sqlalchemy.orm import Session

from ... import models
from . import _runtime
from ._runtime import AgentError
from .tools import TOOL_REGISTRY, tool_schemas

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-6"
# adaptive thinking делит тот же бюджет с текстом ответа, поэтому берём с запасом,
# чтобы разбор не обрывался на полуслове (см. обработку stop_reason=='max_tokens').
_MAX_TOKENS = 8192

_SYSTEM_PROMPT = """\
Ты — ops-агент диагностического триажа VPN-as-a-service. Тебе дают ID ноды.
Через предоставленные READ-ONLY инструменты собери факты (overview, health-пробы,
traffic-сэмплы, конфиги протоколов, недавние provisioning-таски), скоррелируй их и
выдай КРАТКИЙ разбор. Ты НИЧЕГО не выполняешь и не меняешь — только анализируешь и
рекомендуешь.

Подход:
- Начни с get_node_overview. Дальше тяни ровно то, что нужно для гипотезы (не
  дёргай все тулы вслепую).
- Типичные паттерны: status=error/registering + проваленная bootstrap-таска →
  смотри error_message; active_users>0 но traffic-нули → трафик не идёт (egress/
  relay); health-пробы timeout только в части регионов → блокировка на ISP/РКН;
  reconcile_pending=true давно → залип reconcile.

Ответ (на русском, сжато):
1. **Состояние** — одна строка: что с нодой сейчас.
2. **Root cause** — наиболее вероятная причина, с опорой на конкретные факты из тулов.
3. **Рекомендация** — конкретное действие оператору (bootstrap / reinstall / migrate /
   проверить firewall / заменить IP / и т.п.). Без выполнения.
4. **Уверенность** — high/medium/low + чего не хватило, если low.
Если данных мало или нода не найдена — скажи прямо, не выдумывай.
"""


def _enabled() -> bool:
    return os.getenv("AGENT_ENABLED", "").lower() in ("1", "true", "yes", "on")


def triage_node(db: Session, node_id: int, *, model: str | None = None) -> dict:
    """Прогнать read-only триаж по ноде. Возвращает
    {node_id, model, report, iterations, tool_calls}. Бросает AgentError, если
    агент выключен / нет ключа / нет ноды / LLM-сбой."""
    if not _enabled():
        raise AgentError("AI-агент выключен (включается флагом AGENT_ENABLED=1)")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise AgentError("ANTHROPIC_API_KEY не задан — агенту нечем ходить в Claude")
    if not db.get(models.VPNNode, node_id):
        raise AgentError(f"node {node_id} not found")

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise AgentError("пакет anthropic не установлен в образе") from exc

    model = model or os.getenv("AGENT_MODEL", _DEFAULT_MODEL)
    max_iter = max(1, int(os.getenv("AGENT_MAX_ITERATIONS", "10")))
    client = anthropic.Anthropic(**_runtime.client_kwargs())
    tools = tool_schemas()

    messages: list[dict] = [
        {
            "role": "user",
            "content": f"Проведи диагностический триаж ноды #{node_id}.",
        }
    ]
    tool_calls: list[str] = []
    tool_cache: dict[str, str] = {}  # дедуп одинаковых read-вызовов за прогон

    # run_budget: общий семафор (ops+triage) + wall-clock дедлайн на цикл.
    with _runtime.run_budget("triage") as deadline:
        for iteration in range(max_iter):
            if time.monotonic() > deadline:
                raise AgentError(
                    f"триаж не сошёлся за отведённое время (~{int(_runtime.DEADLINE_S)}с)"
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
                # Финальный ответ — собираем текст.
                report = "\n".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                ).strip()
                report = report or "(агент не вернул текст)"
                if resp.stop_reason == "max_tokens":
                    # Отчёт обрезан по лимиту токенов — оператор мог не увидеть
                    # секции «Рекомендация»/«Уверенность». Помечаем явно, чтобы
                    # усечение не выглядело как полный разбор.
                    report += (
                        "\n\n⚠️ Отчёт обрезан по лимиту токенов (max_tokens) — "
                        "разбор может быть неполным. Перезапусти триаж."
                    )
                return {
                    "node_id": node_id,
                    "model": model,
                    "report": report,
                    "iterations": iteration + 1,
                    "tool_calls": tool_calls,
                    "stop_reason": resp.stop_reason,
                }

            # Сохраняем ассистент-ход целиком (включая thinking-блоки с сигнатурами).
            messages.append({"role": "assistant", "content": resp.content})

            results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls.append(block.name)
                entry = TOOL_REGISTRY.get(block.name)
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
                cache_key = (
                    block.name
                    + ":"
                    + json.dumps(block.input or {}, sort_keys=True, ensure_ascii=False)
                )
                cached = tool_cache.get(cache_key)
                if cached is not None:
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": cached}
                    )
                    continue
                try:
                    # Все тулы — read-only, принимают (db, **input).
                    out = entry["fn"](db, **(block.input or {}))
                    content = _runtime.cap_json(out)
                    tool_cache[cache_key] = content
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": content}
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "agent tool %s failed: %s",
                        block.name,
                        _runtime.redact(f"{type(exc).__name__}: {exc}"),
                    )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _runtime.redact(f"tool error: {exc}"),
                            "is_error": True,
                        }
                    )
            messages.append({"role": "user", "content": results})

        raise AgentError(
            f"триаж не сошёлся за {max_iter} итераций (увеличь AGENT_MAX_ITERATIONS)"
        )
