"""Regression test for finding #68.

Раньше run_migrations() логировал захардкоженную константу _ALEMBIC_HEAD
("0009_balance_billing"), которая вечно отставала от реальных миграций.
Теперь фактическая голова читается из самого Alembic, а лог должен
сообщать именно её.
"""
from __future__ import annotations

import logging

from app import migrations


def test_alembic_head_constant_removed():
    # Константа удалена — источником правды больше не является статический литерал.
    assert not hasattr(migrations, "_ALEMBIC_HEAD")


def test_run_migrations_logs_actual_head(monkeypatch, caplog):
    """run_migrations() логирует то, что вернул _run_alembic_upgrade()."""
    monkeypatch.delenv("SKIP_MIGRATIONS", raising=False)

    # Не трогаем реальную БД: подменяем движок и бэкфилл.
    monkeypatch.setattr(migrations, "create_engine", lambda *a, **kw: object())
    monkeypatch.setattr(migrations, "_backfill_from_legacy", lambda engine: None)
    # Возвращаем реалистичную «голову», отличную от старой константы.
    monkeypatch.setattr(migrations, "_run_alembic_upgrade", lambda: "0039_some_recent")

    # Alembic-миграции на старте харнесса зовут fileConfig(disable_existing_
    # loggers) → сам логгер migrations приходит disabled=True и его INFO не
    # доходит до caplog. Ре-активируем, иначе проверка залогированной головы
    # ложно упадёт при верно отработавшем коде.
    migrations.logger.disabled = False

    with caplog.at_level(logging.INFO, logger=migrations.logger.name):
        migrations.run_migrations()

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "0039_some_recent" in messages
    assert "0009_balance_billing" not in messages


def test_run_migrations_falls_back_when_alembic_absent(monkeypatch):
    """None от _run_alembic_upgrade() → уходим в легаси-раннер."""
    monkeypatch.delenv("SKIP_MIGRATIONS", raising=False)

    monkeypatch.setattr(migrations, "create_engine", lambda *a, **kw: object())
    monkeypatch.setattr(migrations, "_backfill_from_legacy", lambda engine: None)
    monkeypatch.setattr(migrations, "_run_alembic_upgrade", lambda: None)

    called = {"legacy": False}

    def _fake_legacy():
        called["legacy"] = True

    monkeypatch.setattr(migrations, "_legacy_run", _fake_legacy)

    migrations.run_migrations()
    assert called["legacy"] is True
