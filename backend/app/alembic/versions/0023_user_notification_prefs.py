"""Add per-user notification preference columns.

Revision ID: 0023_user_notification_prefs
Revises: 0022_device_sub_token

Two new boolean columns on ``users``:
  - notify_renewals  (default True) — renewal/expiry reminders + low balance
  - notify_migrations (default True) — "VPN-сервер был перемещён" alerts

health_ping_opt_out already exists (migration 0020).
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import Boolean, Column

revision = "0023_user_notification_prefs"
down_revision = "0022_device_sub_token"


def upgrade() -> None:
    op.add_column(
        "users",
        Column("notify_renewals", Boolean, nullable=False, server_default="true"),
    )
    op.add_column(
        "users",
        Column("notify_migrations", Boolean, nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("users", "notify_migrations")
    op.drop_column("users", "notify_renewals")
