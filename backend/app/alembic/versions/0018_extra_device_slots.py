"""Add Subscription.extra_device_slots counter.

Revision ID: 0018_extra_device_slots
Revises: 0017_fix_enum_values

Stores the number of device slots the user bought *above* the plan's
bundled ``max_devices``. Each slot is billed at
``EXTRA_DEVICE_MONTHLY_KOPECKS`` per period on top of ``plan.price`` in
``balance.renew_subscription``.

Model:
  - Bumped by ``webapp_add_device`` when the user exceeds the bundle.
  - Removing a device does NOT decrement — paid slots are sticky so
    renewals stay predictable and no per-remove refund ambiguity.
  - ``balance.change_plan`` resets to 0 (new plan, new bundle).

Backfill is a no-op — any currently-over-bundle subs get 0 slots, so
the next renewal is (briefly) cheaper than the month they just paid
for. Acceptable since the "extra device" feature did not exist before
this migration and the only way to be over the bundle pre-V2 was an
admin override — those should not start billing the user retroactively.
"""
import sqlalchemy as sa
from alembic import op


revision = "0018_extra_device_slots"
down_revision = "0017_fix_enum_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "extra_device_slots",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "extra_device_slots")
