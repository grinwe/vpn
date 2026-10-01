"""Store hoster-issued root password on WG exit nodes (4vps SSH bootstrap).

Revision ID: 0047_exit_provider_root_password
Revises: 0046_cloud_provider_4vps_enum_name_fix

Колонка ``wg_exit_nodes.provider_root_password_enc`` (Fernet) — рут-пароль,
который хостер выдаёт при заказе VPS для провайдеров без инъекции SSH-ключа
(4vps). Нужен, чтобы worker зашёл по паролю и положил provisioning-ключ перед
``bootstrap_exit.yml`` (как у VPNNode, миграция 0045). NULL у exit'ов, заведённых
вручную / у key-based провайдеров. Включает cloud-spawn для exit-нод
(``node_spawner.spawn_exit_async``). Idempotent через has_column.

См. docs/operations/hoster_api_epic.md.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0047_exit_provider_root_password"
down_revision = "0046_cloud_provider_4vps_enum_name_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("wg_exit_nodes", "provider_root_password_enc"):
        op.add_column(
            "wg_exit_nodes",
            sa.Column("provider_root_password_enc", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("wg_exit_nodes", "provider_root_password_enc")
