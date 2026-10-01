"""Store hoster-issued root password on nodes (4vps SSH bootstrap).

Revision ID: 0045_node_provider_root_password
Revises: 0044_cloud_provider_4vps_enum

Колонка ``vpn_nodes.provider_root_password_enc`` (Fernet) — рут-пароль, который
хостер выдаёт при заказе VPS, для провайдеров без инъекции SSH-ключа (4vps).
Нужен для first-connect SSH перед установкой нашего ключа. NULL у key-based
провайдеров. Idempotent через has_column.

См. docs/operations/hoster_api_epic.md.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.alembic._idempotent import has_column

revision = "0045_node_provider_root_password"
down_revision = "0044_cloud_provider_4vps_enum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("vpn_nodes", "provider_root_password_enc"):
        op.add_column(
            "vpn_nodes",
            sa.Column("provider_root_password_enc", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("vpn_nodes", "provider_root_password_enc")
