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
from sqlalchemy.dialects import postgresql


revision = "0026_wg_exit_nodes"
down_revision = "0025_user_banned_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent enum creation — mirrors the 0008_warmpool pattern so a
    # partial prior run that created the type but never marked the migration
    # applied won't crash the replay with DuplicateObject.
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='wgexitnodestatus') "
        "THEN CREATE TYPE wgexitnodestatus AS ENUM "
        "('registering','active','error','disabled'); "
        "END IF; END $$;"
    )

    wg_exit_node_status = postgresql.ENUM(
        "registering",
        "active",
        "error",
        "disabled",
        name="wgexitnodestatus",
        create_type=False,
    )

    bind = op.get_bind()
    if sa.inspect(bind).has_table("wg_exit_nodes"):
        return

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
            wg_exit_node_status,
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
    op.execute("DROP TYPE IF EXISTS wgexitnodestatus")
