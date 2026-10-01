"""Add node_user_bans — per-node user ban list.

Revision ID: 0038_node_user_bans
Revises: 0037_device_client_id_hmac

Per-node бан юзера: на какие ноды авто-выбор (``choose_node`` через
``exclude_node_ids``) НЕ должен селить данного юзера. Ортогонально
глобальному ``users.banned_at`` (бан на уровне бота). Заполняется при
«обновлении подписки» (миграция на свободный сервер авто-банит старую
ноду) и вручную через admin-эндпоинты. Бан per-(user, node).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0038_node_user_bans"
down_revision = "0037_device_client_id_hmac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("node_user_bans"):
        op.create_table(
            "node_user_bans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "node_id",
                sa.Integer(),
                sa.ForeignKey("vpn_nodes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("user_id", "node_id", name="uq_node_user_ban"),
        )

    existing_indexes = {
        ix["name"] for ix in inspector.get_indexes("node_user_bans")
    }
    if "ix_node_user_bans_user_id" not in existing_indexes:
        op.create_index(
            "ix_node_user_bans_user_id",
            "node_user_bans",
            ["user_id"],
        )
    if "ix_node_user_bans_node_id" not in existing_indexes:
        op.create_index(
            "ix_node_user_bans_node_id",
            "node_user_bans",
            ["node_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_node_user_bans_node_id", table_name="node_user_bans")
    op.drop_index("ix_node_user_bans_user_id", table_name="node_user_bans")
    op.drop_table("node_user_bans")
