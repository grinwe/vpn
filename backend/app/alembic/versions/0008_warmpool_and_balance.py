"""Warm credential pool + balance billing scaffolding.

Revision ID: 0008_warmpool_and_balance
Revises: 0007_seed_plans
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.alembic._idempotent import (
    has_column,
    has_foreign_key,
    has_index,
    has_table,
)


revision = "0008_warmpool_and_balance"
down_revision = "0007_seed_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Stage 2.5: warm credentials pool ─────────────────────────────
    pool_state = postgresql.ENUM(
        "warm", "assigned", "revoked",
        name="credentialpoolstate",
        create_type=False,
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='credentialpoolstate') "
        "THEN CREATE TYPE credentialpoolstate AS ENUM ('warm','assigned','revoked'); "
        "END IF; END $$;"
    )

    # Каждый op.add_column обёрнут в has_column-guard. Причина — 0001
    # делает Base.metadata.create_all() с текущим состоянием моделей, у
    # которых эти колонки уже есть. См. app/alembic/_idempotent.py.
    if not has_column("credentials", "pool_state"):
        op.add_column(
            "credentials",
            sa.Column(
                "pool_state",
                pool_state,
                nullable=False,
                server_default="assigned",
            ),
        )
    if not has_column("credentials", "warmed_at"):
        op.add_column("credentials", sa.Column("warmed_at", sa.DateTime(), nullable=True))
    if not has_column("credentials", "assigned_at"):
        op.add_column("credentials", sa.Column("assigned_at", sa.DateTime(), nullable=True))
    if not has_column("credentials", "node_id"):
        op.add_column("credentials", sa.Column("node_id", sa.Integer(), nullable=True))
    if not has_column("credentials", "access_username"):
        op.add_column("credentials", sa.Column("access_username", sa.String(), nullable=True))
    if not has_index("credentials", "ix_credentials_access_username"):
        op.create_index("ix_credentials_access_username", "credentials", ["access_username"])
    if not has_foreign_key("credentials", "fk_credentials_node"):
        op.create_foreign_key(
            "fk_credentials_node", "credentials", "vpn_nodes", ["node_id"], ["id"]
        )
    if not has_index("credentials", "ix_credentials_node_id"):
        op.create_index("ix_credentials_node_id", "credentials", ["node_id"])

    # subscription_id needs to become nullable so warm credentials can
    # exist without a sub. alter_column идемпотентен по природе (если
    # уже NULL — повторное "сделай NULL" no-op).
    op.alter_column(
        "credentials", "subscription_id", existing_type=sa.Integer(), nullable=True
    )

    # Partial index for the hot path: "give me a warm cred on this node".
    # Only indexes warm rows, so it's tiny and the FOR UPDATE SKIP LOCKED
    # scan stays sub-millisecond regardless of how big the credentials
    # table grows.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_credentials_warm_node "
        "ON credentials(node_id) WHERE pool_state = 'warm'"
    )

    # Backfill node_id for pre-existing assigned credentials so the
    # warm-pool queries (which filter by node_id) see legacy rows too.
    op.execute(
        "UPDATE credentials SET node_id = vpn_configs.node_id "
        "FROM vpn_configs "
        "WHERE credentials.config_id = vpn_configs.id "
        "AND credentials.node_id IS NULL"
    )

    # ── Stage 4: balance billing ─────────────────────────────────────
    if not has_column("users", "balance_kopecks"):
        op.add_column(
            "users",
            sa.Column(
                "balance_kopecks",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if not has_column("plans", "daily_rate_kopecks"):
        op.add_column(
            "plans",
            sa.Column("daily_rate_kopecks", sa.Integer(), nullable=True),
        )

    tx_kind = postgresql.ENUM(
        "topup", "spend", "refund", "bonus", "adjust",
        name="balancetxkind",
        create_type=False,
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='balancetxkind') "
        "THEN CREATE TYPE balancetxkind AS ENUM "
        "('topup','spend','refund','bonus','adjust'); "
        "END IF; END $$;"
    )

    if not has_table("balance_transactions"):
        op.create_table(
            "balance_transactions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("amount_kopecks", sa.Integer(), nullable=False),
            sa.Column("kind", tx_kind, nullable=False),
            sa.Column("reference", sa.String(), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
    if not has_index("balance_transactions", "ix_balance_transactions_user_id"):
        op.create_index(
            "ix_balance_transactions_user_id", "balance_transactions", ["user_id"]
        )
    if not has_index("balance_transactions", "ix_balance_transactions_created_at"):
        op.create_index(
            "ix_balance_transactions_created_at", "balance_transactions", ["created_at"]
        )


def downgrade() -> None:
    op.drop_index("ix_balance_transactions_created_at", "balance_transactions")
    op.drop_index("ix_balance_transactions_user_id", "balance_transactions")
    op.drop_table("balance_transactions")
    op.execute("DROP TYPE IF EXISTS balancetxkind")

    op.drop_column("plans", "daily_rate_kopecks")
    op.drop_column("users", "balance_kopecks")

    op.execute("DROP INDEX IF EXISTS ix_credentials_warm_node")
    op.alter_column(
        "credentials", "subscription_id", existing_type=sa.Integer(), nullable=False
    )
    op.drop_index("ix_credentials_node_id", "credentials")
    op.drop_constraint("fk_credentials_node", "credentials", type_="foreignkey")
    op.drop_column("credentials", "node_id")
    op.drop_index("ix_credentials_access_username", "credentials")
    op.drop_column("credentials", "access_username")
    op.drop_column("credentials", "assigned_at")
    op.drop_column("credentials", "warmed_at")
    op.drop_column("credentials", "pool_state")
    op.execute("DROP TYPE IF EXISTS credentialpoolstate")
