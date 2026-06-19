"""Add users.source — ad-campaign attribution tag (first-touch).

Revision ID: 0054_user_source
Revises: 0053_ops_plans_execution

Метка рекламного источника из deep-link старт-параметра Telegram
(``t.me/bot?start=<tag>``). Ставится один раз при первом /start с меткой (не
``ref_`` — те идут в referred_by_id). Воронка started→trial→paid по этой колонке.
См. docs/operations/ad_source_attribution.md.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_user_source"
down_revision = "0053_ops_plans_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("users")}
    if "source" not in cols:
        op.add_column("users", sa.Column("source", sa.String(length=64), nullable=True))
    existing = {ix["name"] for ix in inspector.get_indexes("users")}
    if "ix_users_source" not in existing:
        op.create_index("ix_users_source", "users", ["source"])


def downgrade() -> None:
    op.drop_index("ix_users_source", table_name="users")
    op.drop_column("users", "source")
