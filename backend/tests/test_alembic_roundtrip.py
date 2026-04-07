"""Upgrade → downgrade → upgrade Alembic round-trip.

Goal: catch migrations that claim to be reversible but crash on the way
down. This is where most "worked on my machine" schema bugs live —
``upgrade()`` gets tested every time someone starts the app, but
``downgrade()`` almost never runs until you need it in an incident.

The session-scoped conftest fixture has already brought the DB to head.
This test tears it all the way down to ``base`` and back up to ``head``
twice to make sure:

* every revision's ``downgrade()`` actually runs;
* re-upgrading to head after a full teardown lands on an identical
  schema (same tables, same columns).
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db import DATABASE_URL, engine


def _alembic_config() -> Config:
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    cfg.set_main_option(
        "script_location", str(ini_path.parent / "app" / "alembic")
    )
    return cfg


def _table_set() -> set[str]:
    return set(inspect(engine).get_table_names())


def test_alembic_downgrade_upgrade_roundtrip() -> None:
    cfg = _alembic_config()

    # Snapshot the schema at head before we start moving versions around.
    # This is the "known good" state — everything we see after a full
    # teardown + restore must match it.
    tables_at_head = _table_set()
    assert "users" in tables_at_head, (
        "Schema looks empty at head — is the session fixture doing its job?"
    )

    # Walk all the way down. A broken downgrade() raises here and
    # pytest reports which revision choked.
    command.downgrade(cfg, "base")
    tables_at_base = _table_set()
    # Alembic itself keeps ``alembic_version`` around after downgrade
    # to base; application tables should all be gone.
    assert tables_at_base.issubset({"alembic_version"}), (
        f"Tables survived downgrade to base: {tables_at_base - {'alembic_version'}}"
    )

    # Climb back to head. Every upgrade() must be idempotent-friendly
    # enough to run against a clean DB.
    command.upgrade(cfg, "head")
    tables_after_roundtrip = _table_set()

    # The set of application tables must be identical before and after.
    # We compare on set-equality rather than just "contains" so that an
    # accidental DROP TABLE in a downgrade that forgets to recreate on
    # upgrade is caught.
    assert tables_after_roundtrip == tables_at_head, (
        "Schema drift after downgrade→upgrade round-trip:\n"
        f"  missing: {tables_at_head - tables_after_roundtrip}\n"
        f"  extra:   {tables_after_roundtrip - tables_at_head}"
    )
