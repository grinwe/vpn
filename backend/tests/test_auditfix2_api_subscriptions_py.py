"""Regression tests for audit fix #213.

``enable_subscription`` must surface a failed reprovision to the caller
instead of silently returning 200 as if everything is fine — otherwise
the operator marks the ticket resolved while the user has an ``active``
subscription with no working config (sub-link returns 503).
"""
from __future__ import annotations

from app import models
from app.services.provisioning import ProvisioningOrchestrator

from .factories import (
    make_config,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def _make_blocked_sub(db):
    user = make_user(db, telegram_id="tg-enable-1")
    plan = make_plan(db)
    node = make_node(db)
    # У ноды ДОЛЖЕН быть включённый конфиг, иначе reprovision_subscription
    # честно падает с RuntimeError("No enabled VPN configs found for node")
    # и happy-path сигналил бы ложный сбой. С конфигом cold-path проходит:
    # provisioning замокан (run_task_async → no-op) на уровне conftest, но
    # reprovision_subscription сам по себе отрабатывает и возвращает device+task.
    make_config(db, node)
    sub = make_subscription(db, user, plan, node)
    sub.status = models.SubscriptionStatus.blocked
    db.add(sub)
    db.commit()
    return sub


def test_enable_reprovision_ok_flag_false(client, db_session):
    """Happy path: reprovision succeeds (noop in tests) → no failure flag."""
    sub = _make_blocked_sub(db_session)

    resp = client.post(f"/api/subscriptions/{sub.id}/enable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["reprovision_failed"] is False


def test_enable_reprovision_failure_is_signaled(client, db_session, monkeypatch):
    """Reprovision blows up → sub stays active but response flags the failure."""
    sub = _make_blocked_sub(db_session)

    def _boom(self, subscription):  # noqa: ANN001, ANN201
        raise RuntimeError("ansible exploded")

    monkeypatch.setattr(
        ProvisioningOrchestrator, "reprovision_subscription", _boom
    )

    resp = client.post(f"/api/subscriptions/{sub.id}/enable")
    assert resp.status_code == 200
    body = resp.json()
    # Подписка всё равно активна — фикс не меняет это поведение...
    assert body["status"] == "active"
    # ...но факт сбоя теперь виден вызывающему.
    assert body["reprovision_failed"] is True
