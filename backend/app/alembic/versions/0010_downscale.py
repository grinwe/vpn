"""Pool downscale (stage 5).

Revision ID: 0010_downscale
Revises: 0009_balance_billing

Adds the ``VPNNodeStatus.draining`` enum value plus two ServerPool knobs
(``autoscale_low_watermark``, ``autoscale_min_nodes``) that drive the
new drain tick. No data backfill — existing pools default to NULL on
both columns and inherit the env-level defaults at evaluation time.
"""
from alembic import op
import sqlalchemy as sa


revision = "0010_downscale"
down_revision = "0009_balance_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres enum extension — must run with autocommit, IF NOT EXISTS
    # makes the migration safe to re-run on a partially-applied DB.
    op.execute(
        "ALTER TYPE vpnnodestatus ADD VALUE IF NOT EXISTS 'draining'"
    )

    op.add_column(
        "server_pools",
        sa.Column("autoscale_low_watermark", sa.Numeric(4, 3), nullable=True),
    )
    op.add_column(
        "server_pools",
        sa.Column("autoscale_min_nodes", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    # PG has no DROP VALUE for enums; leaving 'draining' in the type is
    # harmless once the application stops emitting it. Any rows still
    # carrying that status would block the downgrade — operator must
    # flip them to 'disabled' first.
    op.drop_column("server_pools", "autoscale_min_nodes")
    op.drop_column("server_pools", "autoscale_low_watermark")
