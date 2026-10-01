"""Helpers for idempotent Alembic migrations.

Why this exists: `0001_initial.py` bootstraps schema via
``Base.metadata.create_all()`` — that takes the *current* model state,
which includes columns and tables added by later migrations. So on a
fresh DB, 0001 creates everything immediately, and subsequent migrations
(0008, 0009, 0015, ...) blow up with "column/table/index already exists"
when they try to `ADD COLUMN` / `CREATE TABLE` / `CREATE INDEX` that's
already there.

The fix: each schema-additive operation in those migrations checks the
DB state first via SQLAlchemy's Inspector and skips if the target is
already present. Same code path is safe on:
* fresh DB (0001 created everything via create_all → guards skip later migs)
* historical DB (migrated incrementally up to some point → guards add what's missing)
* partially-failed DB (some columns half-applied → guards converge)

Usage:
    from app.alembic._idempotent import has_column, has_table, has_index

    def upgrade():
        if not has_column("credentials", "pool_state"):
            op.add_column("credentials", sa.Column("pool_state", ...))
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


def _inspector():
    return inspect(op.get_bind())


def has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def has_column(table: str, column: str) -> bool:
    if not has_table(table):
        return False
    return column in {c["name"] for c in _inspector().get_columns(table)}


def has_index(table: str, index_name: str) -> bool:
    if not has_table(table):
        return False
    return index_name in {i["name"] for i in _inspector().get_indexes(table)}


def has_unique_constraint(table: str, constraint_name: str) -> bool:
    if not has_table(table):
        return False
    return constraint_name in {
        c["name"] for c in _inspector().get_unique_constraints(table)
    }


def has_foreign_key(table: str, constraint_name: str) -> bool:
    if not has_table(table):
        return False
    return constraint_name in {
        c["name"] for c in _inspector().get_foreign_keys(table)
    }
