"""software_releases.pinned_version — целевой пин, записанный воркером.

Revision ID: 0060_software_release_pin
Revises: 0059_software_releases

Пин xray живёт в ``roles/xray_core/defaults/main.yml``, а ansible-дерево лежит
только в образе воркера (``COPY infra``). API-контейнер файл прочитать не может,
поэтому ``GET /api/versions/overview`` отдавал ``pinned: null``. Воркер при
проверке релизов кладёт пин сюда, API читает из БД.

Идемпотентна: на свежей БД (0001 create_all) — no-op за счёт has_column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_column

revision = "0060_software_release_pin"
down_revision = "0059_software_releases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("software_releases", "pinned_version"):
        op.add_column(
            "software_releases", sa.Column("pinned_version", sa.String(), nullable=True)
        )


def downgrade() -> None:
    if has_column("software_releases", "pinned_version"):
        op.drop_column("software_releases", "pinned_version")
