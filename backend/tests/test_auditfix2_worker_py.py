"""Аудит-фиксы (волна 2) worker.py — находки 60, 65, 215.

* #60  — ``dlq_exception_handler`` больше не считает падение финальным, пока у
  джобы остались ретраи, и не приписывает чужие (non-provisioning) джобы
  provisioning-таскам.
* #65  — ``_env_int`` возвращает дефолт на мусорном/пустом env вместо
  ValueError (который на module-level ронял импорт и весь фоновый контур).
* #215 — тело периодического тика ре-бросает неожиданное исключение, чтобы
  джоба честно ушла в ``failed`` и подсветилась в /ops.
"""
from __future__ import annotations

import pytest

from app import worker


class _FakeJob:
    """Минимальный дубль rq.job.Job для проверки веток обработчика."""

    def __init__(self, *, func_name, args, retries_left, job_id="job-x"):
        self.func_name = func_name
        self.args = args
        self.retries_left = retries_left
        self.id = job_id


def _dlq_value() -> float:
    return worker.DLQ_ENTRIES._value.get()


# ── #65: _env_int ──────────────────────────────────────────────────────

def test_env_int_parses_valid(monkeypatch):
    monkeypatch.setenv("AUDITFIX2_INT", "42")
    assert worker._env_int("AUDITFIX2_INT", 7) == 42


def test_env_int_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("AUDITFIX2_INT", "24h")
    # Мусор не роняет процесс — возвращается дефолт.
    assert worker._env_int("AUDITFIX2_INT", 7) == 7


def test_env_int_falls_back_on_empty_and_missing(monkeypatch):
    monkeypatch.setenv("AUDITFIX2_INT", "   ")
    assert worker._env_int("AUDITFIX2_INT", 5) == 5
    monkeypatch.delenv("AUDITFIX2_INT", raising=False)
    assert worker._env_int("AUDITFIX2_INT", 5) == 5


# ── #60: dlq_exception_handler ─────────────────────────────────────────

def test_dlq_skips_while_retries_pending():
    """retries_left>0 → падение транзиентное: без DLQ-инкремента/аудита."""
    before = _dlq_value()
    job = _FakeJob(
        func_name="app.worker.run_provisioning_task",
        args=[123],
        retries_left=2,
    )
    assert worker.dlq_exception_handler(job, RuntimeError, RuntimeError("x"), None) is True
    assert _dlq_value() == before  # счётчик не тронут


def test_dlq_ignores_non_provisioning_job():
    """Финальное падение НЕ-provisioning джобы (args[0] != task_id) не пишет
    provisioning_dlq и не инкрементит счётчик."""
    before = _dlq_value()
    job = _FakeJob(
        func_name="app.worker.run_scale_workers",
        args=[10],  # это число реплик, а не task_id
        retries_left=0,
    )
    assert worker.dlq_exception_handler(job, RuntimeError, RuntimeError("x"), None) is True
    assert _dlq_value() == before


def test_dlq_counts_final_provisioning_failure(db_session):
    """Исчерпавшая ретраи provisioning-джоба → DLQ-счётчик растёт и пишется
    аудит provisioning_dlq."""
    from app import models

    before = _dlq_value()
    job = _FakeJob(
        func_name="app.worker.run_provisioning_task",
        args=[987654],
        retries_left=0,
        job_id="job-final",
    )
    assert worker.dlq_exception_handler(job, RuntimeError, RuntimeError("boom"), None) is True
    assert _dlq_value() == before + 1

    row = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.action == "provisioning_dlq",
            models.AuditLog.target_id == 987654,
        )
        .first()
    )
    assert row is not None


# ── #215: тик ре-бросает неожиданное исключение ────────────────────────

def test_balance_tick_reraises_unexpected(db_session, monkeypatch):
    """Неожиданная ошибка в теле балансового тика больше не глотается —
    джоба уходит в failed (raise), а не тихо возвращает finished."""
    import app.time_utils as time_utils

    def _boom():
        raise RuntimeError("kaboom")

    # utcnow() — первая операция в try-блоке тика; подменяем на бросок.
    monkeypatch.setattr(time_utils, "utcnow", _boom)
    with pytest.raises(RuntimeError, match="kaboom"):
        worker.run_balance_charge_tick()
