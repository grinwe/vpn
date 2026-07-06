"""Аудит-фикс #187: страхующие тесты sub-link инварианта (`GET /api/sub/{token}`).

Load-bearing блок в ``api_extensions.dynamic_sub_link`` («DO NOT TOUCH»,
seamless-migration alias) до этого не выполнялся ни одним тестом — существующий
``test_subscription_link.py`` ходит только legacy-токеном подписки. Здесь
закрываем per-device ветку целиком:

1. happy-path per-device: токен девайса отдаёт ТОЛЬКО его credentials;
2. alias: отозванный Device по СТАРОМУ токену отдаёт конфиг живого соседа,
   в AuditLog пишется ``aliased_to_device_id``;
3. цепочка миграций A→B→C: alias выбирает самого свежего (updated_at)
   живого соседа, а не первый попавшийся;
4. защита от пустой выдачи: все девайсы мертвы → 503, а не пустой 200
   (пустой 200 затирает кэшированный профиль у клиента).

Инвариант описан в docs/components/backend-api.md, раздел «Sub-link invariant».
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta

from app import models

from .factories import make_config, make_node, make_plan, make_subscription, make_user


def _mk_device(
    db,
    sub: models.Subscription,
    cfg: models.VPNConfig,
    *,
    token: str,
    uri: str,
    status: models.DeviceStatus = models.DeviceStatus.active,
    cred_active: bool = True,
    name: str = "primary",
) -> models.Device:
    """Device + один credential с plain-URI (decrypt отдаёт его as-is)."""
    device = models.Device(
        user_id=sub.user_id,
        subscription_id=sub.id,
        config_id=cfg.id,
        name=name,
        status=status,
        access_username=f"u-{sub.id}-{token}",
        sub_token=token,
    )
    db.add(device)
    db.flush()
    db.add(
        models.Credential(
            subscription_id=sub.id,
            device_id=device.id,
            config_id=cfg.id,
            node_id=cfg.node_id,
            proto="vless-reality",
            config_text=uri,
            is_active=cred_active,
        )
    )
    db.commit()
    db.refresh(device)
    return device


def _setup(db):
    node = make_node(db)
    cfg = make_config(db, node)
    user = make_user(db)
    plan = make_plan(db)
    sub = make_subscription(db, user, plan, node)
    return sub, cfg


def _body(resp) -> str:
    return base64.b64decode(resp.text).decode()


def test_per_device_token_returns_only_that_device(client, db_session):
    """Основной путь новых клиентов: Device.sub_token → только его конфиги."""
    sub, cfg = _setup(db_session)
    dev_a = _mk_device(db_session, sub, cfg, token="tok-dev-a", uri="vless://device-a")
    _mk_device(db_session, sub, cfg, token="tok-dev-b", uri="vless://device-b", name="second")

    resp = client.get(f"/api/sub/{dev_a.sub_token}", headers={})
    assert resp.status_code == 200
    lines = [line for line in _body(resp).splitlines() if line]
    # Токен девайса A не должен раскрывать credentials девайса B.
    assert lines == ["vless://device-a"]

    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="subscription_fetch", target_type="device", target_id=dev_a.id)
        .first()
    )
    assert audit is not None
    assert audit.extra.get("device_token") is True
    # Без миграции alias не срабатывает.
    assert audit.extra.get("aliased_to_device_id") is None


def test_revoked_device_token_aliases_to_live_sibling(client, db_session):
    """Seamless-migration alias: старый токен продолжает отдавать конфиг
    живого соседа по той же подписке (инвариант «DO NOT TOUCH»)."""
    sub, cfg = _setup(db_session)
    old = _mk_device(
        db_session, sub, cfg,
        token="tok-old", uri="vless://old-node",
        status=models.DeviceStatus.revoked, cred_active=False,
    )
    fresh = _mk_device(
        db_session, sub, cfg,
        token="tok-fresh", uri="vless://fresh-node", name="migrated",
    )

    # Сохранённый в клиенте СТАРЫЙ URL после миграции жив и отдаёт нового соседа.
    resp = client.get(f"/api/sub/{old.sub_token}", headers={})
    assert resp.status_code == 200
    assert _body(resp).strip() == "vless://fresh-node"

    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="subscription_fetch", target_type="device", target_id=old.id)
        .first()
    )
    assert audit is not None
    assert audit.extra.get("aliased_to_device_id") == fresh.id


def test_alias_chain_picks_most_recent_live_sibling(client, db_session):
    """Цепочка миграций A→B→C: alias указывает на самого свежего по
    updated_at живого соседа (C), а не на застрявший B."""
    sub, cfg = _setup(db_session)
    dev_a = _mk_device(
        db_session, sub, cfg,
        token="tok-a", uri="vless://node-a",
        status=models.DeviceStatus.revoked, cred_active=False,
    )
    dev_b = _mk_device(db_session, sub, cfg, token="tok-b", uri="vless://node-b", name="b")
    dev_c = _mk_device(db_session, sub, cfg, token="tok-c", uri="vless://node-c", name="c")

    # Bulk-update обходит onupdate=utcnow — фиксируем детерминированный порядок:
    # B заметно старее C.
    now = datetime.utcnow()
    db_session.query(models.Device).filter_by(id=dev_b.id).update(
        {"updated_at": now - timedelta(hours=2)}
    )
    db_session.query(models.Device).filter_by(id=dev_c.id).update(
        {"updated_at": now}
    )
    db_session.commit()

    resp = client.get(f"/api/sub/{dev_a.sub_token}", headers={})
    assert resp.status_code == 200
    assert _body(resp).strip() == "vless://node-c"

    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="subscription_fetch", target_type="device", target_id=dev_a.id)
        .first()
    )
    assert audit is not None
    assert audit.extra.get("aliased_to_device_id") == dev_c.id


def test_no_live_configs_returns_503_not_empty_200(client, db_session):
    """Все девайсы подписки мертвы → 503 (retry), а НЕ пустой 200: пустой
    200 затёр бы last-known-good профиль в клиенте."""
    sub, cfg = _setup(db_session)
    dead_a = _mk_device(
        db_session, sub, cfg,
        token="tok-dead-a", uri="vless://dead-a",
        status=models.DeviceStatus.revoked, cred_active=False,
    )
    # Второй девайс active-статусом, но без живых credentials — тоже не кандидат.
    _mk_device(
        db_session, sub, cfg,
        token="tok-dead-b", uri="vless://dead-b",
        cred_active=False, name="halfdead",
    )

    resp = client.get(f"/api/sub/{dead_a.sub_token}", headers={})
    assert resp.status_code == 503
    # AuditLog subscription_fetch не пишется — выдачи не было.
    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="subscription_fetch")
        .first()
    )
    assert audit is None
