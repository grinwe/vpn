"""Audit-fix #207: /ops/worker/scale не должен sleep-поллить job до 30с внутри
HTTP-запроса — окно ожидания сужено (env OPS_SCALE_WAIT_SECONDS, дефолт 5с),
а не дождавшись — отвечает status=enqueued (виджет дотянет счётчик поллингом).
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy.orm import Session

from app import schemas
from app.api import ops


class _FakeJob:
    """Job, который никогда не финишит — эмулирует «скейл ещё идёт»."""

    id = "job-fake"
    result = None
    exc_info = None

    def get_status(self, refresh: bool = False) -> str:  # noqa: ARG002
        return "queued"


class _FakeQueue:
    def enqueue(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
        return _FakeJob()


def test_scale_returns_enqueued_quickly(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Незавершённый job → status=enqueued за ~окно ожидания, а не за 30с."""
    monkeypatch.setattr(ops, "get_queue", lambda: _FakeQueue())
    monkeypatch.setenv("OPS_SCALE_WAIT_SECONDS", "1")

    started = time.monotonic()
    out = ops.scale_workers(
        body=schemas.WorkerScaleRequest(replicas=3),
        db=db_session,
        admin_token="x",
        admin_actor=None,
    )
    elapsed = time.monotonic() - started

    assert out.status == "enqueued"
    assert out.replicas == 3
    # Раньше цикл крутился до 30с; теперь окно = 1с (+сон 1с на последней итерации).
    assert elapsed < 5, f"ожидание {elapsed:.1f}s слишком долгое"


def test_scale_wait_seconds_bad_value_falls_back(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Некорректный env не роняет эндпоинт — дефолтимся и всё равно отвечаем."""
    monkeypatch.setattr(ops, "get_queue", lambda: _FakeQueue())
    monkeypatch.setenv("OPS_SCALE_WAIT_SECONDS", "not-a-number")
    # Не ждём реальные ~5с дефолта — цикл прокручивается мгновенно.
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    out = ops.scale_workers(
        body=schemas.WorkerScaleRequest(replicas=1),
        db=db_session,
        admin_token="x",
        admin_actor=None,
    )
    assert out.status == "enqueued"
