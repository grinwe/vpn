"""Phase 1 reconciler: отмена provisioning-тасок.

Revision ID: 0042_provisioning_task_cancel
Revises: 0041_provisioning_bootstrap_coalesce

Добавляет:
  * enum-значение ``cancelled`` в provisioningtaskstatus;
  * колонку ``cancel_requested_at`` — оператор просит отмену; worker проверяет
    её перед стартом (skip→cancelled) и во время ansible-прогона (poll →
    SIGTERM процессу ansible-playbook).

ALTER TYPE ... ADD VALUE требует PG 12+ (там работает внутри транзакции
миграции; новое значение нельзя ИСПОЛЬЗОВАТЬ в той же транзакции — мы и не
используем). Idempotent: ADD VALUE IF NOT EXISTS + has_column.

См. docs/operations/provisioning_reconciler_epic.md (Phase 1).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.alembic._idempotent import has_column


revision = "0042_provisioning_task_cancel"
down_revision = "0041_provisioning_bootstrap_coalesce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE provisioningtaskstatus ADD VALUE IF NOT EXISTS 'cancelled'"
    )
    if not has_column("provisioning_tasks", "cancel_requested_at"):
        op.add_column(
            "provisioning_tasks",
            sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    # enum-значение не удаляем — PG не поддерживает DROP VALUE, а оставить
    # лишний label безвредно (downgrade — манёвр последней надежды).
    op.drop_column("provisioning_tasks", "cancel_requested_at")
