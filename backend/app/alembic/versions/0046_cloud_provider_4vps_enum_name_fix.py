"""Fix 4vps enum label: add member NAME 'fourvps' to cloudproviderkind.

Revision ID: 0046_cloud_provider_4vps_enum_name_fix
Revises: 0045_node_provider_root_password

Миграция 0044 ошибочно добавила в PG-enum значение '4vps' (это VALUE члена), но
SQLAlchemy для Enum хранит ИМЯ члена — 'fourvps' (как vless_reality, а не
'vless-reality'). Из-за этого INSERT провайдера kind=4vps падал:
``invalid input value for enum cloudproviderkind: "fourvps"`` → 500 на
POST /cloud/providers. Добавляем правильный label 'fourvps'. Старый '4vps' в
типе остаётся неиспользуемым (PG не умеет DROP VALUE) — безвреден.

Idempotent через ADD VALUE IF NOT EXISTS.
"""
from __future__ import annotations

from alembic import op

revision = "0046_cloud_provider_4vps_enum_name_fix"
down_revision = "0045_node_provider_root_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE cloudproviderkind ADD VALUE IF NOT EXISTS 'fourvps'")


def downgrade() -> None:
    # PG не поддерживает DROP VALUE; лишний label безвреден.
    pass
