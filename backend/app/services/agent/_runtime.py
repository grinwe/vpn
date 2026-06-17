"""Общие runtime-гардрейлы для AI-агентов (ops-планировщик, триаж).

И ops, и triage гоняют одинаковую агентную петлю Claude tool-use. Анти-DoS
бюджеты и хелперы держим в ОДНОМ месте, чтобы границы не разъезжались между
агентами:

- ``client_kwargs()`` — таймаут на вызов + без растягивающих ретраев.
- ``run_budget()`` — общий семафор (суммарный кап параллельных прогонов ops+triage,
  оба занимают sync-threadpool-слоты одного процесса) + wall-clock дедлайн на цикл.
- ``redact()`` — скраббер секретов перед записью в лог (read-тулы расшифровывают
  провайдерский токен; полный трейсбек из этого пути — тонкая щель утечки).
- ``cap_json()`` — кап размера tool-вывода, чтобы контекст не рос квадратично.

Все кнобы — ``os.getenv`` с безопасными дефолтами (как ``AGENT_MAX_ITERATIONS``),
без проводки в compose/vault.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any

REQUEST_TIMEOUT_S = float(os.getenv("AGENT_REQUEST_TIMEOUT", "60"))  # на один вызов Claude
# Дефолт 0: один вызов капится timeout'ом, без ретрая он не растягивается до
# 2×timeout — иначе зависший вызов упёрся бы в 120с-таймаут бота, а
# межитерационный дедлайн одиночный вызов не прерывает.
MAX_RETRIES = max(0, int(os.getenv("AGENT_MAX_RETRIES", "0")))
DEADLINE_S = float(os.getenv("AGENT_DEADLINE_S", "100"))  # на весь цикл (< 120с бота)
MAX_CONCURRENCY = max(1, int(os.getenv("AGENT_MAX_CONCURRENCY", "3")))
TOOL_OUTPUT_CAP = max(1024, int(os.getenv("AGENT_TOOL_OUTPUT_CAP", "16000")))

# Общий семафор на ВСЕ агентные прогоны (ops + triage): burst не должен выжрать
# sync-threadpool и застопорить остальной app.
_RUN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENCY)


class AgentError(RuntimeError):
    """Агент выключен, не сконфигурирован, занят, или вызов LLM упал.

    Один тип на оба агента: ``api/agent.py`` ловит его и отдаёт 503. ops/triage
    реэкспортируют его (``from ._runtime import AgentError``)."""


def client_kwargs() -> dict[str, Any]:
    """kwargs для ``anthropic.Anthropic()`` — таймаут на вызов + без ретраев."""
    return {"timeout": REQUEST_TIMEOUT_S, "max_retries": MAX_RETRIES}


@contextmanager
def run_budget(label: str):
    """Захватить слот прогона (иначе ``AgentError`` «занят») и отдать дедлайн
    на цикл (monotonic-таймстемп). Слот всегда освобождается; дедлайн проверяет
    вызывающий между итерациями."""
    if not _RUN_SLOTS.acquire(blocking=False):
        raise AgentError(
            f"агент занят: уже идёт {MAX_CONCURRENCY} прогонов "
            "(AGENT_MAX_CONCURRENCY) — повтори через минуту"
        )
    try:
        yield time.monotonic() + DEADLINE_S
    finally:
        _RUN_SLOTS.release()


_SECRET_RE = re.compile(
    r"(?i)\b(token|api[_-]?key|authinfo|password|passwd|secret|bearer)\b\s*[=:]\s*\S+"
)


def redact(text: str) -> str:
    """Затереть похожее на секреты (key=value) перед записью в лог."""
    return _SECRET_RE.sub(r"\1=<redacted>", text or "")


def cap_json(obj: Any, *, limit: int = TOOL_OUTPUT_CAP) -> str:
    """``json.dumps`` с капом размера — tool-вывод не должен раздувать контекст."""
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) > limit:
        return s[:limit] + f'… "[обрезано, всего {len(s)} симв.]"'
    return s
