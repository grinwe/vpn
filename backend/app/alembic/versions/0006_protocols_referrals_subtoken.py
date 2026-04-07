"""Add new protocols, referral_codes, sub_token, is_visible, referred_by_id.

Revision ID: 0006_protocols_referrals_subtoken
Revises: 0005_api_tokens
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_protocols_referrals_subtoken"
down_revision = "0005_api_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Extend VPNConfigProtocol enum with new values ---
    # Postgres enums need explicit ALTER TYPE for new values.
    op.execute("ALTER TYPE vpnconfigprotocol ADD VALUE IF NOT EXISTS 'vless-ws-cdn'")
    op.execute("ALTER TYPE vpnconfigprotocol ADD VALUE IF NOT EXISTS 'hysteria2'")

    # --- Subscription: add sub_token for dynamic links ---
    op.add_column("subscriptions", sa.Column("sub_token", sa.String(), nullable=True))
    op.create_index("ix_subscriptions_sub_token", "subscriptions", ["sub_token"], unique=True)

    # --- Plan: add is_visible flag ---
    op.add_column("plans", sa.Column("is_visible", sa.Boolean(), server_default="true", nullable=True))

    # --- User: add referred_by_id ---
    op.add_column("users", sa.Column("referred_by_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_users_referred_by", "users", "users", ["referred_by_id"], ["id"])

    # --- Referral codes table ---
    op.create_table(
        "referral_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("bonus_days", sa.Integer(), server_default="3"),
        sa.Column("reward_days", sa.Integer(), server_default="3"),
        sa.Column("uses", sa.Integer(), server_default="0"),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_referral_codes_code", "referral_codes", ["code"], unique=True)


def downgrade() -> None:
    op.drop_table("referral_codes")
    op.drop_column("users", "referred_by_id")
    op.drop_column("plans", "is_visible")
    op.drop_index("ix_subscriptions_sub_token", "subscriptions")
    op.drop_column("subscriptions", "sub_token")
    # Note: cannot remove enum values in Postgres; leave them.
