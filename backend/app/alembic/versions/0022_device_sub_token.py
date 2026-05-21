"""Add per-device sub_token for credential isolation.

Revision ID: 0022_device_sub_token
Revises: 0021_payment_unique_provider_external_id

Each Device gets its own sub_token so /sub/{token} returns only that
device's credentials. Prevents credential sharing: sharing a link
exposes one device, not the entire subscription.

Backfills existing devices with unique tokens (secrets.token_urlsafe).
"""
from __future__ import annotations

import secrets

from alembic import op
from sqlalchemy import Column, String, text

from app.alembic._idempotent import has_column, has_index

revision = "0022_device_sub_token"
down_revision = "0021_payment_unique_provider_external_id"


def upgrade() -> None:
    # 1. Add nullable column first (can't add UNIQUE + NOT NULL in one step
    #    when existing rows have no value). has_column-guard: 0001
    #    create_all() уже создаёт колонку из модели.
    if not has_column("devices", "sub_token"):
        op.add_column("devices", Column("sub_token", String, nullable=True))

    # 2. Backfill existing devices with unique tokens. NULL only — на
    #    fresh DB колонка уже есть, но строк нет, no-op. На исторической
    #    DB апдейтит только те, что без токена.
    bind = op.get_bind()
    rows = bind.execute(
        text("SELECT id FROM devices WHERE sub_token IS NULL")
    ).fetchall()
    for (device_id,) in rows:
        token = secrets.token_urlsafe(32)
        bind.execute(
            text("UPDATE devices SET sub_token = :token WHERE id = :id"),
            {"token": token, "id": device_id},
        )

    # 3. Add unique index. has_index-guard.
    if not has_index("devices", "ix_devices_sub_token"):
        op.create_index(
            "ix_devices_sub_token", "devices", ["sub_token"], unique=True
        )


def downgrade() -> None:
    op.drop_index("ix_devices_sub_token", table_name="devices")
    op.drop_column("devices", "sub_token")
