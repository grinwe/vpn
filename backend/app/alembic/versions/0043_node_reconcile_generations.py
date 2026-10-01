"""Phase 2+3 reconciler: desired-state generations + debounce на vpn_nodes.

Revision ID: 0043_node_reconcile_generations
Revises: 0042_provisioning_task_cancel

Колонки (все на vpn_nodes):
  * desired_generation     — бампается на КАЖДОЙ правке/запросе bootstrap'а
                             (правка перестаёт сразу диспатчить таску).
  * reconciled_generation  — generation, до которого нода реально сошлась
                             (ставится на успехе bootstrap'а).
  * reconcile_due_at       — debounce-таймер: reconcile-тик берёт ноды, у
                             которых он наступил И desired > reconciled.

Работает ТОЛЬКО при RECONCILER_ENABLED=1 (по умолчанию off → поведение
Phase 0 не меняется). См. docs/operations/provisioning_reconciler_epic.md.
Idempotent через has_column.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.alembic._idempotent import has_column


revision = "0043_node_reconcile_generations"
down_revision = "0042_provisioning_task_cancel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("vpn_nodes", "desired_generation"):
        op.add_column(
            "vpn_nodes",
            sa.Column(
                "desired_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if not has_column("vpn_nodes", "reconciled_generation"):
        op.add_column(
            "vpn_nodes",
            sa.Column(
                "reconciled_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if not has_column("vpn_nodes", "reconcile_due_at"):
        op.add_column(
            "vpn_nodes",
            sa.Column("reconcile_due_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("vpn_nodes", "reconcile_due_at")
    op.drop_column("vpn_nodes", "reconciled_generation")
    op.drop_column("vpn_nodes", "desired_generation")
