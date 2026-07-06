"""Finding #124: триаж должен помечать отчёт, обрезанный по max_tokens.

Боевую LLM-петлю не гоняем — подсовываем фейковый модуль ``anthropic``
(реальный локально не импортится: broken ssl) с клиентом, чей
``messages.create`` сразу отдаёт финальный ответ. Проверяем, что при
``stop_reason=='max_tokens'`` в report дописана видимая пометка об усечении,
а при ``end_turn`` — нет.
"""
from __future__ import annotations

import sys
import types

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services.agent import triage


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, stop_reason: str, text: str) -> None:
        self.stop_reason = stop_reason
        self.content = [_Block(text)]


def _install_fake_anthropic(monkeypatch: pytest.MonkeyPatch, resp: _Resp) -> None:
    mod = types.ModuleType("anthropic")

    class _Messages:
        def create(self, **kwargs):  # noqa: ANN003
            return resp

    class _Anthropic:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.messages = _Messages()

    mod.Anthropic = _Anthropic
    mod.APIError = type("APIError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "anthropic", mod)


def _node(db: Session) -> models.VPNNode:
    n = models.VPNNode(
        name="ru-triage-1", region="ru", host="1.2.3.4",
        status=models.VPNNodeStatus.active, is_active=True,
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def test_triage_marks_max_tokens_truncation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    node = _node(db_session)
    _install_fake_anthropic(
        monkeypatch, _Resp("max_tokens", "1. Состояние — нода живёт\n2. Root cause —")
    )

    res = triage.triage_node(db_session, node.id)

    assert res["stop_reason"] == "max_tokens"
    assert "обрезан по лимиту токенов" in res["report"]
    # исходный текст сохранён, пометка дописана в конец
    assert res["report"].startswith("1. Состояние")


def test_triage_end_turn_no_marker(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    node = _node(db_session)
    _install_fake_anthropic(monkeypatch, _Resp("end_turn", "Полный разбор ноды."))

    res = triage.triage_node(db_session, node.id)

    assert res["stop_reason"] == "end_turn"
    assert "обрезан по лимиту токенов" not in res["report"]
    assert res["report"] == "Полный разбор ноды."
