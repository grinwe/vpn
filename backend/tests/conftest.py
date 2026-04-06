"""Integration test fixtures.

Design notes:

* Tests run against a **real** Postgres, not SQLite or an in-memory stub.
  Several of our migrations and models use JSONB, enum types, and
  ``ON DELETE CASCADE`` constraints that are not portable to SQLite, and
  catching Alembic / SQLAlchemy regressions against a real database is
  the entire point of this suite.
* ``DATABASE_URL`` (and a handful of other required env vars) must be
  set *before* ``app.main`` is imported, because import-time code in
  ``app/db.py`` refuses to load without it. The fixtures here set
  defaults pointing at ``postgres://postgres:postgres@localhost:5432/vpn_test``
  if nothing is exported — matching the service container we bring up
  in ``.github/workflows/ci.yml``.
* Between tests we ``TRUNCATE`` every application table rather than
  drop-and-recreate the schema. Truncation is ~100x faster and still
  restarts identity sequences so primary keys are stable.
* ``ProvisioningOrchestrator.run_task_async`` is monkey-patched to a
  no-op. The code under test still creates ``ProvisioningTask`` rows and
  all accompanying DB state, so we can assert on *what would have been
  provisioned* without actually touching Redis or ansible.
"""
from __future__ import annotations

import os
from typing import Iterator

import pytest

# ---------------------------------------------------------------------------
# Environment — MUST be set before importing the app.
# ---------------------------------------------------------------------------
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@localhost:5432/vpn_test",
)
os.environ.setdefault("ADMIN_API_TOKEN", "test-admin-token")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-used-in-prod")
# A missing Redis would cause run_task_async to raise; we disable the
# guard because the monkey-patch below makes it moot, but the env var
# also keeps import-time code paths happy.
os.environ.setdefault("ALLOW_INPROCESS_PROVISIONING", "1")


# ---------------------------------------------------------------------------
# Session-scoped: bring the schema up to head exactly once.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _prepare_database() -> Iterator[None]:
    from sqlalchemy import text

    from app.db import Base, engine  # noqa: F401  (ensures models are registered)
    from app.migrations import run_migrations

    # Wipe anything a previous run left behind. We drop everything,
    # including the alembic_version table, so the upgrade path runs
    # from zero — which is also what we want for the alembic round-trip
    # test.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    run_migrations()
    yield


# ---------------------------------------------------------------------------
# Function-scoped: truncate all tables between tests.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    from sqlalchemy import text

    from app.db import Base, engine

    yield

    table_names = [t.name for t in reversed(Base.metadata.sorted_tables)]
    if not table_names:
        return
    quoted = ", ".join(f'"{name}"' for name in table_names)
    with engine.begin() as conn:
        # RESTART IDENTITY so primary keys are deterministic across tests.
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))


# ---------------------------------------------------------------------------
# No ansible, no Redis. Any code path that enqueues a provisioning task
# gets silently noop'd.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.provisioning import ProvisioningOrchestrator

    def _noop(self, task, node=None):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ProvisioningOrchestrator, "run_task_async", _noop)


# ---------------------------------------------------------------------------
# HTTP client bound to the FastAPI app, with the admin token pre-set.
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.headers.update({"X-Admin-Token": os.environ["ADMIN_API_TOKEN"]})
        yield c


@pytest.fixture
def db_session():
    """A plain SQLAlchemy session for tests that want to poke the DB
    directly. Commits are per-test; the outer ``_clean_tables`` fixture
    truncates everything afterwards so there's no bleed."""
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
