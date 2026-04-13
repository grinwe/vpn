"""Passive bot health-ping consent + dedup state (Phase C).

Revision ID: 0020_user_health_ping
Revises: 0019_node_traffic_samples

Adds two columns to ``users``:

  - ``health_ping_opt_out``: user pressed "не хочу видеть этот опрос".
    Worker tick skips them forever after the flag flips. Defaults to
    FALSE so existing users start in the opt-in cohort.
  - ``health_ping_last_at``: timestamp of the last delivered ping.
    Worker tick uses this for the per-user 24h debounce so we never
    spam the same person twice in a day even if Telegram retries.

The actual user-side responses ("works" / "doesn't work") land in
audit_logs as ``health_ping_response`` rows — we don't need a per-user
column for them because the detector reads the time-series directly.
"""
from __future__ import annotations

from alembic import op


revision = "0020_user_health_ping"
down_revision = "0019_node_traffic_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS health_ping_opt_out BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS health_ping_last_at TIMESTAMP
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS health_ping_last_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS health_ping_opt_out")
