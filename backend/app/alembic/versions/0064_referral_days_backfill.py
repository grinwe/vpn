"""Актуальные дефолты реферальных наград + стоп-бэкфилл приглашений.

Revision ID: 0064_referral_days_backfill
Revises: 0063_ad_link_cost

Две правки данных, обе — следствие вчерашнего выката.

1. `referral_codes.reward_days` со дня первой миграции лежал со значением 3 и
   не читался никем: награда рефереру была фиксированной суммой 50 ₽. Как
   только код начал читать поле, награда молча упала втрое — 3 дня это 15 ₽ по
   текущему прайсу. Возвращаем прежнюю экономику: 10 дней = ровно те же 50 ₽,
   просто в днях (так подарок читается весомее при той же цене).

   `bonus_days` НЕ трогаем: 3 дня приглашённому сверх 30 дней триала — это
   осознанный размер, он ещё ничего не заплатил.

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
# 10 дней = 50 ₽ (Solo 150 ₽ / 30 дней) — прежняя фиксированная награда.
_NEW_REWARD_DAYS = 10


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
        # Только NULL-ы: значение 3 у приглашённого — это текущий дефолт, а не
        # historical leftover, менять его не нужно.
        bind.execute(
            sa.text(
                "UPDATE referral_codes SET bonus_days = :new "
                "WHERE bonus_days IS NULL"
            ),
            {"new": _LEGACY_DAYS},
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
    # Данные назад не откатываем: вернуть 15-рублёвую награду и снова разослать
    # приглашения существующим пользователям — ровно тот исход, ради устранения
    # которого миграция и написана.
    pass
