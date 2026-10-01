"""Schema migration entrypoint.

Historically this module contained a hand-rolled "applied names → function"
runner backed by a ``schema_migrations`` table. We have since moved to
Alembic (see ``backend/alembic.ini`` and ``app/alembic/versions/``). This
module stays as the single public entry point so callers — FastAPI on boot
and the RQ worker — do not need to know about the migration tool at all.

On boot we:

1. If an existing database was managed by the legacy runner (i.e.
   ``schema_migrations`` exists and contains revisions), we stamp the
   equivalent Alembic revision so ``alembic upgrade head`` becomes a no-op
   instead of re-running DDL.
2. Run ``alembic upgrade head``.

If Alembic is not installed (development without the full extras), we fall
back to the legacy code path below so that ``docker-compose up`` keeps
working during the transition.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from .db import DATABASE_URL, Base

logger = logging.getLogger(__name__)

_LEGACY_TO_ALEMBIC = {
    "0001_initial": "0001_initial",
    "0002_health_and_cloud": "0002_health_and_cloud",
}


def _backfill_from_legacy(engine: Engine) -> None:
    """If the DB used the old runner, stamp Alembic at the matching revision."""
    inspector = inspect(engine)
    if "alembic_version" in inspector.get_table_names():
        return
    if "schema_migrations" not in inspector.get_table_names():
        return

    with engine.begin() as conn:
        applied = {
            row[0]
            for row in conn.execute(text("SELECT name FROM schema_migrations"))
        }
    if not applied:
        return

    # Pick the highest legacy revision we know how to map, and stamp Alembic
    # at its equivalent.
    stamp: str | None = None
    for legacy_name, alembic_rev in _LEGACY_TO_ALEMBIC.items():
        if legacy_name in applied:
            stamp = alembic_rev
    if not stamp:
        return

    logger.info("Backfilling Alembic version from legacy runner: stamping %s", stamp)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": stamp})


def _run_alembic_upgrade() -> str | None:
    """Try to run ``alembic upgrade head``.

    Returns the actual head revision that was applied (as reported by
    Alembic itself) on success, or ``None`` if Alembic is unavailable and
    the caller should fall back to the legacy runner.
    """
    try:
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError:
        logger.warning("Alembic not installed — falling back to legacy migration runner")
        return None

    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    if not ini_path.exists():
        logger.warning("alembic.ini not found at %s — using legacy runner", ini_path)
        return None

    cfg = Config(str(ini_path))
    # env.py reads DATABASE_URL directly; we set it here too for completeness.
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    # Ensure `script_location` resolves relative to the ini file no matter
    # where the process was launched from.
    cfg.set_main_option(
        "script_location", str(ini_path.parent / "app" / "alembic")
    )
    command.upgrade(cfg, "head")
    # Читаем фактическую голову из самого Alembic, чтобы лог не врал о версии
    # (раньше здесь была захардкоженная константа, вечно отстающая от миграций).
    head = ScriptDirectory.from_config(cfg).get_current_head()
    return head or "unknown"


def _legacy_run() -> None:
    """Minimal fallback that mirrors the pre-Alembic behaviour."""
    engine = create_engine(DATABASE_URL, future=True)
    Base.metadata.create_all(bind=engine)


def run_migrations() -> None:
    # Allow operators to skip auto-migration on boot (useful when running
    # many replicas and centralising schema changes in a job).
    if os.getenv("SKIP_MIGRATIONS") == "1":
        logger.info("SKIP_MIGRATIONS=1, leaving schema untouched")
        return

    engine = create_engine(DATABASE_URL, future=True)
    try:
        _backfill_from_legacy(engine)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to backfill Alembic from legacy runner; continuing anyway")

    head = _run_alembic_upgrade()
    if head is not None:
        logger.info("Alembic migrations applied up to %s", head)
        return

    logger.warning("Using legacy migration runner (no Alembic available)")
    _legacy_run()
