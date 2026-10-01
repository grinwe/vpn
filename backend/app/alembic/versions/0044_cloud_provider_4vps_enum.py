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
    # NB: SQLAlchemy Enum хранит ИМЯ Python-члена ('fourvps'), не value ('4vps')
    # — как vless_reality хранится именем, а не 'vless-reality'. Поэтому в PG-enum
    # нужно 'fourvps'. (Изначально тут было '4vps' — баг; прод дочинен миграцией
    # 0046, этот ADD оставлен для свежих БД корректным.)
    op.execute("ALTER TYPE cloudproviderkind ADD VALUE IF NOT EXISTS 'fourvps'")


def downgrade() -> None:
    # PG не поддерживает DROP VALUE; лишний label безвреден.
    pass
