"""Dedupe + UNIQUE(relay_node_id, exit_id) on relay_exit_links.

Revision ID: 0031_uniq_relay_exit_pair
Revises: 0030_device_config_id_nullable

До этой миграции таблица защищена только UNIQUE(relay_id,
wg_interface_name) — одна и та же (relay, exit) пара могла
появиться дважды с разными wgN, если два attach_relay POST'а
приходили одновременно (read-then-insert guard не race-safe) или
если orphan-link оставался после упавшей ansible-таски и админ
повторял attach с нового wgN. Результат — два peer в wg0.conf
exit'а с одинаковым AllowedIPs: WG match'ит первый, curl через
второй интерфейс уходит в rc=28.

Очищаем дубли (оставляем min(id) — обычно первый, работающий) и
ставим композитный UNIQUE, чтобы такие вставки падали на уровне
БД независимо от гонки в API.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0031_uniq_relay_exit_pair"
down_revision = "0030_device_config_id_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Удаляем все дубли одним DELETE — min(id) на каждую (relay, exit)
    # пару оставляем, остальное вычищаем. RAISE NOTICE показывает
    # админу, сколько строк удалено.
    bind.execute(
        sa.text(
            """
            WITH dupes AS (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY relay_node_id, exit_id
                           ORDER BY id
                       ) AS rn
                FROM relay_exit_links
            )
            DELETE FROM relay_exit_links
            WHERE id IN (SELECT id FROM dupes WHERE rn > 1)
            """
        )
    )

    inspector = sa.inspect(bind)
    existing_ucs = {
        uc.get("name") for uc in inspector.get_unique_constraints("relay_exit_links")
    }
    if "uq_relay_exit_links_relay_exit" not in existing_ucs:
        op.create_unique_constraint(
            "uq_relay_exit_links_relay_exit",
            "relay_exit_links",
            ["relay_node_id", "exit_id"],
        )


def downgrade() -> None:
    op.drop_constraint(
        "uq_relay_exit_links_relay_exit",
        "relay_exit_links",
        type_="unique",
    )
    # Дубликаты, удалённые в upgrade(), не восстанавливаем — схема
    # rollback только снимает constraint.
