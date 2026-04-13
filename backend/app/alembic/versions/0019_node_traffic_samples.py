"""Passive xray traffic stats baseline (Phase B).

Revision ID: 0019_node_traffic_samples
Revises: 0018_extra_device_slots

Adds ``node_traffic_samples`` — a periodic snapshot of per-node uplink /
downlink bytes and active-user counts pulled by the worker from xray's
gRPC StatsService on the node loopback. Each row is one collector tick
for one node; the ``details`` JSONB carries the raw per-protocol
breakdown so we can later replay it without re-reading the node.

Why a separate table (vs reusing health_probes): probe rows are 1:1
with active-probe attempts (TCP/TLS handshake from a vantage point);
this table is 1:1 with passive observations of the node's own internal
counters. Two different signals, two different cardinalities — folding
them together would leak the schema and complicate the eventual
detector that compares the two.

The detector that consumes these rows lives in a follow-up — for the
MVP we just collect the baseline.
"""
from __future__ import annotations

from alembic import op


revision = "0019_node_traffic_samples"
down_revision = "0018_extra_device_slots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS node_traffic_samples (
            id SERIAL PRIMARY KEY,
            node_id INTEGER NOT NULL REFERENCES vpn_nodes(id) ON DELETE CASCADE,
            observed_at TIMESTAMP NOT NULL DEFAULT NOW(),
            interval_seconds INTEGER NOT NULL DEFAULT 0,
            uplink_bytes BIGINT NOT NULL DEFAULT 0,
            downlink_bytes BIGINT NOT NULL DEFAULT 0,
            active_users INTEGER NOT NULL DEFAULT 0,
            details JSONB
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_node_traffic_samples_node_observed
        ON node_traffic_samples (node_id, observed_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_node_traffic_samples_node_observed")
    op.execute("DROP TABLE IF EXISTS node_traffic_samples")
