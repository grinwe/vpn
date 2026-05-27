"""devices.client_id_hmac — идентификатор клиента в control-channel'е.

Revision ID: 0037_device_client_id_hmac
Revises: 0036_node_auto_diagnose_disabled

Phase A control-channel'а (см. docs/operations/control_channel_roadmap.md):
custom-клиент шлёт `client_id_hmac = HMAC(APP_SECRET_KEY, sub_token)[:12]`
вместо самого sub_token, backend lookup'ит Device по индексу. Колонка
nullable + unique с index'ом — UNIQUE на NULL в PG разрешает множественные
NULL row'ы, что нужно для legacy/pending devices без sub_token'а.

Backfill в той же миграции: для каждой Device с sub_token проставляем
HMAC через `security.compute_client_id_hmac`. Это **тот же helper**, что
будет использоваться в orchestrator при создании новых Device'ов, так что
backfill даёт идентичные значения тому что писалось бы при rollout'е.

Если APP_SECRET_KEY не задан (dev окружение без секрета) — backfill
пропускается, оставляем все NULL. Orchestrator потом пересоздаст
client_id_hmac при следующем provisioning task'е этой Device.

Idempotent через has_column + has_index.
"""
from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa

from app.alembic._idempotent import has_column, has_index


revision = "0037_device_client_id_hmac"
down_revision = "0036_node_auto_diagnose_disabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not has_column("devices", "client_id_hmac"):
        op.add_column(
            "devices",
            sa.Column("client_id_hmac", sa.String(24), nullable=True),
        )
    if not has_index("devices", "ix_devices_client_id_hmac"):
        op.create_index(
            "ix_devices_client_id_hmac",
            "devices",
            ["client_id_hmac"],
            unique=True,
        )

    # ── Backfill из существующих sub_token'ов ────────────────────────
    # Импортим helper лениво, чтобы alembic мог парсить файл без
    # инициализации app context. Если APP_SECRET_KEY не задан —
    # backfill пропускаем (compute_client_id_hmac вернёт пустую
    # строку, UNIQUE constraint её бы отверг при N>1 row'ах).
    if not os.getenv("APP_SECRET_KEY"):
        return

    from app.security import compute_client_id_hmac

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, sub_token FROM devices "
            "WHERE sub_token IS NOT NULL AND client_id_hmac IS NULL"
        )
    ).fetchall()
    for row in rows:
        h = compute_client_id_hmac(row.sub_token)
        if not h:
            continue
        bind.execute(
            sa.text(
                "UPDATE devices SET client_id_hmac = :h WHERE id = :id"
            ),
            {"h": h, "id": row.id},
        )


def downgrade() -> None:
    op.drop_index("ix_devices_client_id_hmac", table_name="devices")
    op.drop_column("devices", "client_id_hmac")
