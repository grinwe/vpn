"""Stage 6 — multi-cloud fallback chain.

Revision ID: 0011_provider_fallback_chain
Revises: 0010_downscale

Adds ``ServerPool.autoscale_fallback_provider_ids`` (JSONB list of
CloudProvider ids) so the autoscaler can walk a chain of providers when
the primary one raises NodeSpawnError, instead of going straight to a
per-pool backoff. Defaults to NULL (= no fallbacks, behaves like before).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_provider_fallback_chain"
down_revision = "0010_downscale"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "server_pools",
        sa.Column(
            "autoscale_fallback_provider_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("server_pools", "autoscale_fallback_provider_ids")
