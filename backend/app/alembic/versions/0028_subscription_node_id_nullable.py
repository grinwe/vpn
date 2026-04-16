"""Make subscriptions.node_id nullable + ON DELETE SET NULL.

Revision ID: 0028_subscription_node_id_nullable
Revises: 0027_relay_exit_link

Stage G.1 of ``docs/RELAY_ROADMAP.md``. The old non-nullable FK with
no cascade rule was making ``DELETE FROM vpn_nodes`` fail with an
``IntegrityError`` whenever any historical (terminated/expired)
``Subscription`` row pointed at the node — even after all active
subs had been migrated away. The admin-UI button surfaced a useless
"Не удалось удалить" and the only fix was a manual SQL UPDATE.

After this migration:
  * ``subscriptions.node_id`` is nullable.
  * The FK is recreated with ``ON DELETE SET NULL`` so deleting a
    node detaches historical subs instead of exploding.

Active/frozen subs are still guarded at the endpoint layer — the
409 there tells the admin to migrate first and the UI walks them
through it. Only terminated/expired rows end up with NULL node_id,
which is fine because they no longer serve any client traffic.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0028_subscription_node_id_nullable"
down_revision = "0027_relay_exit_link"
branch_labels = None
depends_on = None


def _subscriptions_node_fk_name(bind) -> str | None:
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys("subscriptions"):
        if fk.get("constrained_columns") == ["node_id"]:
            return fk.get("name")
    return None


def upgrade() -> None:
    bind = op.get_bind()

    fk_name = _subscriptions_node_fk_name(bind)
    if fk_name:
        op.drop_constraint(fk_name, "subscriptions", type_="foreignkey")

    op.alter_column(
        "subscriptions",
        "node_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    op.create_foreign_key(
        "fk_subscriptions_node_id",
        "subscriptions",
        "vpn_nodes",
        ["node_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Reverse is only safe if no rows have NULL node_id. We don't try
    # to backfill here — a downgrade in prod would already be a manual
    # recovery scenario, and re-tightening the FK without a target
    # node would be destructive.
    op.drop_constraint(
        "fk_subscriptions_node_id", "subscriptions", type_="foreignkey"
    )
    op.alter_column(
        "subscriptions",
        "node_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_foreign_key(
        None,
        "subscriptions",
        "vpn_nodes",
        ["node_id"],
        ["id"],
    )
