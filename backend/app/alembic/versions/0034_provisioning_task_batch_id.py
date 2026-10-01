"""provisioning_tasks.batch_id — группировка task'ов по одному batch-attach.

Revision ID: 0034_provisioning_task_batch_id
Revises: 0033_broadcasts

Контекст: новый flow batch-attach (1 relay → N exits через POST
/admin/exits/batch-attach) создаёт N независимых ProvisioningTask'ов,
по одной на каждый attach. Чтобы UI мог отрисовать их одной группой с
прогрессом N/M и отдельным per-task retry, проставляем общий UUID на
все task'и одного батча. Колонка nullable: для одиночных attach'ей
(старый flow на /exits drill-down + AttachRelayForm с N-1 POST'ами)
остаётся NULL.

Никакой логики aggregation в orchestrator'е (см. обсуждение
batch_id vs parent_task_id): UI считает прогресс по children'ам сам,
SELECT WHERE batch_id=X с group by status.

Idempotent через has_column/has_index — на fresh DB модель уже
содержит колонку через create_all, на инкрементальной — добавляем.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from app.alembic._idempotent import has_column, has_index


revision = "0034_provisioning_task_batch_id"
down_revision = "0033_broadcasts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("provisioning_tasks", "batch_id"):
        op.add_column(
            "provisioning_tasks",
            sa.Column("batch_id", UUID(as_uuid=True), nullable=True),
        )

    if not has_index("provisioning_tasks", "ix_provisioning_tasks_batch_id"):
        op.create_index(
            "ix_provisioning_tasks_batch_id",
            "provisioning_tasks",
            ["batch_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_provisioning_tasks_batch_id", table_name="provisioning_tasks"
    )
    op.drop_column("provisioning_tasks", "batch_id")
