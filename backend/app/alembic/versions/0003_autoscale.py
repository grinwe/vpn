"""Pool autoscale configuration columns.

Revision ID: 0003_autoscale
Revises: 0002_health_and_cloud
Create Date: 2026-04-06
"""
from __future__ import annotations

from alembic import op

revision = "0003_autoscale"
down_revision = "0002_health_and_cloud"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for clause in [
        "ADD COLUMN IF NOT EXISTS autoscale_enabled BOOLEAN DEFAULT FALSE",
        "ADD COLUMN IF NOT EXISTS autoscale_provider_id INTEGER REFERENCES cloud_providers(id)",
        "ADD COLUMN IF NOT EXISTS autoscale_region VARCHAR",
        "ADD COLUMN IF NOT EXISTS autoscale_plan VARCHAR",
        "ADD COLUMN IF NOT EXISTS autoscale_image VARCHAR",
        "ADD COLUMN IF NOT EXISTS autoscale_high_watermark NUMERIC(4,3)",
        "ADD COLUMN IF NOT EXISTS autoscale_max_nodes INTEGER",
    ]:
        op.execute(f"ALTER TABLE server_pools {clause}")


def downgrade() -> None:
    for col in [
        "autoscale_enabled",
        "autoscale_provider_id",
        "autoscale_region",
        "autoscale_plan",
        "autoscale_image",
        "autoscale_high_watermark",
        "autoscale_max_nodes",
    ]:
        op.execute(f"ALTER TABLE server_pools DROP COLUMN IF EXISTS {col}")
