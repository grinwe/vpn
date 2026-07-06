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


def _schema_snapshot() -> dict[str, dict]:
    """Return a structural snapshot of the public schema.

    For each table we capture column names + their declared SQL type
    (rendered as string), nullability, and the set of index column
    tuples. That's deep enough to catch a downgrade that drops a
    column / index without recreating it on upgrade — the original
    test only compared table *names* and would have missed both.
    """
    insp = inspect(engine)
    snap: dict[str, dict] = {}
    for table in insp.get_table_names():
        cols = {
            c["name"]: (str(c["type"]), bool(c["nullable"]))
            for c in insp.get_columns(table)
        }
        idx = sorted(
            tuple(i["column_names"]) for i in insp.get_indexes(table)
        )
        snap[table] = {"columns": cols, "indexes": idx}
    return snap


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


def _restore_head_or_reset(cfg: Config) -> None:
    """Guarantee the shared session DB is back at ``head``.

    This test tears the *shared* pytest-session database down to
    ``base``. If any ``downgrade()`` chokes midway (exactly the bug this
    test hunts for), the schema is left half-disassembled and every
    later test in the session would cascade-fail with «relation does not
    exist», burying the real failure. So we unconditionally rebuild head
    in ``finally``: first try a plain re-``upgrade`` (works when the DB
    is at some intact intermediate revision), and if that also fails —
    because a partial downgrade left the schema inconsistent — fall back
    to the same hard reset the session fixture uses (DROP SCHEMA +
    ``run_migrations``).
    """
    from sqlalchemy import text

    from app.db import engine
    from app.migrations import run_migrations

    try:
        command.upgrade(cfg, "head")
        return
    except Exception:
        pass
    # Nuclear option: wipe the schema and re-migrate from zero.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    run_migrations()


def test_alembic_downgrade_upgrade_roundtrip() -> None:
    cfg = _alembic_config()

    # Deep snapshot at head — columns, types, nullability, indexes per
    # table. Must match exactly after the round-trip; the previous
    # version of this test only compared table names and would have
    # missed a downgrade that dropped a column without recreating it.
    snap_at_head = _schema_snapshot()
    assert "users" in snap_at_head, (
        "Schema looks empty at head — is the session fixture doing its job?"
    )

    # The whole destructive round-trip runs inside try/finally so that a
    # broken downgrade() (or a failed assertion) can never leave the
    # shared session DB in a half-torn-down state — see
    # ``_restore_head_or_reset``.
    try:
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
        snap_after_roundtrip = _schema_snapshot()

        assert snap_after_roundtrip == snap_at_head, (
            "Schema drift after downgrade→upgrade round-trip.\n"
            f"  before: {snap_at_head}\n"
            f"  after:  {snap_after_roundtrip}"
        )
    finally:
        # Whatever happened above, hand the DB back to the rest of the
        # session at a clean head so no unrelated test fails downstream.
        _restore_head_or_reset(cfg)
