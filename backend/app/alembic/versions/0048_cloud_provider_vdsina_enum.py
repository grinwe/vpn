"""Add 'vdsina' member to cloudproviderkind enum.

Revision ID: 0048_cloud_provider_vdsina_enum
Revises: 0047_exit_provider_root_password

Новый драйвер VDSina (services/cloud/vdsina.py). SQLAlchemy для Enum хранит в PG
ИМЯ члена; у vdsina имя == value == "vdsina" (рассинхрона как у 4vps нет), поэтому
label прямой. Без этого INSERT провайдера kind=vdsina падал бы
``invalid input value for enum cloudproviderkind: "vdsina"``.

Idempotent через ADD VALUE IF NOT EXISTS.
"""
from __future__ import annotations

from alembic import op

revision = "0048_cloud_provider_vdsina_enum"
down_revision = "0047_exit_provider_root_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE cloudproviderkind ADD VALUE IF NOT EXISTS 'vdsina'")


def downgrade() -> None:
    # PG не поддерживает DROP VALUE; лишний label безвреден.
    pass
