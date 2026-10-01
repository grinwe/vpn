"""Аудит-фиксы в backend/app/api/nodes.py (находки 24, 25).

* Находка 24: DELETE /api/nodes/{id} должен быть атомарным. Раньше
  detach/удаление кредов коммитились ранним commit'ом внутри ``_audit``,
  и при последующем IntegrityError на ``db.delete(node)`` (ветка 409
  «fk_blocked») нода оставалась, а креды подписок уже были безвозвратно
  деактивированы/отвязаны. Теперь единственный commit — после
  ``db.delete(node)``; rollback откатывает всё, включая audit-строку.

* Находка 25: POST /nodes/{id}/configs?defer_bootstrap=true раньше терял
  backfill-креды: backfill делает только flush, а commit был только в
  ветке ``if not defer_bootstrap`` → при defer сессия закрывалась без
  commit и строки откатывались. Теперь commit безусловный.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from app import models
from tests.factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription,
    make_user,
)


def test_delete_node_fk_blocked_rolls_back_credential_detach(
    client, db_session: Session
) -> None:
    """409 fk_blocked ⇒ НИЧЕГО не изменилось: warm-кред жив, bound-кред
    по-прежнему привязан и активен, девайс не отвязан, audit-строки нет.

    FK-блок моделируем как конкурентную вставку: слушатель before_flush
    в момент, когда сессия запроса собирается удалить VPNNode, вставляет
    (в ОТДЕЛЬНОЙ транзакции) новый Credential со ссылкой на ноду —
    у credentials.node_id нет ON DELETE, поэтому DELETE vpn_nodes падает
    с IntegrityError, ровно как в проде при гонке с warm-пулом.
    """
    from app.db import SessionLocal

    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session, name="fkblk", region="ru")
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)
    sub.status = models.SubscriptionStatus.frozen  # чтобы пройти гейт active_subs
    db_session.commit()
    dev = make_device(db_session, sub, cfg, access_username="u-fk")
    bound = models.Credential(
        node_id=node.id, subscription_id=sub.id, device_id=dev.id, is_active=True,
        proto="vless-reality", config_text="enc", access_username="u-fk-c",
        pool_state=models.CredentialPoolState.assigned,
    )
    warm = models.Credential(
        node_id=node.id, is_active=True, proto="vless-reality",
        config_text="enc", access_username="warm-fk",
        pool_state=models.CredentialPoolState.warm,
    )
    db_session.add_all([bound, warm])
    db_session.commit()
    nid, cfg_id, dev_id, bound_id, warm_id = (
        node.id, cfg.id, dev.id, bound.id, warm.id,
    )

    fired = {"n": 0}

    def _inject_fk_blocker(session, flush_context, instances) -> None:
        # Срабатываем только на флаше, который реально удаляет ноду.
        if not any(isinstance(o, models.VPNNode) for o in session.deleted):
            return
        if fired["n"]:
            return
        fired["n"] += 1
        other = SessionLocal()
        try:
            other.add(
                models.Credential(
                    node_id=nid, is_active=True, proto="vless-reality",
                    config_text="enc", access_username="concurrent-blocker",
                    pool_state=models.CredentialPoolState.warm,
                )
            )
            other.commit()
        finally:
            other.close()

    event.listen(SessionLocal, "before_flush", _inject_fk_blocker)
    try:
        resp = client.delete(f"/api/nodes/{nid}")
    finally:
        event.remove(SessionLocal, "before_flush", _inject_fk_blocker)

    assert fired["n"] == 1, "FK-блокер не вставился — тест не проверил сценарий"
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "fk_blocked"

    db_session.expire_all()
    # Нода и её конфиг на месте.
    assert db_session.get(models.VPNNode, nid) is not None
    assert db_session.get(models.VPNConfig, cfg_id) is not None
    # Warm-кред НЕ удалён.
    assert db_session.get(models.Credential, warm_id) is not None
    # Bound-кред НЕ детачнут и НЕ деактивирован (раньше — терялся навсегда).
    bound_after = db_session.get(models.Credential, bound_id)
    assert bound_after is not None
    assert bound_after.node_id == nid
    assert bound_after.is_active is True
    # Девайс не отвязан от конфига.
    dev_after = db_session.get(models.Device, dev_id)
    assert dev_after is not None
    assert dev_after.config_id == cfg_id
    # Audit-строка «node_deleted» откатилась вместе с транзакцией.
    audit_rows = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "node_deleted")
        .count()
    )
    assert audit_rows == 0


def test_delete_node_success_writes_audit_row(client, db_session: Session) -> None:
    """Happy-path: после перевода audit-строки на ручной db.add() она
    по-прежнему пишется при успешном удалении (единым commit'ом)."""
    node = make_node(db_session, name="del-audit", region="ru")
    nid = node.id

    resp = client.delete(f"/api/nodes/{nid}")

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(models.VPNNode, nid) is None
    row = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.action == "node_deleted",
            models.AuditLog.target_id == nid,
        )
        .one_or_none()
    )
    assert row is not None
    assert row.extra["warm_credentials_deleted"] == 0


def test_create_config_defer_bootstrap_commits_backfill(
    client, db_session: Session
) -> None:
    """defer_bootstrap=true: backfill-креды для существующих девайсов
    должны быть ЗАКОММИЧЕНЫ (раньше терялись при закрытии сессии),
    а bootstrap-таска при этом не создаётся."""
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session, name="defer-bf", region="ru")
    cfg = make_config(db_session, node)  # существующий reality-конфиг
    sub = make_subscription(db_session, user, plan, node)  # active
    dev = make_device(db_session, sub, cfg, access_username="u-bf")
    dev_id, node_id = dev.id, node.id

    resp = client.post(
        f"/api/nodes/{node_id}/configs?defer_bootstrap=true",
        json={"name": "hy2", "protocol": "hysteria2", "port": 8443},
    )

    assert resp.status_code == 200, resp.text
    new_cfg_id = resp.json()["id"]

    # Читаем ОТДЕЛЬНОЙ сессией: видим только закоммиченное.
    db_session.expire_all()
    backfilled = (
        db_session.query(models.Credential)
        .filter(
            models.Credential.device_id == dev_id,
            models.Credential.config_id == new_cfg_id,
        )
        .all()
    )
    assert len(backfilled) == 1
    assert backfilled[0].proto == "hysteria2"
    assert backfilled[0].is_active is True
    assert backfilled[0].node_id == node_id
    # defer ⇒ bootstrap-таска не создана.
    tasks = db_session.query(models.ProvisioningTask).count()
    assert tasks == 0
