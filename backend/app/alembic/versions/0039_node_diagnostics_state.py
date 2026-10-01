"""Diagnostics overhaul — per-node toggles + incident state + exit telemetry.

Revision ID: 0039_node_diagnostics_state
Revises: 0038_node_user_bans

Splits the old single ``vpn_nodes.auto_diagnose_disabled_at`` mute into TWO
independent axes (operator asked for them separately):

  * ``diagnostics_disabled_at`` — hard-disable ALL diagnose tasks (auto +
    manual). Gated in the worker triggers, the manual endpoints and the
    orchestrator.
  * ``alerts_muted_until``      — silence admin Telegram alerts until a TTL
    ("замутить N часов" from the bot push). NULL or past = not muted.

Plus per-node diagnose-incident state so a down node is diagnosed ONCE per
outage instead of every health tick (anti-spam): ``diagnose_incident_open_at``
(NULL = no open incident, cleared on recovery), ``last_diagnosed_at``,
``diagnose_backoff_until`` (exponential opt-in), ``diagnose_follow_mode``
('once' default | 'exponential'), ``diagnose_acked_at`` ("вижу, работаю").

The same toggle + incident columns land on ``wg_exit_nodes`` (exits get their
own reachability probe now) plus ``last_probe_at`` / ``last_probe_status``
telemetry (WGExitNode had no health columns at all).

Backfill: existing muted nodes (``auto_diagnose_disabled_at`` set) get
``diagnostics_disabled_at`` set to the same value so current "don't diagnose"
behaviour is preserved. ``auto_diagnose_disabled_at`` is kept for now and
retired in a later pass once all reads move to the two new columns.

Idempotent (add-column guards) per the post-2026-05-19 migration policy.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_node_diagnostics_state"
down_revision = "0038_node_user_bans"
branch_labels = None
depends_on = None


# (column_name, sqlalchemy type factory) — same set reused on both tables
# except the exit-only probe telemetry.
# Both vpn_nodes and wg_exit_nodes get the same set — the reachability
# tick probes (and records ``last_probe_*`` on) both.
_COMMON_COLS = [
    ("diagnostics_disabled_at", lambda: sa.DateTime()),
    ("alerts_muted_until", lambda: sa.DateTime()),
    ("diagnose_incident_open_at", lambda: sa.DateTime()),
    ("last_diagnosed_at", lambda: sa.DateTime()),
    ("diagnose_backoff_until", lambda: sa.DateTime()),
    ("diagnose_follow_mode", lambda: sa.String()),
    ("diagnose_acked_at", lambda: sa.DateTime()),
    ("last_probe_at", lambda: sa.DateTime()),
    ("last_probe_status", lambda: sa.String()),
]


def _add_missing(inspector, table: str, cols) -> None:
    existing = {c["name"] for c in inspector.get_columns(table)}
    for name, type_factory in cols:
        if name not in existing:
            op.add_column(table, sa.Column(name, type_factory(), nullable=True))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _add_missing(inspector, "vpn_nodes", _COMMON_COLS)
    _add_missing(inspector, "wg_exit_nodes", _COMMON_COLS)

    # Preserve current behaviour: a node muted under the old combined flag
    # stays "do not diagnose" under the new dedicated toggle.
    vpn_cols = {c["name"] for c in inspector.get_columns("vpn_nodes")}
    if "auto_diagnose_disabled_at" in vpn_cols:
        op.execute(
            "UPDATE vpn_nodes "
            "SET diagnostics_disabled_at = auto_diagnose_disabled_at "
            "WHERE auto_diagnose_disabled_at IS NOT NULL "
            "AND diagnostics_disabled_at IS NULL"
        )


def downgrade() -> None:
    for name, _ in reversed(_COMMON_COLS):
        op.drop_column("wg_exit_nodes", name)
        op.drop_column("vpn_nodes", name)
