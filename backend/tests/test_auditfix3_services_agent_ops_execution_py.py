"""Находка 219: сбой заказа нод в ops-агенте должен писать полный трейсбек
в лог (logger.exception), а не только redact-строку в результат плана.

Заказ VPS — денежная операция с частичными эффектами (часть нод уже оплачена);
для драйверных багов (напр. KeyError в парсинге ответа API) без стектрейса
причину не восстановить.
"""
from __future__ import annotations

import logging

from app.services.agent import ops_execution


class _FakeSession:
    def rollback(self):
        pass


def test_exec_order_node_logs_traceback_on_failure(monkeypatch, caplog):
    """При сбое spawn_node_async: (1) в результат уходит redact-строка,
    (2) в лог пишется полный трейсбек через logger.exception."""
    import app.services.node_spawner as node_spawner

    monkeypatch.setattr(node_spawner, "resolve_spawn_name", lambda *a, **k: "n-1")

    def _boom(*a, **k):
        # Имитируем драйверный баг в парсинге ответа API.
        raise KeyError("id")

    monkeypatch.setattr(node_spawner, "spawn_node_async", _boom)

    step = {"resolved": {"provider_id": 7, "count": 1, "region": "ru", "plan": "x"}}

    # Alembic-миграции на старте харнесса зовут fileConfig(disable_existing_
    # loggers) → логгер модуля приходит disabled=True и его записи не доходят
    # до caplog. Ре-активируем, иначе logger.exception «пропадёт».
    logging.getLogger(ops_execution.__name__).disabled = False

    with caplog.at_level(logging.ERROR, logger=ops_execution.__name__):
        created, err = ops_execution._exec_order_node(_FakeSession(), step, plan_id=42)

    # created пуст (упали на первой ноде), в UI — redact-строка с типом.
    assert created == []
    assert err is not None
    assert "KeyError" in err

    # В лог попал ERROR-трейсбек (exc_info) с местом падения.
    order_errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "node order failed" in r.getMessage()
    ]
    assert order_errors, "ожидался ERROR-лог с трейсбеком заказа нод"
    rec = order_errors[0]
    assert rec.exc_info is not None
    assert rec.exc_info[0] is KeyError
