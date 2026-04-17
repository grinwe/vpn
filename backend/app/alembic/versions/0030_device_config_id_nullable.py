"""Make devices.config_id nullable + ON DELETE SET NULL.

Revision ID: 0030_device_config_id_nullable
Revises: 0029_multi_exit_per_relay

Same shape as 0028_subscription_node_id_nullable, applied to Device.
A revoked ``Device`` row is deliberately kept after revocation so its
``sub_token`` keeps resolving in ``/api/sub/{token}`` (see the comment
block in ``services/provisioning.py`` on the ``revoke`` branch). With
the old non-nullable ``config_id`` FK this meant that every disabled
Device on a doomed node would block ``DELETE FROM vpn_nodes`` — the
endpoint already cleaned up Subscriptions and Credentials but Devices
were untouched, so the cascade from vpn_nodes → vpn_configs tripped
SQLAlchemy's orphan-nullify, which failed the NOT NULL constraint:

    null value in column "config_id" of relation "devices"
    violates not-null constraint

Fix: drop NOT NULL, recreate the FK with ON DELETE SET NULL. The
cascade chain is now: delete node → delete vpn_configs (CASCADE) →
devices.config_id becomes NULL. The Device row survives — its
``sub_token`` alias keeps working, audit history stays intact.

Existing readers (see ``device.config.node if device.config else
device.subscription.node`` in provisioning.py) already handle the
None case because bundle-orphan paths hit it too.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0030_device_config_id_nullable"
down_revision = "0029_multi_exit_per_relay"
branch_labels = None
depends_on = None


def _devices_config_fk_name(bind) -> str | None:
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys("devices"):
        if fk.get("constrained_columns") == ["config_id"]:
            return fk.get("name")
    return None


def upgrade() -> None:
    bind = op.get_bind()

    fk_name = _devices_config_fk_name(bind)
    if fk_name:
        op.drop_constraint(fk_name, "devices", type_="foreignkey")

    op.alter_column(
        "devices",
        "config_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    op.create_foreign_key(
        "fk_devices_config_id",
        "devices",
        "vpn_configs",
        ["config_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Mirrors 0028 — downgrade is only safe if no rows have NULL
    # config_id; we don't attempt a backfill. A prod downgrade would
    # already be a manual recovery.
    op.drop_constraint(
        "fk_devices_config_id", "devices", type_="foreignkey"
    )
    op.alter_column(
        "devices",
        "config_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_foreign_key(
        None,
        "devices",
        "vpn_configs",
        ["config_id"],
        ["id"],
    )
