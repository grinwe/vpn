"""Add ops_plans.execution — per-step execution result (Phase 3 executor).

Revision ID: 0053_ops_plans_execution
Revises: 0052_ops_plans

Исполнитель ops-плана (services/agent/ops_execution.py::execute_plan) пишет сюда
результат: фазу (validate/preflight/done), per-step статусы, созданные node-id,
итоговую стоимость. Для отчёта оператору. NULL пока план не исполнялся.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0053_ops_plans_execution"
down_revision = "0052_ops_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("ops_plans")}
    if "execution" not in cols:
        op.add_column("ops_plans", sa.Column("execution", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("ops_plans", "execution")
