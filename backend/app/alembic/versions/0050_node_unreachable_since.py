"""Add unreachable_since to vpn_nodes + wg_exit_nodes (reachability confirm-window).

Revision ID: 0050_node_unreachable_since
Revises: 0049_cloud_provider_billmgr_enum

Начало текущей серии непрошедших controller→host probe'ов. Reachability-tick
ставит её на первом DOWN, чистит на recovery; админ-алерт о недоступности
шлётся только когда серия длится >= NODE_ALERT_CONFIRM_MIN — анти-спам, чтобы
единичные пропущенные пинги не будили админа. Колонка на ОБЕИХ таблицах, т.к.
тик гоняет nodes и exits общим кодом (duck-typed).
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import Column, DateTime

from app.alembic._idempotent import has_column

revision = "0050_node_unreachable_since"
down_revision = "0049_cloud_provider_billmgr_enum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("vpn_nodes", "unreachable_since"):
        op.add_column(
            "vpn_nodes", Column("unreachable_since", DateTime, nullable=True)
        )
    if not has_column("wg_exit_nodes", "unreachable_since"):
        op.add_column(
            "wg_exit_nodes", Column("unreachable_since", DateTime, nullable=True)
        )


def downgrade() -> None:
    op.drop_column("wg_exit_nodes", "unreachable_since")
    op.drop_column("vpn_nodes", "unreachable_since")
