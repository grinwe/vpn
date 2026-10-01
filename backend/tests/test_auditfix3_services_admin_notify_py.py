"""Находка #125 — дедуп админ-алертов гонится между процессами.

`notify_admins` делал check-then-insert без блокировки: два конкурентных
события с одним `dedup_key` (burst при массовом падении нод) оба проходили
SELECT и оба создавали серии → дубли пушей каждому админу. Фикс: перед
проверкой берём `pg_advisory_xact_lock(hashtextextended(...))`, чтобы
сериализовать конкурентные вставки одного needle; лок снимается на
commit/rollback той же транзакции.

Гоняем против реального Postgres (фикстура `db_session` из conftest):
advisory-lock — pg-специфичный примитив, на SQLite не эмулируется.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app import models
from app.services import admin_notify


def _advisory_lock_count(db_session) -> int:
    """Сколько advisory-локов держит ТЕКУЩАЯ сессия прямо сейчас."""
    return (
        db_session.execute(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            )
        ).scalar()
        or 0
    )


def test_advisory_lock_taken_in_insert_transaction(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Лок берётся в той же (незакоммиченной) транзакции, что и insert.

    Вызываем с autocommit=False → транзакция открыта, xact-лок обязан
    висеть, а строки — уже быть добавлены (flush).
    """
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111,222")

    before = _advisory_lock_count(db_session)

    ids = admin_notify.notify_admins(
        db_session,
        kind="user_report",
        text="нода упала",
        dedup_key={"node_id": 7},
        autocommit=False,
    )

    # Серия создана (по строке на админа) и лок висит в этой же транзакции.
    assert len(ids) == 2
    assert _advisory_lock_count(db_session) == before + 1


def test_advisory_lock_key_matches_needle(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Классобъект-лока = hashtextextended(action_name + needle).

    Проверяем, что взятый лок совпадает по ключу с ожидаемым hash — т.е.
    сериализуются именно одинаковые needle, а разные dedup_key не блокируют
    друг друга.
    """
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111")

    kind = "node_diagnosis"
    dedup_key = {"target_kind": "node", "target_id": 42}
    needle = {"kind": kind, **dedup_key}
    lock_needle = f"admin_alert_{kind}" + json.dumps(needle, sort_keys=True)

    expected_key = db_session.execute(
        text("SELECT hashtextextended(:n, 0)"), {"n": lock_needle}
    ).scalar()

    admin_notify.notify_admins(
        db_session,
        kind=kind,
        text="диагностика",
        dedup_key=dedup_key,
        autocommit=False,
    )

    # pg_locks для 64-битного advisory-лока раскладывает ключ на два int4:
    # classid (старшие 32 бита) и objid (младшие 32). Собираем обратно.
    row = db_session.execute(
        text(
            "SELECT classid, objid FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
            "AND objsubid = 1"
        )
    ).fetchone()
    assert row is not None
    got_key = (int(row[0]) << 32) | (int(row[1]) & 0xFFFFFFFF)
    # hashtextextended возвращает signed bigint; приводим к беззнаковому
    # представлению pg_locks для сравнения.
    assert got_key == (int(expected_key) & 0xFFFFFFFFFFFFFFFF)


def test_dedup_still_blocks_second_series(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Лок не ломает основную дедуп-логику: второй вызов с тем же needle
    в окне по-прежнему возвращает пустой список."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111,222")

    first = admin_notify.notify_admins(
        db_session,
        kind="user_report",
        text="раз",
        dedup_key={"node_id": 9},
        autocommit=True,
    )
    second = admin_notify.notify_admins(
        db_session,
        kind="user_report",
        text="два",
        dedup_key={"node_id": 9},
        autocommit=True,
    )

    assert len(first) == 2
    assert second == []
    assert (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "admin_alert_user_report")
        .count()
        == 2
    )
