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
os.environ.setdefault("WEBAPP_JWT_SECRET", "test-webapp-jwt-secret")

# ---------------------------------------------------------------------------
# Изоляция от боевого/dev-окружения разработчика (находка 194).
#
# Стартовый хук приложения (@app.on_event("startup")) зовёт
# register_webhook(); он реально дёргает Telegram setWebhook, если заданы
# TELEGRAM_WEBHOOK_URL + TELEGRAM_WEBHOOK_SECRET_TOKEN + BOT_TOKEN. Точно так
# же admin_notify читает ADMIN_TELEGRAM_IDS и может слать реальные алерты.
# Если у разработчика эти переменные экспортированы (боевой бот), запуск
# pytest молча перенастроил бы webhook живого бота на dev-URL. Чистим их ДО
# импорта app.main (get_settings() кэширует значения при первом импорте),
# т.е. на уровне модуля conftest — раньше любой фикстуры.
for _net_var in (
    "BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_URL",
    "TELEGRAM_WEBHOOK_SECRET_TOKEN",
    "ADMIN_TELEGRAM_IDS",
):
    os.environ.pop(_net_var, None)


# ---------------------------------------------------------------------------
# Регистрация кастомных маркеров (чтобы --strict-markers не ругался и маркер
# был виден в `pytest --markers`).
# ---------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_provisioning: не глушить ProvisioningOrchestrator.run_task_async "
        "(opt-out из autouse-фикстуры _no_provisioning) — для тестов, которым "
        "нужно реальное исполнение стейт-машины таски.",
    )


# ---------------------------------------------------------------------------
# Session-scoped: bring the schema up to head exactly once.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _prepare_database() -> Iterator[None]:
    from sqlalchemy import text

    from app.db import Base, engine  # noqa: F401  (ensures models are registered)
    from app.migrations import run_migrations

    # Предохранитель (находка 192): DROP SCHEMA public CASCADE безусловно
    # сносит ВСЮ базу из DATABASE_URL. Если разработчик экспортировал
    # DATABASE_URL от dev/staging-базы с данными, запуск pytest уничтожил бы
    # её целиком. Разрешаем wipe только если имя БД похоже на тестовое
    # (содержит "test") — либо явно разрешено через TESTS_ALLOW_DB_WIPE=1.
    _db_name = (engine.url.database or "")
    _wipe_ok = os.getenv("TESTS_ALLOW_DB_WIPE", "").lower() in {"1", "true", "yes"}
    if not _wipe_ok and "test" not in _db_name.lower():
        pytest.exit(
            "Отказ выполнять DROP SCHEMA public CASCADE: имя базы "
            f"{_db_name!r} из DATABASE_URL не похоже на тестовое (нет 'test'). "
            "Тесты сносят схему целиком. Укажите тестовую базу (напр. "
            "vpn_test) или явно разрешите очистку через TESTS_ALLOW_DB_WIPE=1.",
            returncode=1,
        )

    # Wipe anything a previous run left behind. We drop everything,
    # including the alembic_version table, so the upgrade path runs
    # from zero — which is also what we want for the alembic round-trip
    # test.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    run_migrations()

    # Одноразовая очистка сид-данных (находка 198): миграция 0007_seed_plans
    # делает INSERT в plans при upgrade. Чистим их ЗДЕСЬ, один раз после
    # прогона миграций, чтобы самый первый тест сессии стартовал с пустой БД
    # — а не платить за это двойным TRUNCATE в каждом из ~700 тестов (это
    # удваивало время сьюта). Дальше чистоту держит per-test TRUNCATE после
    # каждого теста (см. _clean_tables). Тесты, которым нужны планы, создают
    # их сами через factories.make_plan.
    _truncate_all_tables()
    yield


def _truncate_all_tables() -> None:
    """TRUNCATE всех прикладных таблиц с RESTART IDENTITY (детерминированные PK)."""
    from sqlalchemy import text

    from app.db import Base, engine

    table_names = [t.name for t in reversed(Base.metadata.sorted_tables)]
    if not table_names:
        return
    quoted = ", ".join(f'"{name}"' for name in table_names)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))


# ---------------------------------------------------------------------------
# Function-scoped: truncate all tables after each test.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    # Чистим ПОСЛЕ теста: сид-данные уже сняты один раз в _prepare_database,
    # поэтому каждый тест (включая первый) стартует с пустой БД без двойного
    # TRUNCATE на каждый тест. Тесты создают нужные данные через factories.
    yield
    _truncate_all_tables()


# ---------------------------------------------------------------------------
# No ansible, no Redis. Any code path that enqueues a provisioning task
# gets silently noop'd.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_provisioning(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Opt-out (находка 188): тест, помеченный @pytest.mark.real_provisioning,
    # получает НЕглушёный run_task_async — чтобы прогнать реальную стейт-машину
    # исполнения таски (run_task/_execute_task/_handle_task_outcome). Такие
    # тесты обязаны сами замокать ansible (напр. _execute_task), иначе полезут
    # в сеть.
    if request.node.get_closest_marker("real_provisioning") is not None:
        return

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
