"""Тесты `admin_notify.notify_admins`.

Покрываем: пустой env, N админов → N строк, дедуп в окне, разные
dedup_key → разные серии, autocommit-поведение, расширяемый `extra`.

Используем реальный Postgres через фикстуру `db_session` из conftest —
JSONB containment `@>` не эмулируется SQLite, поэтому тест обязан
гонять против боевого драйвера.
"""
from __future__ import annotations

import pytest

from app import models
from app.services import admin_notify


def _count_alerts(db_session, kind: str) -> int:
    return (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == f"admin_alert_{kind}")
        .count()
    )


def test_empty_env_returns_empty(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой ADMIN_TELEGRAM_IDS → ранний return, AuditLog не трогаем."""
    monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)

    result = admin_notify.notify_admins(
        db_session,
        kind="user_report",
        text="hello",
        dedup_key={"node_id": 1, "user_id": 2},
    )

    assert result == []
    assert _count_alerts(db_session, "user_report") == 0


def test_blank_env_returns_empty(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Только пробелы/запятые → пустой список id'шников, push не идёт."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "   ,  ,")

    result = admin_notify.notify_admins(
        db_session, kind="infra_ssh", text="t", dedup_key={}
    )

    assert result == []


def test_writes_one_row_per_admin(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Три админа → три AuditLog-строки, уникальные telegram_id, общий text."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111,222,333")

    ids = admin_notify.notify_admins(
        db_session,
        kind="user_report",
        text="юзер 42 жалуется",
        dedup_key={"node_id": 5, "user_id": 42},
        extra={"source": "bot"},
        autocommit=True,
    )

    assert len(ids) == 3
    rows = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_user_report")
        .order_by(models.AuditLog.id.asc())
        .all()
    )
    assert len(rows) == 3

    telegram_ids = sorted(r.extra["telegram_id"] for r in rows)
    assert telegram_ids == ["111", "222", "333"]

    for r in rows:
        assert r.actor == "admin_notify"
        assert r.actor_type == models.AuditActor.system
        assert r.target_type == "admin_alert"
        assert r.extra["kind"] == "user_report"
        assert r.extra["text"] == "юзер 42 жалуется"
        assert r.extra["node_id"] == 5
        assert r.extra["user_id"] == 42
        assert r.extra["source"] == "bot"


def test_dedup_within_window_suppresses_repeat(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повтор с тем же {kind, dedup_key} в окне → пустой результат, новых строк нет."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111,222")
    monkeypatch.setenv("ADMIN_ALERT_DEDUP_WINDOW_SEC", "600")

    first = admin_notify.notify_admins(
        db_session,
        kind="infra_dlq",
        text="job упал",
        dedup_key={"task_id": "abc-123"},
        autocommit=True,
    )
    assert len(first) == 2

    second = admin_notify.notify_admins(
        db_session,
        kind="infra_dlq",
        text="job упал снова (этот текст не уйдёт)",
        dedup_key={"task_id": "abc-123"},
        autocommit=True,
    )
    assert second == []
    assert _count_alerts(db_session, "infra_dlq") == 2


def test_different_dedup_key_creates_new_series(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Другой task_id → самостоятельная серия, дедуп не срабатывает."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")

    ids_a = admin_notify.notify_admins(
        db_session,
        kind="infra_dlq",
        text="A",
        dedup_key={"task_id": "task-A"},
        autocommit=True,
    )
    ids_b = admin_notify.notify_admins(
        db_session,
        kind="infra_dlq",
        text="B",
        dedup_key={"task_id": "task-B"},
        autocommit=True,
    )

    assert len(ids_a) == 1
    assert len(ids_b) == 1
    assert _count_alerts(db_session, "infra_dlq") == 2


def test_different_kind_is_independent(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тот же dedup_key, но другой kind → дедуп не пересекается."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")

    r1 = admin_notify.notify_admins(
        db_session, kind="user_report", text="u", dedup_key={"x": 1}, autocommit=True
    )
    r2 = admin_notify.notify_admins(
        db_session, kind="infra_ssh", text="i", dedup_key={"x": 1}, autocommit=True
    )

    assert len(r1) == 1
    assert len(r2) == 1


def test_window_sec_override(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """window_sec=0 означает «окно в прошлое нулевое» → подавляет только
    одновременно созданные записи. Наш хелпер фильтрует `created_at >= cutoff`
    с `cutoff = now - window`. Когда window=0 и created_at у первой серии
    строго < текущего now (разница обычно микросекунды, но timestamp
    внутри одного transaction обычно равен), containment будет матчить
    пока строки попадают в фильтр. Проверяем, что override-аргумент вообще
    проходит до needle, на очень маленьком окне дедуп всё ещё может
    сработать — поэтому здесь просто smoke-тест: явно переданный
    window_sec не падает."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")

    ids = admin_notify.notify_admins(
        db_session,
        kind="infra_ssh",
        text="t",
        dedup_key={},
        window_sec=1,
        autocommit=True,
    )
    assert len(ids) == 1


def test_autocommit_false_does_not_commit(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """autocommit=False → flush, но commit не делается; rollback откатывает."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")

    ids = admin_notify.notify_admins(
        db_session,
        kind="user_report",
        text="pending",
        dedup_key={"user_id": 1},
        autocommit=False,
    )
    assert len(ids) == 1

    db_session.rollback()

    # После rollback запись должна исчезнуть
    assert _count_alerts(db_session, "user_report") == 0


def test_autocommit_true_persists_after_rollback(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """autocommit=True → commit внутри хелпера, rollback не откатит."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")

    ids = admin_notify.notify_admins(
        db_session,
        kind="user_report",
        text="persisted",
        dedup_key={"user_id": 1},
        autocommit=True,
    )
    assert len(ids) == 1

    db_session.rollback()

    assert _count_alerts(db_session, "user_report") == 1


def test_bad_telegram_id_skipped(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Невалидный id в CSV тихо пропускается, валидный доходит."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111,notanint,222")

    ids = admin_notify.notify_admins(
        db_session,
        kind="infra_ssh",
        text="ok",
        dedup_key={},
        autocommit=True,
    )
    assert len(ids) == 2
