"""Аудит-фикс conftest (находки 188, 192, 194).

188 — стейт-машина исполнения provisioning-таски (run_task) раньше не
    прогонялась ни одним тестом: autouse-фикстура _no_provisioning глушила
    run_task_async, а run_task никто не звал напрямую. Здесь мы:
      * проверяем сам opt-out (@pytest.mark.real_provisioning снимает заглушку);
      * прогоняем реальные ветки run_task (pending→running→success/failed/
        cancelled), мокая только ansible (_execute_task) и побочки outcome.

Проверки предохранителя DROP SCHEMA (192) и очистки сетевого env (194)
делаются на уровне самого conftest и здесь дублируются точечными ассертами
на итоговое состояние процесса (env вычищен до импорта app).
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services.provisioning import ProvisioningOrchestrator
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


# ---------------------------------------------------------------------------
# 194 — сетевые переменные вычищены до импорта app (реальный Telegram не дёргается)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "var",
    [
        "BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_URL",
        "TELEGRAM_WEBHOOK_SECRET_TOKEN",
        "ADMIN_TELEGRAM_IDS",
    ],
)
def test_network_env_scrubbed(var: str) -> None:
    assert os.getenv(var) is None


# ---------------------------------------------------------------------------
# 188 — opt-out маркера real_provisioning реально снимает заглушку
# ---------------------------------------------------------------------------
def test_run_task_async_stubbed_by_default() -> None:
    # Без маркера autouse-фикстура подменяет run_task_async на локальный _noop.
    assert ProvisioningOrchestrator.run_task_async.__name__ == "_noop"


@pytest.mark.real_provisioning
def test_run_task_async_not_stubbed_with_marker() -> None:
    # С маркером заглушка снята — метод оригинальный.
    assert ProvisioningOrchestrator.run_task_async.__name__ != "_noop"


# ---------------------------------------------------------------------------
# 188 — реальная стейт-машина run_task (ansible замокан на уровне _execute_task)
# ---------------------------------------------------------------------------
def _make_apply_task(db: Session) -> tuple[ProvisioningOrchestrator, models.ProvisioningTask]:
    plan = make_plan(db)
    user = make_user(db)
    node = make_node(db)
    cfg = make_config(db, node)
    sub = make_subscription(db, user, plan, node)
    device = make_device(db, sub, cfg)
    orch = ProvisioningOrchestrator(db)
    task = orch.create_task(
        "device", device.id, "apply", {"username": device.access_username}
    )
    db.commit()
    return orch, task


def _spy_outcome(orch: ProvisioningOrchestrator) -> list[bool]:
    """Подменяем _handle_task_outcome записью факта вызова: его собственные
    побочки покрыты test_auditfix_provisioning; здесь важно лишь, что run_task
    зовёт outcome с правильным success-флагом и корректно проставляет статус."""
    calls: list[bool] = []
    orch._handle_task_outcome = lambda task, *, success: calls.append(success)  # type: ignore[method-assign]
    return calls


def test_run_task_success_marks_success(db_session: Session) -> None:
    orch, task = _make_apply_task(db_session)
    outcome = _spy_outcome(orch)
    orch._execute_task = lambda task, node=None: {"returncode": 0}  # type: ignore[method-assign]

    result = orch.run_task(task)

    assert result.status == models.ProvisioningTaskStatus.success
    assert result.started_at is not None
    assert outcome == [True]


def test_run_task_ansible_nonzero_marks_failed(db_session: Session) -> None:
    orch, task = _make_apply_task(db_session)
    outcome = _spy_outcome(orch)
    orch._execute_task = lambda task, node=None: {  # type: ignore[method-assign]
        "returncode": 2,
        "stdout": "fatal: boom",
        "stderr": "",
    }

    result = orch.run_task(task)

    assert result.status == models.ProvisioningTaskStatus.failed
    assert result.error_message  # хвост stdout/stderr сохранён
    assert outcome == [False]


def test_run_task_unexpected_exception_marks_failed(db_session: Session) -> None:
    orch, task = _make_apply_task(db_session)
    outcome = _spy_outcome(orch)

    def _boom(task, node=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("inventory build failed")

    orch._execute_task = _boom  # type: ignore[method-assign]

    result = orch.run_task(task)

    assert result.status == models.ProvisioningTaskStatus.failed
    assert outcome == [False]


def test_run_task_cancel_requested_skips_execution(db_session: Session) -> None:
    from app.services.provisioning import utcnow

    orch, task = _make_apply_task(db_session)
    outcome = _spy_outcome(orch)
    ran = {"execute": False}

    def _should_not_run(task, node=None):  # type: ignore[no-untyped-def]
        ran["execute"] = True
        return {"returncode": 0}

    orch._execute_task = _should_not_run  # type: ignore[method-assign]

    # Оператор отменил таску, пока она ждала в очереди.
    task.cancel_requested_at = utcnow()
    db_session.commit()

    result = orch.run_task(task)

    assert result.status == models.ProvisioningTaskStatus.cancelled
    assert ran["execute"] is False
    # Отмена ≠ поломка: outcome НЕ вызывается (ноду не демотим).
    assert outcome == []
