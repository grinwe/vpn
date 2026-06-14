"""operator-routing P1 — _do_failover пишет OperatorNodeReport.

Корень пустой карты: webapp-путь «VPN не работает» (_do_failover) переселял
юзера, но НЕ создавал OperatorNodeReport (его писал только бот-флоу). Теперь
каждый user-reported failover пишет репорт (operator=None, проставляется
отдельным тапом). См. docs/operations/operator_routing_roadmap.md.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import models
from app.services.provisioning import ProvisioningOrchestrator
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def test_do_failover_creates_operator_report(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.client_control import _do_failover

    plan = make_plan(db_session)
    user = make_user(db_session)
    a = make_node(db_session, name="op-a", region="ru")
    b = make_node(db_session, name="op-b", region="ru", host="10.0.0.2")
    cfg_b = make_config(db_session, b)
    sub = make_subscription(db_session, user, plan, a)
    new_dev = make_device(db_session, sub, cfg_b, access_username="u-on-b")

    # migrate подменяем — нас интересует факт создания репорта, не ansible.
    monkeypatch.setattr(
        ProvisioningOrchestrator,
        "migrate_subscription_to_free_node",
        lambda self, s, **k: (b, new_dev, None, True),
    )

    res = _do_failover(db_session, sub, kind="user_reported", actor=f"user:{user.id}")
    assert res.action == "migrated"
    assert res.report_id is not None

    report = db_session.get(models.OperatorNodeReport, res.report_id)
    assert report is not None
    assert report.user_id == user.id
    assert report.failed_node_id == a.id          # упала старая нода
    assert report.target_node_id == b.id          # пересадили на новую
    assert report.target_access_username == "u-on-b"
    assert report.operator is None                # карьер ещё не выбран
    assert report.outcome == "pending"
