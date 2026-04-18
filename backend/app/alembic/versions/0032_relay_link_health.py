"""Health telemetry columns on relay_exit_links.

Revision ID: 0032_relay_link_health
Revises: 0031_uniq_relay_exit_pair

До этой миграции у админа нет ответа на вопрос «работает ли
сейчас туннель между этим relay и exit» — видно только, что
link существует и ансибл таска когда-то прошла. Когда батч
attach падает на половине relay (как после 0030 + handler-bug)
или когда RKN блочит UDP endpoint, админ узнаёт об этом только
от юзеров.

Тик ``run_relay_link_health_tick`` в worker'е будет раз в 5 минут
ходить SSH'ем на каждый relay с ``relay_config``, читать
``wg show all dump`` и заполнять эти колонки. UI в Exits.tsx
показывает цветной индикатор на основе «как давно был
handshake».

Все колонки nullable — свежий link до первого тика логично
оставить без данных, а NULL в UI рендерится как «нет данных»
(серый).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0032_relay_link_health"
down_revision = "0031_uniq_relay_exit_pair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "relay_exit_links",
        sa.Column("last_handshake_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "relay_exit_links",
        sa.Column("last_rx_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "relay_exit_links",
        sa.Column("last_tx_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "relay_exit_links",
        sa.Column("last_observed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("relay_exit_links", "last_observed_at")
    op.drop_column("relay_exit_links", "last_tx_bytes")
    op.drop_column("relay_exit_links", "last_rx_bytes")
    op.drop_column("relay_exit_links", "last_handshake_at")
