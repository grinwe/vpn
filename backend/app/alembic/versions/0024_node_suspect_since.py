"""Add suspect_since column to vpn_nodes for Phase D traffic-drop detector.

Revision ID: 0024_node_suspect_since
Revises: 0023_user_notification_prefs

Set when active_users drops from ≥MIN to 0 between two traffic_stats
ticks; cleared on the next tick after confirmation or false-alarm.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime

revision = "0024_node_suspect_since"
down_revision = "0023_user_notification_prefs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vpn_nodes", Column("suspect_since", DateTime, nullable=True))


def downgrade() -> None:
    op.drop_column("vpn_nodes", "suspect_since")
