"""Billing V2: monthly billing replaces daily tick.

Revision ID: 0015_billing_v2
Revises: 0014_backfill_prepaid

Key changes:
  - ``expires_at`` becomes the authoritative ``paid_until`` timestamp.
    For balance-billed subs it may be stale (set at creation, never
    updated by the daily tick). We recalculate it from the remaining
    prepaid bucket: ``now + ceil(prepaid_kopecks / daily_rate_kopecks)``.
  - ``auto_renew`` flipped to True for all active subs (was False by
    default; V2 uses this as the user-visible toggle).
  - ``frozen_days_used`` reset to 0 and ``frozen_year`` to NULL — V2
    simplifies freeze to "1 per year, 7 days, tracked by has_frozen_this_year".
  - Add ``has_frozen_this_year`` boolean column for simpler freeze tracking.

Columns that become unused (kept for rollback safety, not dropped):
  - ``prepaid_kopecks`` — V2 charges full plan price at once
  - ``next_charge_at`` — V2 uses ``expires_at`` + hourly renewal check
"""
import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column


revision = "0015_billing_v2"
down_revision = "0014_backfill_prepaid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add has_frozen_this_year boolean for simplified freeze tracking.
    # has_column-guard: 0001 create_all() уже создаёт колонку из модели.
    if not has_column("subscriptions", "has_frozen_this_year"):
        op.add_column(
            "subscriptions",
            sa.Column(
                "has_frozen_this_year",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            ),
        )

    # Backfill: if they already froze this calendar year under V1, mark it.
    op.execute(
        """
        UPDATE subscriptions
        SET has_frozen_this_year = true
        WHERE frozen_days_used > 0
          AND frozen_year = EXTRACT(YEAR FROM NOW())::INTEGER;
        """
    )

    # 2. Recalculate expires_at for active balance-billed subs.
    #    runway = prepaid_kopecks / daily_rate_kopecks (days remaining in bucket)
    #    New expires_at = now + runway days.
    #    Only touch subs that have a prepaid bucket and daily_rate (balance-billed).
    op.execute(
        """
        UPDATE subscriptions AS s
        SET expires_at = NOW() + (
            GREATEST(
                CEIL(s.prepaid_kopecks::NUMERIC / p.daily_rate_kopecks),
                0
            ) || ' days'
        )::INTERVAL
        FROM plans AS p
        WHERE p.id = s.plan_id
          AND s.status IN ('active', 'frozen')
          AND p.daily_rate_kopecks IS NOT NULL
          AND p.daily_rate_kopecks > 0
          AND s.prepaid_kopecks > 0;
        """
    )

    # 3. Set auto_renew = True for active balance-billed subs.
    op.execute(
        """
        UPDATE subscriptions AS s
        SET auto_renew = true
        FROM plans AS p
        WHERE p.id = s.plan_id
          AND s.status = 'active'
          AND p.daily_rate_kopecks IS NOT NULL
          AND p.daily_rate_kopecks > 0;
        """
    )

    # 4. Zero out prepaid_kopecks and next_charge_at — V2 doesn't use them.
    #    Money was already debited from the wallet at purchase time, and
    #    expires_at now reflects the correct runway. Zeroing prepaid
    #    prevents the old daily tick (if it runs during deploy) from
    #    double-charging.
    op.execute(
        """
        UPDATE subscriptions
        SET prepaid_kopecks = 0,
            next_charge_at = NULL
        WHERE status IN ('active', 'frozen');
        """
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "has_frozen_this_year")
    # expires_at, auto_renew, prepaid_kopecks changes are not reversible.
    # A manual data fix would be needed to restore daily-tick state.
