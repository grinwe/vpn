"""devices.pending_swap_from — журнал намерения failover-свапа.

Revision ID: 0068_device_pending_swap
Revises: 0067_payment_pay_url

Failover делает несколько коммитов (создать замену → ревокнуть старый →
перенести sub_token), и оба его слабых места — смерть процесса между
коммитами (девайс-сирота навсегда занимает слот лимита плана) и сухой
warm-пул (старый погашен, замена мертва до ansible → пустая выдача) —
лечатся одинаково: замена создаётся с маркером «кого меняю», своп
доделывается по факту активации (или жнецом-тиком после сбоя), маркер
снимается в той же транзакции, что и своп.

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column, has_index

revision = "0068_device_pending_swap"
down_revision = "0067_payment_pay_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("devices", "pending_swap_from"):
        op.add_column(
            "devices",
            sa.Column(
                "pending_swap_from",
                sa.Integer(),
                sa.ForeignKey("devices.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    # Отдельный гард (ревью 2026-08-25): на свежей БД create_all уже создал
    # колонку И индекс из модели — но на инкрементальной, где add_column
    # выполнился в прошлом прогоне, вложенный create_index не выполнился бы
    # никогда. Частичный индекс: жнец сканирует только незавершённые свапы,
    # которых в норме ноль.
    if not has_index("devices", "ix_devices_pending_swap_from"):
        op.create_index(
            "ix_devices_pending_swap_from",
            "devices",
            ["pending_swap_from"],
            postgresql_where=sa.text("pending_swap_from IS NOT NULL"),
        )


def downgrade() -> None:
    op.drop_index("ix_devices_pending_swap_from", table_name="devices")
    op.drop_column("devices", "pending_swap_from")
