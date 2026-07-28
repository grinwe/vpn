"""Актуальные дефолты реферальных наград + стоп-бэкфилл приглашений.

Revision ID: 0064_referral_days_backfill
Revises: 0063_ad_link_cost

Две правки данных, обе — следствие вчерашнего выката.

1. `referral_codes.bonus_days` / `reward_days` со дня первой миграции лежали со
   значением 3 и не читались никем (награда была фиксированной суммой в 50 ₽).
   Как только код начал их читать, пользователю написали «получишь 3 дня»
   вместо задуманных 30. Подтягиваем к текущим дефолтам ровно те строки, где
   стоит историческое значение, — если оператор выставил своё, не трогаем.

2. `users.first_config_fetch_at` появился пустым у ВСЕХ пользователей, а
   приглашение позвать друга гейтится по «эта колонка пуста». Из-за этого
   письмо-приглашение уходило не только новым, но и каждому существующему
   пользователю при очередном обновлении подписки в клиенте. Проставляем метку
   всем, кто зарегистрировался до этой миграции: приглашение — механика для
   новых, а старым оно приходит как непрошеная рассылка.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0064_referral_days_backfill"
down_revision = "0063_ad_link_cost"
branch_labels = None
depends_on = None

_LEGACY_DAYS = 3
_NEW_REWARD_DAYS = 30
_NEW_BONUS_DAYS = 7


def upgrade() -> None:
    bind = op.get_bind()

    if has_column("referral_codes", "reward_days"):
        bind.execute(
            sa.text(
                "UPDATE referral_codes SET reward_days = :new "
                "WHERE reward_days = :legacy OR reward_days IS NULL"
            ),
            {"new": _NEW_REWARD_DAYS, "legacy": _LEGACY_DAYS},
        )
    if has_column("referral_codes", "bonus_days"):
        bind.execute(
            sa.text(
                "UPDATE referral_codes SET bonus_days = :new "
                "WHERE bonus_days = :legacy OR bonus_days IS NULL"
            ),
            {"new": _NEW_BONUS_DAYS, "legacy": _LEGACY_DAYS},
        )

    # Стоп непрошеной рассылке: всем, кто уже существует, метка проставляется
    # «задним числом», и приглашение им не уйдёт. NOW() достаточно — колонка
    # используется только как признак «уже было», а не как точная дата.
    if has_column("users", "first_config_fetch_at"):
        bind.execute(
            sa.text(
                "UPDATE users SET first_config_fetch_at = NOW() "
                "WHERE first_config_fetch_at IS NULL"
            )
        )


def downgrade() -> None:
    # Данные назад не откатываем: вернуть 3 дня и снова разослать приглашения
    # существующим пользователям — ровно тот исход, ради устранения которого
    # миграция и написана.
    pass
