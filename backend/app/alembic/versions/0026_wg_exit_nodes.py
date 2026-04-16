"""Add wg_exit_nodes table for the relay architecture.

Revision ID: 0026_wg_exit_nodes
Revises: 0025_user_banned_at

Introduces foreign exit nodes as a first-class entity (see
``docs/RELAY_ROADMAP.md`` stage B). Exits hold WireGuard server keys
and are referenced by relay jump nodes through a later M:N table.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0026_wg_exit_nodes"
down_revision = "0025_user_banned_at"
branch_labels = None
depends_on = None


WG_EXIT_NODE_STATUS = sa.Enum(
    "registering",
    "active",
    "error",
    "disabled",
    name="wgexitnodestatus",
)


def upgrade() -> None:
    WG_EXIT_NODE_STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "wg_exit_nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("host", sa.String(), nullable=False),
        sa.Column("ssh_port", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("wg_port", sa.Integer(), nullable=False, server_default="51820"),
        sa.Column(
            "wg_address_v4",
            sa.String(),
            nullable=False,
            server_default="10.77.0.1/24",
        ),
        sa.Column("wg_public_key", sa.String(), nullable=True),
        sa.Column("wg_private_key_enc", sa.Text(), nullable=True),
        sa.Column(
            "provider_id",
            sa.Integer(),
            sa.ForeignKey("cloud_providers.id"),
            nullable=True,
        ),
        sa.Column("provider_external_id", sa.String(), nullable=True),
        sa.Column("provider_region", sa.String(), nullable=True),
        sa.Column(
            "status",
            WG_EXIT_NODE_STATUS,
            nullable=False,
            server_default="registering",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("wg_exit_nodes")
    WG_EXIT_NODE_STATUS.drop(op.get_bind(), checkfirst=True)
