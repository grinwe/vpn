"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-04-06

Rather than hand-writing every ``CREATE TABLE`` statement, this revision
delegates to ``Base.metadata.create_all(checkfirst=True)``. That keeps the
declarative models the single source of truth for the initial schema and
means new models automatically join the initial revision when cutting a
fresh deployment. Subsequent revisions (``0002_...`` and later) contain
hand-written ``ALTER TABLE``/``CREATE TABLE`` statements so they remain
precise and reviewable.
"""
from __future__ import annotations

from alembic import op

from app.db import Base
from app import models  # noqa: F401 — register models

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
