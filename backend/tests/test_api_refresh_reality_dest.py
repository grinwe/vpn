"""HTTP smoke tests for POST /api/nodes/{id}/refresh-reality-dest.

Покрывает:
- Миссинг нода → 404.
- Нода без vless_reality конфига → 400.
- Тот же SNI что был → 400 (noop-protection, чтобы случайный клик в
  UI не перепровижинил всю ноду впустую).
- Happy path: sni обновляется в DB, active sub репровижинится,
  audit log пишется, task_ids возвращаются.
- Explicit sni override работает (вне пула тоже).
"""
from __future__ import annotations

from app import models
from app.services import node_spawner

from .factories import (
    make_config,
    make_device,
    make_node,
    make_plan,
    make_subscription_with_device,
    make_user,
)


def test_refresh_reality_dest_node_not_found(client):
    resp = client.post(
        "/api/nodes/9999/refresh-reality-dest",
        json={"sni": "vk.ru"},
    )
    assert resp.status_code == 404


def test_refresh_reality_dest_no_reality_config(client, db_session):
    node = make_node(db_session, name="n-no-reality", host="203.0.113.1")
    resp = client.post(
        f"/api/nodes/{node.id}/refresh-reality-dest",
        json={"sni": "vk.ru"},
    )
    assert resp.status_code == 400
    assert "vless_reality" in resp.json()["detail"]


def test_refresh_reality_dest_noop_when_same_sni(client, db_session):
    node = make_node(db_session, name="n-same", host="203.0.113.2")
    make_config(db_session, node, sni="vk.ru")
    resp = client.post(
        f"/api/nodes/{node.id}/refresh-reality-dest",
        json={"sni": "vk.ru"},
    )
    assert resp.status_code == 400
    assert "already" in resp.json()["detail"].lower()


def test_refresh_reality_dest_updates_config_and_reprovisions(client, db_session):
    node = make_node(db_session, name="n-refresh", host="203.0.113.10")
    cfg = make_config(db_session, node, sni="www.yandex.ru")
    plan = make_plan(db_session)
    user = make_user(db_session, telegram_id="tg-refresh")
    sub = make_subscription_with_device(db_session, user, plan, node)

    resp = client.post(
        f"/api/nodes/{node.id}/refresh-reality-dest",
        json={"sni": "vk.ru"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["node_id"] == node.id
    assert body["old_sni"] == "www.yandex.ru"
    assert body["new_sni"] == "vk.ru"
    assert body["sub_count"] == 1
    assert body["failed_subs"] == []

    db_session.refresh(cfg)
    assert cfg.sni == "vk.ru"
    assert cfg.fallback == "vk.ru:443"
    assert cfg.settings["dest"] == "vk.ru:443"

    # sub остаётся active, на ноде появляется новый device с свежим
    # cred_text (UUID и sni — новые). Старый device disabled.
    db_session.refresh(sub)
    assert sub.status == models.SubscriptionStatus.active
    active_devices = [d for d in sub.devices if d.status == models.DeviceStatus.active]
    assert len(active_devices) >= 1

    # Audit row.
    audit = (
        db_session.query(models.AuditLog)
        .filter_by(action="node_reality_dest_refreshed", target_id=node.id)
        .one_or_none()
    )
    assert audit is not None
    assert audit.extra["old_sni"] == "www.yandex.ru"
    assert audit.extra["new_sni"] == "vk.ru"
    assert audit.extra["sub_count"] == 1


def test_refresh_reality_dest_auto_picks_from_pool(client, db_session):
    node = make_node(db_session, name="n-auto", host="203.0.113.20")
    # Ставим sni который НЕ в пуле — чтобы любой выбор из пула прошёл.
    cfg = make_config(db_session, node, sni="www.microsoft.com")

    resp = client.post(
        f"/api/nodes/{node.id}/refresh-reality-dest",
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["new_sni"] in node_spawner.REALITY_DEST_POOL
    db_session.refresh(cfg)
    assert cfg.sni == body["new_sni"]


def test_refresh_reality_dest_preserves_multi_device_count(client, db_session):
    """Регрессия: до фикса refresh схлопывал N девайсов сабы в один
    "primary" — админ терял купленные extra-slots. Сейчас каждый
    active Device получает свою reprovision пару (revoke old + add new
    с тем же device_name)."""
    node = make_node(db_session, name="n-multi", host="203.0.113.40")
    make_config(db_session, node, sni="www.yandex.ru")
    plan = make_plan(db_session, max_devices=5)
    user = make_user(db_session, telegram_id="tg-multi")
    sub = make_subscription_with_device(
        db_session, user, plan, node, access_username="u-primary"
    )
    cfg_node = node.configs[0]
    # Два дополнительных девайса в сабу, чтобы sub имел 3 активных.
    make_device(db_session, sub, cfg_node, access_username="u-extra-1")
    make_device(db_session, sub, cfg_node, access_username="u-extra-2")
    db_session.refresh(sub)
    assert (
        sum(1 for d in sub.devices if d.status == models.DeviceStatus.active) == 3
    )

    resp = client.post(
        f"/api/nodes/{node.id}/refresh-reality-dest",
        json={"sni": "vk.ru"},
    )
    assert resp.status_code == 200, resp.text

    db_session.refresh(sub)
    active = [d for d in sub.devices if d.status == models.DeviceStatus.active]
    # После refresh на ноде должно быть 3 fresh Device'а (по одному на
    # каждый снапшотнутый старый). Ревокнутые остались в БД для
    # sub_token-aliasing'а, но как disabled.
    assert len(active) == 3, (
        f"expected 3 active devices post-refresh, got {len(active)}; "
        f"statuses: {[d.status.value for d in sub.devices]}"
    )


def test_refresh_reality_dest_accepts_sni_outside_pool(client, db_session):
    """Явный sni вне пула разрешён — admin может форсить whitelist-домен."""
    node = make_node(db_session, name="n-custom", host="203.0.113.30")
    make_config(db_session, node, sni="www.yandex.ru")

    resp = client.post(
        f"/api/nodes/{node.id}/refresh-reality-dest",
        json={"sni": "custom.example.io"},
    )
    assert resp.status_code == 200
    assert resp.json()["new_sni"] == "custom.example.io"


def test_refresh_reality_dest_requires_admin_token(client):
    """Без X-Admin-Token → 401/403 (зависит от require_admin)."""
    resp = client.post(
        "/api/nodes/1/refresh-reality-dest",
        json={"sni": "vk.ru"},
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status_code in (401, 403)
