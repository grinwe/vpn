"""relay_exit_links.auto_diagnose_disabled_at — mute-флаг для проблемных линков.

Revision ID: 0035_relay_link_auto_diagnose_disabled
Revises: 0034_provisioning_task_batch_id

Контекст: смарт-диагностика (worker.py:_auto_diagnose_stale_links) ENQUEUE'ит
diagnose-task раз в RELAY_LINK_HEALTH_INTERVAL для каждого unhealthy линка.
На известных проблемных линках (нода ещё bootstrap'ится, ждём ручного fix'а,
exit поднимается) этот auto-trigger создаёт постоянный шум в /admin/tasks
и съедает worker concurrency.

Поле `auto_diagnose_disabled_at`:
  NULL — auto-trigger работает (default)
  timestamp — оператор явно выключил, дата выключения для аудита

Управление — POST /exits/links/{id}/auto-diagnose/disable | enable.
Worker фильтрует links по `auto_diagnose_disabled_at IS NULL`.

Idempotent через has_column.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.alembic._idempotent import has_column


revision = "0035_relay_link_auto_diagnose_disabled"
down_revision = "0034_provisioning_task_batch_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("relay_exit_links", "auto_diagnose_disabled_at"):
        op.add_column(
            "relay_exit_links",
            sa.Column(
                "auto_diagnose_disabled_at",
                sa.DateTime(),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("relay_exit_links", "auto_diagnose_disabled_at")
