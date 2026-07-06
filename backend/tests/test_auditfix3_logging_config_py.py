"""Тесты структурного access-лога (находка 220).

Проверяют helper log_request: одно событие на запрос с method/path/status/
duration_ms, фильтрацию шумных путей и выключение через ACCESS_LOG=0.
"""
from __future__ import annotations

import structlog

from app.logging_config import _ACCESS_LOG_SKIP_PATHS, log_request


def test_log_request_emits_structured_event(monkeypatch):
    monkeypatch.delenv("ACCESS_LOG", raising=False)
    with structlog.testing.capture_logs() as logs:
        log_request("GET", "/api/users/{user_id}", 200, 12.34)
    assert len(logs) == 1
    ev = logs[0]
    assert ev["event"] == "http_request"
    assert ev["method"] == "GET"
    assert ev["path"] == "/api/users/{user_id}"
    assert ev["status"] == 200
    assert ev["duration_ms"] == 12.3  # округление до 0.1
    assert ev["log_level"] == "info"


def test_log_request_skips_noisy_paths(monkeypatch):
    monkeypatch.delenv("ACCESS_LOG", raising=False)
    for path in _ACCESS_LOG_SKIP_PATHS:
        with structlog.testing.capture_logs() as logs:
            log_request("GET", path, 200, 1.0)
        assert logs == [], f"путь {path} не должен логироваться"


def test_log_request_disabled_via_env(monkeypatch):
    monkeypatch.setenv("ACCESS_LOG", "0")
    with structlog.testing.capture_logs() as logs:
        log_request("POST", "/api/pay", 500, 99.0)
    assert logs == []


def test_log_request_env_off_variants(monkeypatch):
    for val in ("false", "OFF", "No"):
        monkeypatch.setenv("ACCESS_LOG", val)
        with structlog.testing.capture_logs() as logs:
            log_request("GET", "/api/x", 200, 1.0)
        assert logs == [], f"ACCESS_LOG={val} должен выключать журнал"
