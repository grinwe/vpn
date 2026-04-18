"""Add relay_exit_links for relay↔exit N:1 attachment.

Revision ID: 0027_relay_exit_link
Revises: 0026_wg_exit_nodes

Stage C of the relay architecture (``docs/RELAY_ROADMAP.md``).
A relay ``VPNNode`` attaches to exactly one ``WGExitNode`` at a time;
the link holds the per-client WG keypair and tunnel address.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027_relay_exit_link"
down_revision = "0026_wg_exit_nodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("relay_exit_links"):
        op.create_table(
            "relay_exit_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "relay_node_id",
                sa.Integer(),
                sa.ForeignKey("vpn_nodes.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "exit_id",
                sa.Integer(),
                sa.ForeignKey("wg_exit_nodes.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("wg_client_private_key_enc", sa.Text(), nullable=False),
            sa.Column("wg_client_public_key", sa.String(), nullable=False),
            sa.Column("wg_client_address_v4", sa.String(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    existing_indexes = {
        ix["name"] for ix in inspector.get_indexes("relay_exit_links")
    }
    if "ix_relay_exit_links_exit_id" not in existing_indexes:
        op.create_index(
            "ix_relay_exit_links_exit_id",
            "relay_exit_links",
            ["exit_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_relay_exit_links_exit_id", table_name="relay_exit_links")
    op.drop_table("relay_exit_links")
