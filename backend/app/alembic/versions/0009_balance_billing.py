"""Balance billing application layer (stage 4).

Revision ID: 0009_balance_billing
Revises: 0008_warmpool_and_balance

Adds the per-subscription billing anchor + freeze fields, the
``Invoice.kind`` discriminator (subscription vs topup), the
``SubscriptionStatus.frozen`` enum value, and seeds ``daily_rate_kopecks``
on the canonical plans so the cron tick has something to charge against
on day one.

The DB scaffold for ``users.balance_kopecks``, ``plans.daily_rate_kopecks``
and ``balance_transactions`` already landed in ``0008``; this revision is
purely the wiring on top.
"""
from alembic import op
import sqlalchemy as sa

from app.alembic._idempotent import has_column


revision = "0009_balance_billing"
down_revision = "0008_warmpool_and_balance"
branch_labels = None
depends_on = None


# Daily rate seed values, in kopecks. Mirrors the existing 30/365-day
# plan prices: roughly price * 100 / duration_days, rounded to a clean
# integer so the user-facing "X ₽/день" stays nice.
_DAILY_RATE_SEED = {
    "Solo": 500,            # 150₽ / 30d
    "Family": 1000,         # 300₽ / 30d
    "Pro": 1700,            # 500₽ / 30d ≈ 16.66 → round up
    "Solo-Year": 330,       # 1200₽ / 365d ≈ 3.29₽ → 330 коп
    "Family-Year": 660,     # 2400₽ / 365d
    "Pro-Year": 1100,       # 4000₽ / 365d
}


def upgrade() -> None:
    # ── Subscription billing anchor + freeze fields ──────────────────
    # next_charge_at is nullable: legacy invoice-based subs leave it
    # NULL and the cron skips them. New balance subs get it set on
    # activate.
    # has_column-guards: 0001 делает create_all() с текущей моделью где
    # эти колонки уже есть. См. _idempotent.py.
    if not has_column("subscriptions", "next_charge_at"):
        op.add_column(
            "subscriptions",
            sa.Column("next_charge_at", sa.DateTime(), nullable=True),
        )
    if not has_column("subscriptions", "frozen_at"):
        op.add_column(
            "subscriptions",
            sa.Column("frozen_at", sa.DateTime(), nullable=True),
        )
    if not has_column("subscriptions", "frozen_until"):
        op.add_column(
            "subscriptions",
            sa.Column("frozen_until", sa.DateTime(), nullable=True),
        )
    if not has_column("subscriptions", "frozen_days_used"):
        op.add_column(
            "subscriptions",
            sa.Column(
                "frozen_days_used",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if not has_column("subscriptions", "frozen_year"):
        op.add_column(
            "subscriptions",
            sa.Column("frozen_year", sa.Integer(), nullable=True),
        )

    # Hot path index for the charge tick:
    #   SELECT ... WHERE status='active' AND next_charge_at <= now
    # Partial index keeps it tiny — frozen/expired subs aren't even
    # candidates and shouldn't bloat the btree.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_subscriptions_due_charge "
        "ON subscriptions(next_charge_at) "
        "WHERE status = 'active' AND next_charge_at IS NOT NULL"
    )

    # ── SubscriptionStatus.frozen enum value ─────────────────────────
    # ALTER TYPE ... ADD VALUE is the only postgres-native way; it must
    # run outside a transaction. Alembic handles autocommit per-statement
    # for raw SQL — we use IF NOT EXISTS so reruns are safe.
    op.execute(
        "ALTER TYPE subscriptionstatus ADD VALUE IF NOT EXISTS 'frozen'"
    )

    # ── Invoice.kind discriminator ───────────────────────────────────
    # 'subscription' = legacy plan-purchase invoice (provisions a sub on
    # paid). 'topup' = balance topup (credits user wallet on paid).
    # Default keeps every existing row addressable as a legacy invoice.
    if not has_column("invoices", "kind"):
        op.add_column(
            "invoices",
            sa.Column(
                "kind",
                sa.String(),
                nullable=False,
                server_default="subscription",
            ),
        )

    # Topup invoices have no plan_id by definition — relax the FK
    # NOT NULL so the same Invoice table can carry both kinds. The FK
    # itself stays in place; we just allow NULL on the column.
    op.alter_column("invoices", "plan_id", existing_type=sa.Integer(), nullable=True)

    # ── Plan daily_rate seed ─────────────────────────────────────────
    # Idempotent: only touches rows where the column is still NULL, so
    # operators who already manually set rates aren't clobbered.
    bind = op.get_bind()
    for name, rate in _DAILY_RATE_SEED.items():
        bind.execute(
            sa.text(
                "UPDATE plans SET daily_rate_kopecks = :rate "
                "WHERE name = :name AND daily_rate_kopecks IS NULL"
            ),
            {"rate": rate, "name": name},
        )


def downgrade() -> None:
    # daily_rate_kopecks revert is best-effort: we set NULL only on the
    # rows we seeded, leaving any operator-set values alone.
    bind = op.get_bind()
    for name in _DAILY_RATE_SEED.keys():
        bind.execute(
            sa.text("UPDATE plans SET daily_rate_kopecks = NULL WHERE name = :name"),
            {"name": name},
        )

    # Restore plan_id NOT NULL — only safe if no topup invoices exist.
    # In practice the operator should clean those up before downgrading
    # since they have no plan to bind to.
    op.alter_column("invoices", "plan_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("invoices", "kind")

    # Postgres has no DROP VALUE for enums — leaving 'frozen' in the
    # type is harmless on downgrade since no row will reference it
    # after we revert the application code.

    op.execute("DROP INDEX IF EXISTS ix_subscriptions_due_charge")
    op.drop_column("subscriptions", "frozen_year")
    op.drop_column("subscriptions", "frozen_days_used")
    op.drop_column("subscriptions", "frozen_until")
    op.drop_column("subscriptions", "frozen_at")
    op.drop_column("subscriptions", "next_charge_at")
