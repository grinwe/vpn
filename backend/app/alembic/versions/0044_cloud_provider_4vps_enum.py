"""Add 4vps.su to cloudproviderkind enum.

Revision ID: 0044_cloud_provider_4vps_enum
Revises: 0043_node_reconcile_generations

Добавляет enum-значение ``4vps`` в тип ``cloudproviderkind`` — новый
cloud-driver FourVpsDriver (services/cloud/fourvps.py). Кредсы провайдера
хранятся в существующей колонке ``cloud_providers.api_token_enc`` как
``panel_id:apikey`` (Fernet), миграция схемы не нужна.

ALTER TYPE ... ADD VALUE требует PG 12+ (работает внутри транзакции; новое
значение нельзя ИСПОЛЬЗОВАТЬ в той же транзакции — мы и не используем).
Idempotent через ADD VALUE IF NOT EXISTS.

См. docs/operations/hoster_api_epic.md.
"""
from __future__ import annotations

from alembic import op

revision = "0044_cloud_provider_4vps_enum"
down_revision = "0043_node_reconcile_generations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE cloudproviderkind ADD VALUE IF NOT EXISTS '4vps'")


def downgrade() -> None:
    # PG не поддерживает DROP VALUE; лишний label безвреден.
    pass
