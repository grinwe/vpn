"""Add ops_plans — persisted dry-run ops-agent plans (confirm-binding foundation).

Revision ID: 0052_ops_plans
Revises: 0051_cloud_provider_vdsina_ru_enum

Планировщик /ops (services/agent/ops.py) пишет сюда каждый построенный план
целиком — шаги, params, оценку, content_hash. Зачем:
1. Полный аудит-след (раньше в audit_logs лежал только счётчик шагов).
2. Фундамент Phase 3: исполнение «по подтверждению» обязано ссылаться на
   сохранённый план по id + content_hash, чтобы исполнялось ровно то, что
   оператор подтвердил (никаких params от клиента, никакого переплана на confirm).
Пока ничего не исполняется — это только запись. См.
docs/operations/ops_agent_security_epic.md.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0052_ops_plans"
down_revision = "0051_cloud_provider_vdsina_ru_enum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("ops_plans"):
        op.create_table(
            "ops_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("actor", sa.String(), nullable=False),
            sa.Column("command", sa.Text(), nullable=False),
            sa.Column("model", sa.String(), nullable=True),
            sa.Column("plan", JSONB(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("feasible", sa.Boolean(), nullable=True),
            sa.Column("needs_confirmation", sa.Boolean(), nullable=True),
            sa.Column(
                "status",
                sa.String(),
                nullable=False,
                server_default="proposed",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
        )

    existing = {ix["name"] for ix in inspector.get_indexes("ops_plans")}
    if "ix_ops_plans_actor" not in existing:
        op.create_index("ix_ops_plans_actor", "ops_plans", ["actor"])
    if "ix_ops_plans_created_at" not in existing:
        op.create_index("ix_ops_plans_created_at", "ops_plans", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ops_plans_created_at", table_name="ops_plans")
    op.drop_index("ix_ops_plans_actor", table_name="ops_plans")
    op.drop_table("ops_plans")
