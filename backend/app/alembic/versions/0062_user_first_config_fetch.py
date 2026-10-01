"""users.first_config_fetch_at — момент первого скачивания конфига.

Revision ID: 0062_user_first_config_fetch
Revises: 0061_node_hysteria_version

Нужен, чтобы поймать «момент вау» — единственную точку, где мы точно знаем,
что человек дошёл до рабочего VPN, и где уместно предложить позвать друга.

Почему колонка, а не запрос по audit_logs: строки `subscription_fetch` чистит
ретеншен (90 дней), плюс они сэмплируются (SUB_FETCH_AUDIT_SAMPLE). Прямого
признака «подключился» в системе нет вообще: Device.last_seen_at объявлен, но
никем не пишется, per-user трафик не собирается, hysteria2 для статистики
невидим — так что первое скачивание конфига это лучший доступный сигнал.

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0062_user_first_config_fetch"
down_revision = "0061_node_hysteria_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("users", "first_config_fetch_at"):
        op.add_column(
            "users", sa.Column("first_config_fetch_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    if has_column("users", "first_config_fetch_at"):
        op.drop_column("users", "first_config_fetch_at")
