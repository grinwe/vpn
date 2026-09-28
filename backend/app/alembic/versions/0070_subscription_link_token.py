"""subscriptions.link_token — ссылка устройства, которую показывает бот.

Revision ID: 0070_subscription_link_token
Revises: 0069_backfill_device_last_seen

Бот отдавал юзеру legacy-токен ПОДПИСКИ, а он отдаёт креды всех устройств
подписки сразу: телефон с ссылкой из бота занимал логины device-2/device-3,
раздаваемых родным из кабинета (user 1000076, 27.09.2026: 12 конфигов в одном
Hiddify). Теперь при создании подписки запоминаем токен первого устройства,
и бот показывает его.

Бэкфилла НЕТ намеренно: NULL = «подписка до этого релиза», ей бот продолжает
показывать legacy-ссылку — она уже вбита в клиенты, другая ссылка в /config
дала бы при переимпорте профиль-дубль. Никакой гейт по NULL не рассылает
сообщений (урок feedback_new_gate_column_backfill не применим).

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0070_subscription_link_token"
down_revision = "0069_backfill_device_last_seen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("subscriptions", "link_token"):
        op.add_column(
            "subscriptions", sa.Column("link_token", sa.String(), nullable=True)
        )


def downgrade() -> None:
    op.drop_column("subscriptions", "link_token")
