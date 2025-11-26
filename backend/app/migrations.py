"""Lightweight migration runner.

This project is expected to use Alembic later, but for now
`run_migrations()` ensures all SQLAlchemy models are created.
"""
from __future__ import annotations

from sqlalchemy import create_engine

from .db import DATABASE_URL, Base


def run_migrations() -> None:
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(bind=engine)

