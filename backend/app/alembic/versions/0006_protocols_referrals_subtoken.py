"""Add new protocols, referral_codes, sub_token, is_visible, referred_by_id.

Revision ID: 0006_protocols_referrals_subtoken
Revises: 0005_api_tokens

Every DDL here is idempotent (``IF NOT EXISTS`` / ``DO $$ ... $$`` guards)
because ``0001_initial`` delegates to ``Base.metadata.create_all()``. On a
fresh database that means the entire *current* model schema — including
the columns this revision is nominally responsible for — already exists
by the time we get here. The guards let Alembic stamp 0006 on both fresh
and legacy databases without blowing up on duplicate columns/indexes.
"""
from alembic import op

revision = "0006_protocols_referrals_subtoken"
down_revision = "0005_api_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Extend VPNConfigProtocol enum with new values ---
    op.execute("ALTER TYPE vpnconfigprotocol ADD VALUE IF NOT EXISTS 'vless-ws-cdn'")
    op.execute("ALTER TYPE vpnconfigprotocol ADD VALUE IF NOT EXISTS 'hysteria2'")

    # --- Subscription: add sub_token for dynamic links ---
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS sub_token VARCHAR")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_subscriptions_sub_token "
        "ON subscriptions (sub_token)"
    )

    # --- Plan: add is_visible flag ---
    op.execute(
        "ALTER TABLE plans ADD COLUMN IF NOT EXISTS is_visible BOOLEAN DEFAULT true"
    )

    # --- User: add referred_by_id + self-FK ---
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_id INTEGER")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_users_referred_by'
            ) THEN
                ALTER TABLE users
                    ADD CONSTRAINT fk_users_referred_by
                    FOREIGN KEY (referred_by_id) REFERENCES users(id);
            END IF;
        END $$;
        """
    )

    # --- Referral codes table ---
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS referral_codes (
            id SERIAL PRIMARY KEY,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            code VARCHAR(32) NOT NULL,
            bonus_days INTEGER DEFAULT 3,
            reward_days INTEGER DEFAULT 3,
            uses INTEGER DEFAULT 0,
            max_uses INTEGER,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMP DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_referral_codes_code "
        "ON referral_codes (code)"
    )


def downgrade() -> None:
    op.drop_table("referral_codes")
    op.drop_column("users", "referred_by_id")
    op.drop_column("plans", "is_visible")
    op.drop_index("ix_subscriptions_sub_token", "subscriptions")
    op.drop_column("subscriptions", "sub_token")
    # Note: cannot remove enum values in Postgres; leave them.
