"""Add banned_at column to users for admin-level bans.

Revision ID: 0025_user_banned_at
Revises: 0024_node_suspect_since

Set by ``POST /api/users/{id}/ban``, cleared by ``.../unban``. Bot
middleware drops updates from users whose ``banned_at IS NOT NULL``
silently (no ACK — don't feed DDoS bots). Independent of subscription
status: banning a user doesn't touch their subs and vice versa.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime

from app.alembic._idempotent import has_column

revision = "0025_user_banned_at"
down_revision = "0024_node_suspect_since"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("users", "banned_at"):
        op.add_column("users", Column("banned_at", DateTime, nullable=True))


def downgrade() -> None:
    op.drop_column("users", "banned_at")
