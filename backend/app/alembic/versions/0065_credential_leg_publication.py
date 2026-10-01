"""credentials.leg_published / leg_role — схема публикации легов.

Revision ID: 0065_credential_leg_publication
Revises: 0064_referral_days_backfill

Эпик «набор эндпоинтов» (docs/operations/subset_epic_2026_07_29.md): в подписку
отдаём один протокол с ноды вместо всех четырёх. Тёплый бандл при этом
по-прежнему назначается целиком — на ноде под одним именем лежат все протоколы,
просто три из них не опубликованы. Благодаря этому «сменить reality на xhttp на
той же ноде» стоит переставить флаг, без единого ansible-прогона.

Backfill `true` намеренный: сразу после миграции поведение не меняется ни на
байт, а схема включается отдельным флагом SUB_LEG_SCHEME.

Почему отдельная колонка, а не is_active: is_active означает «учётка жива на
ноде» и массово переставляется провижинингом (apply-таска поднимает креды
устройства), поэтому схема публикации на нём разваливалась бы молча.

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0065_credential_leg_publication"
down_revision = "0064_referral_days_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("credentials", "leg_published"):
        op.add_column(
            "credentials",
            sa.Column(
                "leg_published",
                sa.Boolean(),
                nullable=False,
                server_default="true",
            ),
        )
    if not has_column("credentials", "leg_role"):
        op.add_column("credentials", sa.Column("leg_role", sa.String(), nullable=True))


def downgrade() -> None:
    for name in ("leg_role", "leg_published"):
        if has_column("credentials", name):
            op.drop_column("credentials", name)
