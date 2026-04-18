"""Alembic ↔ models drift + branch sanity checks.

Two failures we want CI to scream about:

1. Someone added/changed a column on a SQLAlchemy model but forgot to
   write a corresponding Alembic revision (or wrote one that doesn't
   match the model). ``MigrationContext.compare`` against ``Base.metadata``
   is exactly what ``alembic revision --autogenerate`` runs internally;
   if it produces a non-empty diff, the model and the schema disagree.

2. The revision tree has more than one head. This usually means two
   branches both added a migration, were merged together, and nobody
   ran ``alembic merge`` — so ``alembic upgrade head`` only walks one
   leg of the fork and the other migrations silently never apply.
"""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from alembic.script import ScriptDirectory

from app.db import Base, DATABASE_URL, engine
from app import models  # noqa: F401 — register all models on Base.metadata


def _alembic_config() -> Config:
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    cfg.set_main_option(
        "script_location", str(ini_path.parent / "app" / "alembic")
    )
    return cfg


def test_models_match_migrations_no_autogenerate_diff() -> None:
    """``alembic revision --autogenerate`` against head must be empty.

    The session fixture has already brought the DB to head, so any diff
    here is a model field that has no migration backing it (or vice
    versa).
    """
    with engine.connect() as conn:
        # ``compare_type=True`` matches what env.py uses in production
        # so a String→Text change isn't ignored. Without it the diff
        # is too lenient and the test would miss real drift.
        ctx = MigrationContext.configure(
            conn,
            opts={"compare_type": True, "compare_server_default": True},
        )
        diff = compare_metadata(ctx, Base.metadata)

    # ``compare_metadata`` returns a list of operation tuples. Empty
    # list = the live schema matches Base.metadata exactly.
    if diff:
        # Render diff in a way that points at the offending table /
        # column rather than dumping raw alembic op objects.
        formatted = "\n".join(f"  {op!r}" for op in diff)
        raise AssertionError(
            "Models and migrations are out of sync — run "
            "`alembic revision --autogenerate -m '...'` to fix:\n"
            f"{formatted}"
        )


def test_alembic_has_single_head() -> None:
    """A merged-but-unresolved branch leaves two heads in the tree."""
    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"Alembic tree has {len(heads)} heads ({heads}); run "
        "`alembic merge` to resolve before merging."
    )
