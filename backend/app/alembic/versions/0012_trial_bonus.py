"""Free trial bonus — one-time 150₽ credit per user.

Revision ID: 0012_trial_bonus
Revises: 0011_provider_fallback_chain

Adds two nullable timestamps to ``users``:
- ``trial_activated_at`` — when the user claimed their one-time trial
  bonus via POST /api/trial/activate. NULL ⇒ trial still available.
- ``trial_expires_at``   — activated_at + TRIAL_DURATION_DAYS. Worker
  tick reads this to send the 3-day warning and, at expiry, clawback
  the unspent bonus iff the user never made a real topup.
"""
from alembic import op


revision = "0012_trial_bonus"
down_revision = "0011_provider_fallback_chain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_activated_at TIMESTAMP"
    )
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_expires_at TIMESTAMP"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS trial_expires_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS trial_activated_at")
