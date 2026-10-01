"""auto_diagnose_disabled_at переезжает с relay_exit_links на vpn_nodes.

Revision ID: 0036_node_auto_diagnose_disabled
Revises: 0035_relay_link_auto_diagnose_disabled

Контекст: оригинальный mute-флаг (миграция 0035) жил на каждом
RelayExitLink — оператор должен был раскрыть ноду, найти конкретный
exit-link и кликнуть mute на каждом. UX неудобный для случая «нода
сейчас сломана, заглуши все алерты вообще» — клик на N линков плюс
confirm-диалог на каждом. Плюс mute не покрывал Telegram-алёрты на
SSH failure ноды.

Переезд: колонка `auto_diagnose_disabled_at` теперь на `vpn_nodes`.
Один mute глушит:
  * smart-диагностику ВСЕХ link'ов этой ноды (worker filter через JOIN)
  * Telegram-алерты с failed_relay_names == node.name (см. _stage 2)
  * (future) node-level smart-diagnose tick'и

Миграция переносит существующие значения: для каждой ноды, у которой
хотя бы один link замьючен — node.auto_diagnose_disabled_at = MAX из
её link.auto_diagnose_disabled_at (= когда последний раз кто-то
выключил). Это лучше чем дропнуть данные silently.

Idempotent через has_column.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.alembic._idempotent import has_column


revision = "0036_node_auto_diagnose_disabled"
down_revision = "0035_relay_link_auto_diagnose_disabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("vpn_nodes", "auto_diagnose_disabled_at"):
        op.add_column(
            "vpn_nodes",
            sa.Column(
                "auto_diagnose_disabled_at",
                sa.DateTime(),
                nullable=True,
            ),
        )

    # Перенос значений: для каждого relay_node_id, у которого есть хотя
    # бы один link с auto_diagnose_disabled_at IS NOT NULL — проставляем
    # на ноду MAX(link.auto_diagnose_disabled_at). Идемпотентно: если
    # node.auto_diagnose_disabled_at уже set, не перезаписываем.
    if has_column("relay_exit_links", "auto_diagnose_disabled_at"):
        op.execute(
            """
            UPDATE vpn_nodes vn
            SET auto_diagnose_disabled_at = sub.muted_at
            FROM (
                SELECT relay_node_id, MAX(auto_diagnose_disabled_at) AS muted_at
                FROM relay_exit_links
                WHERE auto_diagnose_disabled_at IS NOT NULL
                GROUP BY relay_node_id
            ) sub
            WHERE vn.id = sub.relay_node_id
              AND vn.auto_diagnose_disabled_at IS NULL
            """
        )
        op.drop_column("relay_exit_links", "auto_diagnose_disabled_at")


def downgrade() -> None:
    # Re-add link-level column without copy-back (downgrade — манёвр
    # последней надежды, восстанавливать per-link gradients из node
    # state нечем). Оператор может вернуть mute после downgrade'а
    # руками, проставив timestamp на нужные links.
    op.add_column(
        "relay_exit_links",
        sa.Column("auto_diagnose_disabled_at", sa.DateTime(), nullable=True),
    )
    op.drop_column("vpn_nodes", "auto_diagnose_disabled_at")
