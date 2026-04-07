"""Seed canonical plans (Solo / Family / Pro × month / year).

Revision ID: 0007_seed_plans
Revises: 0006_protocols_referrals_subtoken

Idempotent: ON CONFLICT (name) DO NOTHING. Existing plans are not touched.
Downgrade removes only the seeded names — handy on staging, no-op in prod
once the rows have been edited.
"""
from alembic import op

revision = "0007_seed_plans"
down_revision = "0006_protocols_referrals_subtoken"
branch_labels = None
depends_on = None


PLANS = [
    # (name, duration_days, max_devices, price)
    ("Solo",         30,  1,  150),
    ("Family",       30,  3,  300),
    ("Pro",          30,  5,  500),
    ("Solo-Year",    365, 1,  1200),
    ("Family-Year",  365, 3,  2400),
    ("Pro-Year",     365, 5,  4000),
]


def upgrade() -> None:
    for name, days, devices, price in PLANS:
        op.execute(
            f"""
            INSERT INTO plans (name, duration_days, max_devices, price, traffic_limit_mb, is_visible)
            VALUES ('{name}', {days}, {devices}, {price}, NULL, true)
            ON CONFLICT (name) DO NOTHING
            """
        )


def downgrade() -> None:
    names = ", ".join(f"'{n}'" for n, *_ in PLANS)
    op.execute(f"DELETE FROM plans WHERE name IN ({names})")
