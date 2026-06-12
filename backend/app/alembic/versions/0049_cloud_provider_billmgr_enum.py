"""Add 'billmgr' member to cloudproviderkind enum.

Revision ID: 0049_cloud_provider_billmgr_enum
Revises: 0048_cloud_provider_vdsina_enum

Generic ISPsystem BILLmanager driver (services/cloud/billmgr.py) — обслуживает
пачку RU-хостеров (DataCheap/UFO/AdminVPS), хост+креды в api_token_enc как JSON.
Имя члена == value == "billmgr" (рассинхрона как у 4vps нет), label прямой.

Idempotent через ADD VALUE IF NOT EXISTS.
"""
from __future__ import annotations

from alembic import op

revision = "0049_cloud_provider_billmgr_enum"
down_revision = "0048_cloud_provider_vdsina_enum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE cloudproviderkind ADD VALUE IF NOT EXISTS 'billmgr'")


def downgrade() -> None:
    # PG не поддерживает DROP VALUE; лишний label безвреден.
    pass
