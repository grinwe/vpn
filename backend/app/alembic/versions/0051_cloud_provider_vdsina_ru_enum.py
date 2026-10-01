"""Add 'vdsina_ru' member to cloudproviderkind enum.

Revision ID: 0051_cloud_provider_vdsina_ru_enum
Revises: 0050_node_unreachable_since

Отдельная .ru-инсталляция VDSina (тот же драйвер, base → userapi.vdsina.ru;
см. get_driver). vdsina и vdsina_ru — РАЗНЫЕ аккаунты/токены/балансы/домены.
Имя == value == "vdsina_ru", label прямой. Без этого INSERT провайдера
kind=vdsina_ru падал бы invalid input value for enum.

Idempotent через ADD VALUE IF NOT EXISTS.
"""
from __future__ import annotations

from alembic import op

revision = "0051_cloud_provider_vdsina_ru_enum"
down_revision = "0050_node_unreachable_since"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE cloudproviderkind ADD VALUE IF NOT EXISTS 'vdsina_ru'")


def downgrade() -> None:
    # PG не поддерживает DROP VALUE; лишний label безвреден.
    pass
