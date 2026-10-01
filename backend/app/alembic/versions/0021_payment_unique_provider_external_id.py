"""Add UNIQUE(provider, external_id) on payments (audit #52).

Revision ID: 0021_payment_unique_provider_external_id
Revises: 0020_user_health_ping

Prevents duplicate payment records from webhook retries or checkout
double-clicks. The constraint is partial in effect: Postgres UNIQUE
treats NULLs as distinct, so rows with ``external_id IS NULL``
(manual payments, etc.) are not affected.

Before applying, the migration deduplicates any existing rows that
would violate the constraint — keeping the newest (highest id) row
per ``(provider, external_id)`` pair and deleting the rest.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0021_payment_unique_provider_external_id"
down_revision = "0020_user_health_ping"


def upgrade() -> None:
    # Delete older duplicates (keep highest id per provider+external_id).
    # Only needed for the initial migration — the constraint prevents
    # future duplicates. NULLs are excluded by the WHERE clause since
    # they're allowed to repeat.
    op.execute(
        text(
            """
            DELETE FROM payments
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM payments
                WHERE external_id IS NOT NULL
                GROUP BY provider, external_id
            )
            AND external_id IS NOT NULL
            """
        )
    )
    op.create_unique_constraint(
        "uq_payments_provider_external_id",
        "payments",
        ["provider", "external_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_payments_provider_external_id",
        "payments",
        type_="unique",
    )
