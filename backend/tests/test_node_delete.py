"""DELETE /api/nodes/{id} — удаление ЗАПИСИ ноды из панели (без хостера).

Проверяем поведение гейтов и сохранность данных:
* активная подписка → 409 active_subs (фронт предлагает migrate→retry);
* ЗАМОРОЖЕННАЯ подписка НЕ блокирует (раньше блокировала, а migrate её не
  двигал → нода была неудаляемой) — нода сносится, sub выживает с node_id=NULL;
* живой VPS у хостера → 409 live_vm, обходится ?force=true;
* warm-креды удаляются, привязанные креды детачатся (node_id→NULL,
  is_active→False — мёртвая нода уходит из sub-link'а), подписки/девайсы выживают.
Гоняется в CI (postgres — FK SET NULL/CASCADE на уровне БД).
"""
from __future__ import annotations

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


def test_delete_node_active_sub_blocks_409(client, db_session: Session) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session, name="del-active", region="ru")
    make_subscription(db_session, user, plan, node)  # status=active, node_id=node

    resp = client.delete(f"/api/nodes/{node.id}")

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "active_subs"
    assert detail["active_subs"] == 1
    # нода на месте
    db_session.expire_all()
    assert db_session.get(models.VPNNode, node.id) is not None


def test_delete_node_frozen_sub_does_not_block_and_preserves_data(
    client, db_session: Session
) -> None:
    plan = make_plan(db_session)
    user = make_user(db_session)
    node = make_node(db_session, name="del-frozen", region="ru")
    cfg = make_config(db_session, node)
    sub = make_subscription(db_session, user, plan, node)
    sub.status = models.SubscriptionStatus.frozen
    db_session.commit()
    dev = make_device(db_session, sub, cfg, access_username="u")
    # привязанный кред (подписка) + warm-кред (пул) на той же ноде
    bound = models.Credential(
        node_id=node.id, subscription_id=sub.id, device_id=dev.id, is_active=True,
        proto="vless-reality", config_text="enc", access_username="u-c",
        pool_state=models.CredentialPoolState.assigned,
    )
    warm = models.Credential(
        node_id=node.id, is_active=True, proto="vless-reality",
        config_text="enc", access_username="warm-x",
        pool_state=models.CredentialPoolState.warm,
    )
    db_session.add_all([bound, warm])
    db_session.commit()
    nid, sid, did, bound_id, warm_id = node.id, sub.id, dev.id, bound.id, warm.id

    resp = client.delete(f"/api/nodes/{nid}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["warm_credentials_deleted"] == 1
    assert body["bound_credentials_detached"] == 1
    assert body["devices_detached"] == 1

    db_session.expire_all()
    # нода и warm-кред удалены
    assert db_session.get(models.VPNNode, nid) is None
    assert db_session.get(models.Credential, warm_id) is None
    # привязанный кред ДЕТАЧЕН: node_id обнулён + деактивирован (уходит из sub-link)
    bound_after = db_session.get(models.Credential, bound_id)
    assert bound_after is not None
    assert bound_after.node_id is None
    assert bound_after.is_active is False
    # подписка ВЫЖИЛА (frozen), node_id обнулён через SET NULL
    surviving = db_session.get(models.Subscription, sid)
    assert surviving is not None
    assert surviving.node_id is None
    assert surviving.status == models.SubscriptionStatus.frozen
    # девайс ВЫЖИЛ, config_id обнулён (config каскадно удалён вместе с нодой)
    dev_after = db_session.get(models.Device, did)
    assert dev_after is not None
    assert dev_after.config_id is None


def test_delete_node_live_vm_blocks_without_force(client, db_session: Session) -> None:
    node = make_node(db_session, name="del-livevm", region="ru")
    node.provider_external_id = "vm-123"  # живой VPS у хостера
    db_session.commit()
    nid = node.id

    resp = client.delete(f"/api/nodes/{nid}")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "live_vm"
    db_session.expire_all()
    assert db_session.get(models.VPNNode, nid) is not None

    # force обходит гейт живого VPS (запись сносится, хостер не трогается)
    resp = client.delete(f"/api/nodes/{nid}?force=true")
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(models.VPNNode, nid) is None
