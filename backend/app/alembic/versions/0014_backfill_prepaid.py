"""Backfill ``subscriptions.prepaid_kopecks`` for pre-existing subs.

Revision ID: 0014_backfill_prepaid
Revises: 0013_sub_prepaid

0013 added the column with DEFAULT 0, which is correct for new subs
(they get seeded by ``balance.activate_prepaid`` at purchase time) but
catastrophic for subs that were active *before* the rework — their
prepaid bucket is empty, so the next charge tick would immediately
flip them to expired even though the user still has runway at the
*daily* rate they were previously being charged.

Backfill strategy: for every sub whose status is active/frozen AND
``prepaid_kopecks = 0``, seed the bucket with
``daily_rate_kopecks × ceil(days_to_expires_at)``. This matches the
sub's existing ``expires_at`` window — users get exactly the runway
they already saw advertised on their Home card, no more, no less.

Idempotent: gated on ``prepaid_kopecks = 0`` so re-running this
migration (or running the whole chain on a fresh DB) is a no-op for
subs that were already seeded by the purchase flow.
"""
from alembic import op


revision = "0014_backfill_prepaid"
down_revision = "0013_sub_prepaid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE subscriptions AS s
        SET prepaid_kopecks = GREATEST(
            COALESCE(p.daily_rate_kopecks, 0) * GREATEST(
                CEIL(EXTRACT(EPOCH FROM (s.expires_at - NOW())) / 86400)::INTEGER,
                0
            ),
            0
        )
        FROM plans AS p
        WHERE p.id = s.plan_id
          AND s.status IN ('active', 'frozen')
          AND s.prepaid_kopecks = 0
          AND s.expires_at IS NOT NULL
          AND p.daily_rate_kopecks IS NOT NULL
          AND p.daily_rate_kopecks > 0;
        """
    )


def downgrade() -> None:
    # Not reversible — the funds were conceptually "already there" in
    # the wallet-based model; we can't unsee that. Downgrade is a no-op.
    pass
