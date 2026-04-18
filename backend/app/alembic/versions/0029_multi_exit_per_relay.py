"""Schema for multi-exit-per-relay (N:N between relay and exit).

Revision ID: 0029_multi_exit_per_relay
Revises: 0028_subscription_node_id_nullable

Stage G.3 of ``docs/RELAY_ROADMAP.md``. Relaxes the relay↔exit
attachment from 1:1 to N:N so one relay can tunnel to several
exits and xray routing can split clients across them. Schema-only;
behavior (attach guards, allocation, xray fan-out) arrives in
G.4–G.7.

Shape changes:
  * ``relay_exit_links`` — drop the UNIQUE on ``relay_node_id``
    alone; add ``wg_interface_name`` (``wg0`` for legacy rows) and
    a composite UNIQUE on ``(relay_node_id, wg_interface_name)``
    so a relay can have multiple links as long as each tunnel
    gets its own kernel interface.
  * ``credentials.exit_id`` — nullable FK to ``wg_exit_nodes.id``
    with ``ON DELETE SET NULL``. Populated by provisioning in G.4
    to pin a given credential (and therefore its xray user UUID)
    to one exit; xray routing in G.6 reads this to build the
    ``user → outboundTag`` table.

Nothing is lost in the upgrade: existing 1:1 links roll forward
unchanged (their new ``wg_interface_name`` is ``wg0``) and
existing credentials stay on the legacy "single exit per relay"
path because ``exit_id`` starts NULL.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0029_multi_exit_per_relay"
down_revision = "0028_subscription_node_id_nullable"
branch_labels = None
depends_on = None


def _rel_unique_constraint_name(bind) -> str | None:
    """Find the existing UNIQUE(relay_node_id) constraint on relay_exit_links."""
    inspector = sa.inspect(bind)
    for uc in inspector.get_unique_constraints("relay_exit_links"):
        cols = uc.get("column_names") or []
        if cols == ["relay_node_id"]:
            return uc.get("name")
    # Some Postgres deployments store the unique as an INDEX instead of
    # a CONSTRAINT — cover that path too.
    return None


def _rel_unique_index_name(bind) -> str | None:
    inspector = sa.inspect(bind)
    for ix in inspector.get_indexes("relay_exit_links"):
        if ix.get("unique") and ix.get("column_names") == ["relay_node_id"]:
            return ix.get("name")
    return None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    rel_cols = {c["name"] for c in inspector.get_columns("relay_exit_links")}
    if "wg_interface_name" not in rel_cols:
        op.add_column(
            "relay_exit_links",
            sa.Column(
                "wg_interface_name",
                sa.String(length=16),
                nullable=False,
                server_default="wg0",
            ),
        )

    uc_name = _rel_unique_constraint_name(bind)
    if uc_name:
        op.drop_constraint(uc_name, "relay_exit_links", type_="unique")
    ix_name = _rel_unique_index_name(bind)
    if ix_name:
        op.drop_index(ix_name, table_name="relay_exit_links")

    existing_ucs = {
        uc.get("name") for uc in inspector.get_unique_constraints("relay_exit_links")
    }
    if "uq_relay_exit_links_relay_iface" not in existing_ucs:
        op.create_unique_constraint(
            "uq_relay_exit_links_relay_iface",
            "relay_exit_links",
            ["relay_node_id", "wg_interface_name"],
        )

    cred_cols = {c["name"] for c in inspector.get_columns("credentials")}
    if "exit_id" not in cred_cols:
        op.add_column(
            "credentials",
            sa.Column("exit_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_credentials_exit_id",
            "credentials",
            "wg_exit_nodes",
            ["exit_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            "ix_credentials_exit_id", "credentials", ["exit_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_credentials_exit_id", table_name="credentials")
    op.drop_constraint(
        "fk_credentials_exit_id", "credentials", type_="foreignkey"
    )
    op.drop_column("credentials", "exit_id")

    op.drop_constraint(
        "uq_relay_exit_links_relay_iface",
        "relay_exit_links",
        type_="unique",
    )
    # Downgrade re-tightens to the old 1:1 shape. Only safe if the
    # data hasn't already grown to N:N — same caveat as 0028.
    op.create_unique_constraint(
        None, "relay_exit_links", ["relay_node_id"]
    )
    op.drop_column("relay_exit_links", "wg_interface_name")
