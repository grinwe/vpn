"""Broadcast table для админских рассылок юзерам.

Revision ID: 0033_broadcasts
Revises: 0032_relay_link_health

Новая фича — админка шлёт кастомное сообщение всем юзерам или части
по фильтру (active/ids). Рассылка отрабатывает в worker-тике
`run_broadcast_dispatch_tick` батчами, курсор по id фиксируется в
`last_user_id_cursor`. На каждого юзера пишется AuditLog-строка с
`action=admin_broadcast`, бот доставляет как обычный push.

Статусы:
  queued     — создан через API, ещё не начат
  sending    — тик в процессе, видно прогресс
  completed  — все юзеры обработаны
  cancelled  — админ отменил mid-flight
  failed     — dispatch-тик поймал фатальную ошибку (не per-user)

Индекс по status для быстрого забора следующего в работу.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0033_broadcasts"
down_revision = "0032_relay_link_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status_enum = sa.Enum(
        "queued",
        "sending",
        "completed",
        "cancelled",
        "failed",
        name="broadcast_status",
    )
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("target_filter", JSONB(), nullable=False),
        sa.Column(
            "status",
            status_enum,
            nullable=False,
            server_default="queued",
        ),
        sa.Column("total_recipients", sa.Integer(), nullable=True),
        sa.Column(
            "sent_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "failed_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "last_user_id_cursor",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_reason", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_broadcasts_status", "broadcasts", ["status"], unique=False
    )
    op.create_index(
        "ix_broadcasts_created_at", "broadcasts", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_broadcasts_created_at", table_name="broadcasts")
    op.drop_index("ix_broadcasts_status", table_name="broadcasts")
    op.drop_table("broadcasts")
    sa.Enum(name="broadcast_status").drop(op.get_bind(), checkfirst=True)
