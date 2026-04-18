"""Per-subscription prepaid bucket.

Revision ID: 0013_sub_prepaid
Revises: 0012_trial_bonus

Adds ``subscriptions.prepaid_kopecks`` — the committed pre-payment for
a subscription's billing window. On activate we debit the full plan
price from ``users.balance_kopecks`` into this bucket. ``charge_subscription``
then spends from here instead of the user's wallet. On manual revoke
we refund the leftover back to the user as ``kind=refund``.

Rationale: the old flow let a user top up ₽1/day and run the cheapest
plan forever. Committing the full plan price upfront locks the user in
for the plan window; freeze still works because the tick stops.
"""
from alembic import op


revision = "0013_sub_prepaid"
down_revision = "0012_trial_bonus"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscriptions "
        "ADD COLUMN IF NOT EXISTS prepaid_kopecks INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS prepaid_kopecks")
