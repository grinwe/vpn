"""Add indexes on audit_logs (hot-path notifications queue + dashboards).

Revision ID: 0056_audit_logs_indexes
Revises: 0055_ad_links

#127 / #37 — audit_logs это не только журнал, но и hot-path: поллер
уведомлений бота (GET /api/notifications/pending) и sharing-block-детект
фильтруют по action + created_at, worker-тики и админ-дашборды — по
action/created_at. Без индексов каждый запрос — seq scan неограниченно
растущей таблицы (broadcast пишет строку на каждого получателя).
Индексы объявлены в модели AuditLog.__table_args__; эта миграция
приводит схему в соответствие. См. docs/data-model.md.
"""
from __future__ import annotations

from alembic import op

from app.alembic._idempotent import has_index

revision = "0056_audit_logs_indexes"
down_revision = "0055_ad_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_index("audit_logs", "ix_audit_logs_action_created_at"):
        op.create_index(
            "ix_audit_logs_action_created_at",
            "audit_logs",
            ["action", "created_at"],
        )
    if not has_index("audit_logs", "ix_audit_logs_created_at"):
        op.create_index(
            "ix_audit_logs_created_at",
            "audit_logs",
            ["created_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action_created_at", table_name="audit_logs")
