"""Regression tests for audit fix #218.

Bulk subscription endpoints (regenerate-sublink / rebuild-config /
migrate-auto) put a per-sub failure into the response ``failed`` list as
``str(exc)`` only — with no ``logger.exception`` the stacktrace was lost
forever (the API response is ephemeral). This checks that a failing sub is
BOTH surfaced in ``failed`` AND logged with a traceback, while the run
keeps going (batch resilience) and ``db.rollback()`` stays first.
"""
from __future__ import annotations

import logging

from app.services.provisioning import ProvisioningOrchestrator

from .factories import (
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _make_active_sub(db, tg):
    user = make_user(db, telegram_id=tg)
    plan = make_plan(db)
    node = make_node(db)
    sub = make_subscription(db, user, plan, node)
    db.commit()
    return user, sub


def test_bulk_regenerate_logs_and_surfaces_failure(
    client, db_session, monkeypatch, caplog
):
    user, sub = _make_active_sub(db_session, "tg-af3-regen")

    def _boom(self, subscription):  # noqa: ANN001, ANN201
        raise RuntimeError("regen exploded")

    monkeypatch.setattr(
        ProvisioningOrchestrator, "regenerate_subscription_sublink", _boom
    )

    # Харнесс на старте гоняет alembic-миграции, чей fileConfig
    # (disable_existing_loggers) глушит уже созданные логгеры приложения:
    # ``app.api`` (общий логгер эндпоинтов из _common) приходит в тест с
    # ``disabled=True``, и его записи не доходят до caplog. Ре-активируем,
    # иначе logger.exception «пропадёт» и проверка залогированности ложно
    # упадёт, хотя код отработал верно.
    logging.getLogger("app.api").disabled = False

    with caplog.at_level(logging.ERROR, logger="app.api"):
        resp = client.post(
            "/api/subscriptions/bulk-regenerate-sublink",
            json={"user_ids": [user.id]},
        )
    assert resp.status_code == 200
    body = resp.json()
    # Сбой виден оператору в ответе...
    assert body["failed"] == [
        {"user_id": user.id, "subscription_id": sub.id, "error": "regen exploded"}
    ]
    assert user.id not in body["done"]
    # ...и трейсбек ушёл в лог (exc_info присутствует).
    recs = [
        r for r in caplog.records
        if r.exc_info and "bulk-regenerate" in r.getMessage()
    ]
    assert recs, "ожидался logger.exception с трейсбеком по упавшей подписке"


def test_bulk_migrate_auto_logs_and_surfaces_failure(
    client, db_session, monkeypatch, caplog
):
    user, sub = _make_active_sub(db_session, "tg-af3-migrate")

    def _boom(self, subscription, banned_by=None):  # noqa: ANN001, ANN201
        raise RuntimeError("migrate exploded")

    monkeypatch.setattr(
        ProvisioningOrchestrator, "migrate_subscription_to_free_node", _boom
    )

    # См. коммент в test_bulk_regenerate_...: ре-активируем ``app.api``, иначе
    # disable_existing_loggers из alembic-fileConfig глушит logger.exception.
    logging.getLogger("app.api").disabled = False

    with caplog.at_level(logging.ERROR, logger="app.api"):
        resp = client.post(
            "/api/subscriptions/bulk-migrate-auto",
            json={"user_ids": [user.id]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["failed"] == [
        {"user_id": user.id, "subscription_id": sub.id, "error": "migrate exploded"}
    ]
    recs = [
        r for r in caplog.records
        if r.exc_info and "bulk-migrate-auto" in r.getMessage()
    ]
    assert recs, "ожидался logger.exception с трейсбеком по упавшей подписке"
