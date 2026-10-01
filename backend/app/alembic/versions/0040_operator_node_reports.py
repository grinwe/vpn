"""Add operator_node_reports — crowdsourced (node, operator) block signal.

Revision ID: 0040_operator_node_reports
Revises: 0039_node_diagnostics_state

Phase 1 of operator-aware routing. Each row is one user "VPN не работает"
tap: the failed node + the auto-migrated target node + the operator the
user selected + the observed outcome (did the client reconnect on the
target within T_RECONNECT). The admin matrix aggregates these per
(node, operator) with recency-decay; choose_node is NOT affected yet
(advisory). See docs/operations/operator_routing_roadmap.md.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0040_operator_node_reports"
down_revision = "0039_node_diagnostics_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("operator_node_reports"):
        op.create_table(
            "operator_node_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "subscription_id",
                sa.Integer(),
                sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "device_id",
                sa.Integer(),
                sa.ForeignKey("devices.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("operator", sa.String(), nullable=True),
            sa.Column(
                "failed_node_id",
                sa.Integer(),
                sa.ForeignKey("vpn_nodes.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "target_node_id",
                sa.Integer(),
                sa.ForeignKey("vpn_nodes.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("target_access_username", sa.String(), nullable=True),
            sa.Column(
                "outcome",
                sa.String(),
                nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "reported_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
        )

    existing = {ix["name"] for ix in inspector.get_indexes("operator_node_reports")}
    for col in ("user_id", "operator", "failed_node_id", "target_node_id", "reported_at"):
        name = f"ix_operator_node_reports_{col}"
        if name not in existing:
            op.create_index(name, "operator_node_reports", [col])


def downgrade() -> None:
    for col in ("reported_at", "target_node_id", "failed_node_id", "operator", "user_id"):
        op.drop_index(
            f"ix_operator_node_reports_{col}", table_name="operator_node_reports"
        )
    op.drop_table("operator_node_reports")
