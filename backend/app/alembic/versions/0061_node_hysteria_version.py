"""vpn_nodes.hysteria_version — фактическая версия бинаря hysteria на ноде.

Revision ID: 0061_node_hysteria_version
Revises: 0060_software_release_pin

Три VLESS-протокола обслуживает один бинарь xray, а hysteria2 — отдельный
демон (hysteria-server) со своим бинарём. Его версия не собиралась и не
пинилась: роль ставила upstream latest один раз и при наличии бинаря установку
пропускала, так что нода получала «то, что было latest в день её рождения», и
разъезд версий по флоту был невидим.

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0061_node_hysteria_version"
down_revision = "0060_software_release_pin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("vpn_nodes", "hysteria_version"):
        op.add_column(
            "vpn_nodes", sa.Column("hysteria_version", sa.String(), nullable=True)
        )


def downgrade() -> None:
    if has_column("vpn_nodes", "hysteria_version"):
        op.drop_column("vpn_nodes", "hysteria_version")
