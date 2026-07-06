"""FK-индексы, NOT NULL на статус/флаг-колонках, RESTRICT на devices→subscription.

Revision ID: 0057_fk_indexes_notnull
Revises: 0056_audit_logs_indexes

Аудит #129/#130/#131. Приводит прод-схему (инкрементально мигрированную)
в соответствие с моделью:

* #129 — индексы на «горячих» FK (choose_node, выдача sub-link, удаление
  ноды/конфига) + композит ``ix_subscriptions_status_expires_at`` под
  биллинг-/экспирейшн-тики. Раньше — seq scan растущих таблиц.
* #130 — ``devices.subscription_id`` переводится с ON DELETE CASCADE на
  RESTRICT: CASCADE молча снёс бы revoked-Device со стабильным sub_token
  (нарушение sub-link инварианта). RESTRICT заставляет БД охранять его.
* #131 — статус/флаг-колонки получают NOT NULL + server_default. NULL в
  них молча выпадал из ``filter(is_active == True)`` / ``filter(status ==
  ...)`` — «сущность исчезала» из выборок без ошибки. Перед SET NOT NULL
  бэкфиллим существующие NULL дефолтом (прод-safe).

Идемпотентна: на свежей БД (0001 create_all уже применил текущую модель)
все шаги — no-op за счёт guard'ов. На инкрементальной прод-БД — доводит
недостающее.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.alembic._idempotent import has_index

revision = "0057_fk_indexes_notnull"
down_revision = "0056_audit_logs_indexes"
branch_labels = None
depends_on = None


# (table, column) → имя индекса как генерит SQLAlchemy для index=True.
_FK_INDEXES = [
    ("vpn_configs", "node_id", "ix_vpn_configs_node_id"),
    ("subscriptions", "user_id", "ix_subscriptions_user_id"),
    ("subscriptions", "plan_id", "ix_subscriptions_plan_id"),
    ("subscriptions", "node_id", "ix_subscriptions_node_id"),
    ("devices", "subscription_id", "ix_devices_subscription_id"),
    ("devices", "config_id", "ix_devices_config_id"),
    ("credentials", "subscription_id", "ix_credentials_subscription_id"),
    ("credentials", "device_id", "ix_credentials_device_id"),
    ("credentials", "config_id", "ix_credentials_config_id"),
    ("payments", "subscription_id", "ix_payments_subscription_id"),
    ("invoices", "user_id", "ix_invoices_user_id"),
    ("invoices", "subscription_id", "ix_invoices_subscription_id"),
]

# (table, column, server_default литерал). Для enum — значение члена.
_NOT_NULL_COLUMNS = [
    ("vpn_nodes", "status", "registering"),
    ("vpn_nodes", "is_active", "true"),
    ("vpn_configs", "is_enabled", "true"),
    ("plans", "max_devices", "1"),
    ("plans", "price", "0"),
    ("plans", "is_visible", "true"),
    ("subscriptions", "status", "active"),
    ("subscriptions", "traffic_used_mb", "0"),
    ("subscriptions", "auto_renew", "false"),
    ("devices", "status", "pending"),
    ("credentials", "is_active", "true"),
    ("payments", "amount", "0"),
    ("payments", "status", "pending"),
    ("invoices", "status", "pending"),
    ("cloud_providers", "is_active", "true"),
]


def _column_is_nullable(bind, table: str, column: str) -> bool:
    row = bind.execute(
        sa.text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return row is not None and row[0] == "YES"


def _fk_delete_rule(bind, constraint: str) -> str | None:
    row = bind.execute(
        sa.text(
            "SELECT rc.delete_rule FROM information_schema.referential_constraints rc "
            "WHERE rc.constraint_name = :n"
        ),
        {"n": constraint},
    ).fetchone()
    return row[0] if row else None


def upgrade() -> None:
    bind = op.get_bind()

    # #129 — FK-индексы (guard: не создаём, если уже есть).
    for table, column, name in _FK_INDEXES:
        if not has_index(table, name):
            op.create_index(name, table, [column])
    if not has_index("subscriptions", "ix_subscriptions_status_expires_at"):
        op.create_index(
            "ix_subscriptions_status_expires_at",
            "subscriptions",
            ["status", "expires_at"],
        )

    # #131 — NOT NULL + server_default (бэкфилл NULL перед SET NOT NULL).
    # ``default`` — доверенные литералы из _NOT_NULL_COLUMNS (не польз. ввод),
    # но бэкфилл-значение всё равно передаём параметром через bind.execute
    # (op.execute не принимает bind-параметры).
    for table, column, default in _NOT_NULL_COLUMNS:
        backfill = default == "true" if default in ("true", "false") else default
        bind.execute(
            sa.text(f'UPDATE "{table}" SET "{column}" = :d WHERE "{column}" IS NULL'),
            {"d": backfill},
        )
        op.execute(
            f"ALTER TABLE \"{table}\" ALTER COLUMN \"{column}\" SET DEFAULT '{default}'"
        )
        if _column_is_nullable(bind, table, column):
            op.execute(
                f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET NOT NULL'
            )

    # #130 — devices.subscription_id: CASCADE → RESTRICT.
    if _fk_delete_rule(bind, "devices_subscription_id_fkey") == "CASCADE":
        op.drop_constraint(
            "devices_subscription_id_fkey", "devices", type_="foreignkey"
        )
        op.create_foreign_key(
            "devices_subscription_id_fkey",
            "devices",
            "subscriptions",
            ["subscription_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _fk_delete_rule(bind, "devices_subscription_id_fkey") == "RESTRICT":
        op.drop_constraint(
            "devices_subscription_id_fkey", "devices", type_="foreignkey"
        )
        op.create_foreign_key(
            "devices_subscription_id_fkey",
            "devices",
            "subscriptions",
            ["subscription_id"],
            ["id"],
            ondelete="CASCADE",
        )
    # NOT NULL / server_default и FK-индексы намеренно НЕ откатываем:
    # они безопасны и полезны, downgrade оставляет их (частичный откат
    # допустим — эти изменения аддитивны и не мешают старой схеме).
