"""Lightweight versioned migration runner.

This is intentionally minimal but provides deterministic versioning
and a path to evolve the schema without dropping data.
"""
from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine
from sqlalchemy.engine import Engine

from .db import DATABASE_URL, Base

MIGRATIONS: list[tuple[str, callable[[Engine], None]]] = []


def _register_migration(name: str):
    def decorator(func):
        MIGRATIONS.append((name, func))
        return func

    return decorator


@_register_migration("0001_initial")
def _initial_schema(engine: Engine) -> None:
    Base.metadata.create_all(bind=engine)


def _ensure_schema_table(engine: Engine, metadata: MetaData) -> Table:
    table = Table(
        "schema_migrations",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String, unique=True, nullable=False),
    )
    metadata.create_all(engine)
    return table


def run_migrations() -> None:
    engine = create_engine(DATABASE_URL, future=True)
    metadata = MetaData()
    table = _ensure_schema_table(engine, metadata)

    with engine.begin() as conn:
        applied = {row.name for row in conn.execute(table.select())}
        for name, func in MIGRATIONS:
            if name in applied:
                continue
            func(engine)
            conn.execute(table.insert().values(name=name))

