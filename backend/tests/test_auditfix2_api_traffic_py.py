"""Регрессия для audit-находки #38: атомарный инкремент счётчика трафика
и защита от двойного revoke на одной подписке.

Конкурентность двух коллекторов в один поток воспроизвести детерминированно
нельзя, поэтому здесь проверяем наблюдаемые следствия фикса:

* повторный over-limit отчёт по уже заблокированной подписке НЕ создаёт
  второй пакет revocation-задач (recheck статуса под ``with_for_update``);
* счётчик продолжает атомарно расти даже после блокировки (SQL-инкремент
  ``x = x + delta`` выполняется независимо от ветки revoke).
"""
from __future__ import annotations

from datetime import datetime

from app import models
from tests import factories


def _build_payload(samples: list[tuple[str, int, int]]) -> dict:
    return {
        "collected_at": datetime.utcnow().isoformat() + "Z",
        "samples": [
            {
                "access_username": u,
                "uplink_bytes": up,
                "downlink_bytes": down,
            }
            for (u, up, down) in samples
        ],
    }


def test_repeated_over_limit_report_does_not_revoke_twice(client, db_session) -> None:
    node = factories.make_node(db_session)
    config = factories.make_config(db_session, node)
    plan = factories.make_plan(db_session, traffic_limit_mb=1)  # 1 MB budget
    user = factories.make_user(db_session)
    sub = factories.make_subscription(db_session, user, plan, node)
    factories.make_device(db_session, sub, config, access_username="alice")

    payload = _build_payload([("alice", 10 * 1024 * 1024, 0)])  # 10 MiB > лимита

    first = client.post(f"/api/nodes/{node.id}/traffic", json=payload)
    assert first.status_code == 200, first.text
    first_sub = first.json()["subscriptions"][0]
    assert first_sub["over_limit"] is True
    assert first_sub["revocation_task_ids"], "первый over-limit должен отозвать устройства"

    db_session.expire_all()
    tasks_after_first = db_session.query(models.ProvisioningTask).count()

    # Повторный отчёт: подписка уже blocked → revoke не должен запуститься снова.
    second = client.post(f"/api/nodes/{node.id}/traffic", json=payload)
    assert second.status_code == 200, second.text
    second_sub = second.json()["subscriptions"][0]
    assert second_sub["over_limit"] is True
    assert second_sub["revocation_task_ids"] == []

    db_session.expire_all()
    tasks_after_second = db_session.query(models.ProvisioningTask).count()
    assert tasks_after_second == tasks_after_first, "двойного revoke быть не должно"

    # Атомарный инкремент отработал на обоих отчётах, несмотря на блокировку.
    refreshed = db_session.get(models.Subscription, sub.id)
    assert refreshed.status == models.SubscriptionStatus.blocked
    assert refreshed.traffic_used_mb == 20
