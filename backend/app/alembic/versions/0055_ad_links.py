"""Add ad_links — managed ad-campaign deep-links (admin).

Revision ID: 0055_ad_links
Revises: 0054_user_source

Управляемая рекламная ссылка: name (ярлык) + tag (метка в t.me/bot?start=<tag> →
User.source). Статистика-воронка по User.source==tag; is_active гасит атрибуцию
новых заходов. См. docs/operations/ad_source_attribution.md.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_ad_links"
down_revision = "0054_user_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("ad_links"):
        op.create_table(
            "ad_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("tag", sa.String(length=64), nullable=False),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default="true"
            ),
            sa.Column("notes", sa.String(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
            ),
        )
    existing = {ix["name"] for ix in inspector.get_indexes("ad_links")}
    if "ix_ad_links_tag" not in existing:
        op.create_index("ix_ad_links_tag", "ad_links", ["tag"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_ad_links_tag", table_name="ad_links")
    op.drop_table("ad_links")
