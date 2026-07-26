"""Кэш upstream-релизов стороннего софта (Xray-core).

Revision ID: 0059_software_releases
Revises: 0058_node_software_versions

Тик ``tick-xray-upstream`` кладёт сюда последний релиз с GitHub. Отдельная
таблица, а не поле в конфиге: значение общее для всего флота, у него своя
временная метка проверки, и админке нужно уметь показать «проверяли час назад»
отдельно от «версия такая-то».

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_table.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_table

revision = "0059_software_releases"
down_revision = "0058_node_software_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if has_table("software_releases"):
        return
    op.create_table(
        "software_releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("latest_version", sa.String(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("html_url", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.UniqueConstraint("name", name="uq_software_releases_name"),
    )
    op.create_index("ix_software_releases_name", "software_releases", ["name"])


def downgrade() -> None:
    if has_table("software_releases"):
        op.drop_index("ix_software_releases_name", table_name="software_releases")
        op.drop_table("software_releases")
