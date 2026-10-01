"""Fix vpnconfigprotocol enum: rename hyphenated values to underscore names.

SQLAlchemy Enum sends the Python attribute *name* (e.g. ``vless_xhttp``),
not the *value* (``vless-xhttp``). Migrations 0016 (and an earlier one for
vless-ws-cdn) accidentally added the value form. Rename them so the DB
matches what SQLAlchemy actually sends.

PostgreSQL >=10 supports ``ALTER TYPE ... RENAME VALUE``.

Revision ID: 0017_fix_enum_values
Revises: 0016_vless_xhttp_protocol
"""
from alembic import op

revision = "0017_fix_enum_values"
down_revision = "0016_vless_xhttp_protocol"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # RENAME VALUE in PostgreSQL >=10 atomically renames the enum label
    # *and* updates all rows that reference it — no separate UPDATE needed.
    # The UPDATE-first approach from the previous version failed because
    # you can't SET a column to a value that doesn't exist in the enum yet.
    #
    # На fresh DB через Base.metadata.create_all() (миграция 0001) enum
    # сразу создаётся с underscore-формой (= имена python-членов), и
    # старых hyphen-лейблов просто нет. Чтобы миграция не падала с
    # `InvalidParameterValue`, оборачиваем переименование в pg-блок,
    # пропускающий ошибку invalid_parameter_value (SQLSTATE 22023) —
    # происходит, когда исходного лейбла нет.
    op.execute(
        "DO $$ BEGIN "
        "  ALTER TYPE vpnconfigprotocol RENAME VALUE 'vless-xhttp' TO 'vless_xhttp'; "
        "EXCEPTION WHEN invalid_parameter_value THEN NULL; END $$"
    )

    # Same issue for vless-ws-cdn duplicate (original correct value is
    # vless_ws_cdn which already exists). Rename to inert label.
    op.execute(
        "DO $$ BEGIN "
        "  ALTER TYPE vpnconfigprotocol RENAME VALUE 'vless-ws-cdn' TO '_deprecated_vless_ws_cdn'; "
        "EXCEPTION WHEN invalid_parameter_value THEN NULL; END $$"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TYPE vpnconfigprotocol RENAME VALUE 'vless_xhttp' TO 'vless-xhttp'"
    )
    op.execute(
        "ALTER TYPE vpnconfigprotocol RENAME VALUE '_deprecated_vless_ws_cdn' TO 'vless-ws-cdn'"
    )
