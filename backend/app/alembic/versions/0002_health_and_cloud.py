"""Health probing + cloud provider metadata.

Revision ID: 0002_health_and_cloud
Revises: 0001_initial
Create Date: 2026-04-06

Adds the ``cloud_providers`` and ``health_probes`` tables and extends
``vpn_nodes`` with capacity, health and provider-linkage columns. ``IF NOT
EXISTS`` guards are used because some environments may have had the legacy
hand-rolled migration runner apply parts of this schema already.
"""
from __future__ import annotations

from alembic import op

revision = "0002_health_and_cloud"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cloud_providers (
            id SERIAL PRIMARY KEY,
            name VARCHAR UNIQUE NOT NULL,
            kind VARCHAR NOT NULL,
            api_token_enc TEXT,
            default_image VARCHAR,
            ssh_key_ids JSONB,
            default_region VARCHAR,
            default_plan VARCHAR,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS health_probes (
            id SERIAL PRIMARY KEY,
            node_id INTEGER NOT NULL REFERENCES vpn_nodes(id) ON DELETE CASCADE,
            source_region VARCHAR NOT NULL,
            source_kind VARCHAR,
            result VARCHAR NOT NULL,
            latency_ms INTEGER,
            observed_at TIMESTAMP DEFAULT NOW(),
            details JSONB
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_health_probes_node_id ON health_probes(node_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_health_probes_observed_at ON health_probes(observed_at)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_health_probes_source_region ON health_probes(source_region)"
    )

    alters = [
        "ADD COLUMN IF NOT EXISTS max_users INTEGER",
        "ADD COLUMN IF NOT EXISTS max_bandwidth_mbps INTEGER",
        "ADD COLUMN IF NOT EXISTS health_score INTEGER DEFAULT 100",
        "ADD COLUMN IF NOT EXISTS last_health_check_at TIMESTAMP",
        "ADD COLUMN IF NOT EXISTS blocked_regions JSONB",
        "ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMP",
        "ADD COLUMN IF NOT EXISTS provider_id INTEGER REFERENCES cloud_providers(id)",
        "ADD COLUMN IF NOT EXISTS provider_external_id VARCHAR",
        "ADD COLUMN IF NOT EXISTS provider_region VARCHAR",
        "ADD COLUMN IF NOT EXISTS provider_plan VARCHAR",
        "ADD COLUMN IF NOT EXISTS monthly_cost NUMERIC(10,2)",
    ]
    for clause in alters:
        op.execute(f"ALTER TABLE vpn_nodes {clause}")


def downgrade() -> None:
    cols = [
        "max_users",
        "max_bandwidth_mbps",
        "health_score",
        "last_health_check_at",
        "blocked_regions",
        "cooldown_until",
        "provider_id",
        "provider_external_id",
        "provider_region",
        "provider_plan",
        "monthly_cost",
    ]
    for col in cols:
        op.execute(f"ALTER TABLE vpn_nodes DROP COLUMN IF EXISTS {col}")
    op.execute("DROP TABLE IF EXISTS health_probes")
    op.execute("DROP TABLE IF EXISTS cloud_providers")
