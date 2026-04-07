"""Make Payment.subscription_id nullable and add invoice_id link.

Revision ID: 0004_payment_nullable_subscription
Revises: 0003_autoscale
Create Date: 2026-04-06

A Payment is created at checkout time, *before* provisioning runs, so for
``new_subscription`` invoices there is no Subscription yet. Modelling
Payment as NOT NULL on subscription_id forced us to defer the Payment row
to the webhook handler, which made reconciliation harder. This revision
relaxes the constraint and adds a direct link to the invoice so each
provider callback can be traced back to the checkout unambiguously.
"""
from __future__ import annotations

from alembic import op

revision = "0004_payment_nullable_subscription"
down_revision = "0003_autoscale"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE payments ALTER COLUMN subscription_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS invoice_id INTEGER "
        "REFERENCES invoices(id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_payments_invoice_id ON payments(invoice_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_payments_invoice_id")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS invoice_id")
    # Intentionally do NOT re-tighten subscription_id to NOT NULL: existing
    # rows may legitimately have NULL after this revision ran.
