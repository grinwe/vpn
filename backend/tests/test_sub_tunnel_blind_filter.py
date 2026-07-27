"""Аварийный рубильник: убрать из выдачи эндпоинты без split-tunnel.

Штатно каждый протокол туннелируется сам — у трёх vless-флаворов через
routing-правила + ``sockopt.interface``, у hysteria2 через ``outbounds``
(``bindDevice: wgN``) + ``acl.inline``. Поэтому фильтр по умолчанию
ВЫКЛЮЧЕН.

Он нужен на случай, когда ansible-раскатка split-tunnel где-то не прошла:
на такой ноде эндпоинт даёт коннект вообще без VPN (клиент показывает
«подключено», РУ-сайты работают, заблокированное остаётся заблокированным),
и убрать его из выдачи нужно раньше, чем починится нода.
``SUB_FILTER_TUNNEL_BLIND=1`` включает.
"""
from __future__ import annotations

import base64
import logging

from app import models
from app.security import encrypt

from .factories import make_config, make_node, make_plan, make_subscription, make_user


def _mk_exit_link(db, relay: models.VPNNode, *, iface: str = "wg0"):
    """Делает ноду relay'ем: вешает на неё линк к зарубежному exit'у."""
    exit_node = models.WGExitNode(
        name=f"exit-for-{relay.id}",
        region="Turkey",
        host="203.0.113.7",
        wg_public_key="exit-pub-key",
        status=models.WGExitNodeStatus.active,
    )
    db.add(exit_node)
    db.flush()
    link = models.RelayExitLink(
        relay_node_id=relay.id,
        exit_id=exit_node.id,
        wg_interface_name=iface,
        wg_client_private_key_enc=encrypt("dummy-wg-private"),
        wg_client_public_key="client-pub-key",
        wg_client_address_v4="10.77.0.5/32",
    )
    db.add(link)
    db.commit()
    return link


def _mk_device_with_creds(db, sub, cfg, *, token: str, protos_uris, node_id=None):
    device = models.Device(
        user_id=sub.user_id,
        subscription_id=sub.id,
        config_id=cfg.id,
        name="primary",
        status=models.DeviceStatus.active,
        access_username=f"u-{sub.id}-{token}",
        sub_token=token,
    )
    db.add(device)
    db.flush()
    for proto, uri in protos_uris:
        db.add(
            models.Credential(
                subscription_id=sub.id,
                device_id=device.id,
                config_id=cfg.id,
                node_id=node_id if node_id is not None else cfg.node_id,
                proto=proto,
                config_text=uri,
                is_active=True,
            )
        )
    db.commit()
    db.refresh(device)
    return device


def _setup(db, *, node_name: str):
    node = make_node(db, name=node_name, host="198.51.100.10")
    cfg = make_config(db, node)
    user = make_user(db, telegram_id=f"tg-{node_name}")
    plan = make_plan(db)
    sub = make_subscription(db, user, plan, node)
    return node, cfg, sub


def _uris(resp) -> list[str]:
    text = base64.b64decode(resp.text).decode()
    return [line.split("#", 1)[0] for line in text.splitlines() if line.strip()]


def test_shadowtls_dropped_on_relay_node(client, db_session, monkeypatch):
    """С включённым рубильником протокол без split-tunnel выкидывается,
    остальные остаются. shadowtls — единственный такой протокол после
    2026-07-28: роль отключена, split-tunnel там никто не делал."""
    monkeypatch.setenv("SUB_FILTER_TUNNEL_BLIND", "1")
    node, cfg, sub = _setup(db_session, node_name="relay-ru-1")
    _mk_exit_link(db_session, node)
    dev = _mk_device_with_creds(
        db_session,
        sub,
        cfg,
        token="tok-relay",
        protos_uris=[
            ("vless-reality", "vless://reality-uri"),
            ("shadowtls+shadowsocks", "ss://blind-uri"),
        ],
    )

    resp = client.get(f"/api/sub/{dev.sub_token}")

    assert resp.status_code == 200
    uris = _uris(resp)
    assert "vless://reality-uri" in uris
    assert "ss://blind-uri" not in uris


def test_hysteria2_kept_on_relay_node(client, db_session, monkeypatch):
    """hy2 НЕ режется даже с включённым рубильником: с 2026-07-28 он умеет
    split-tunnel (outbounds + acl в config.yaml.j2). Регресс-гард — иначе
    фикс роли и фильтр разъедутся, и рабочий протокол начнёт вырезаться."""
    monkeypatch.setenv("SUB_FILTER_TUNNEL_BLIND", "1")
    node, cfg, sub = _setup(db_session, node_name="relay-hy2-ok")
    _mk_exit_link(db_session, node)
    dev = _mk_device_with_creds(
        db_session, sub, cfg, token="tok-hy2-ok",
        protos_uris=[("hysteria2", "hy2://tunnelled-uri")],
    )

    resp = client.get(f"/api/sub/{dev.sub_token}")

    assert resp.status_code == 200
    assert "hy2://tunnelled-uri" in _uris(resp)


def test_hysteria2_kept_on_direct_node(client, db_session):
    """На ноде без relay-линков hy2 корректен — выходит с её же IP, как и
    должен."""
    node, cfg, sub = _setup(db_session, node_name="direct-nl-1")
    dev = _mk_device_with_creds(
        db_session,
        sub,
        cfg,
        token="tok-direct",
        protos_uris=[
            ("vless-reality", "vless://reality-uri"),
            ("hysteria2", "hy2://fine-uri"),
        ],
    )

    resp = client.get(f"/api/sub/{dev.sub_token}")

    assert resp.status_code == 200
    assert "hy2://fine-uri" in _uris(resp)


def test_only_blind_endpoints_still_served(client, db_session, caplog, monkeypatch):
    """Если туннелирующих легов не осталось вовсе — отдаём что есть.

    503 навсегда оставил бы юзера без связи; кривой коннект хотя бы работает.
    Но это состояние обязано быть громким в логах.
    """
    monkeypatch.setenv("SUB_FILTER_TUNNEL_BLIND", "1")
    node, cfg, sub = _setup(db_session, node_name="relay-ru-2")
    _mk_exit_link(db_session, node)
    dev = _mk_device_with_creds(
        db_session,
        sub,
        cfg,
        token="tok-only-blind",
        protos_uris=[("shadowtls+shadowsocks", "ss://only-uri")],
    )

    # alembic fileConfig(disable_existing_loggers) на старте харнесса глушит
    # уже созданный логгер модуля — ре-активируем, иначе запись не доходит до
    # caplog и проверка залогированности ложно падает.
    logging.getLogger("app.api_extensions").disabled = False
    with caplog.at_level(logging.ERROR, logger="app.api_extensions"):
        resp = client.get(f"/api/sub/{dev.sub_token}")

    assert resp.status_code == 200
    assert "ss://only-uri" in _uris(resp)
    assert any("no VPN" in r.getMessage() for r in caplog.records)


def test_filter_is_off_by_default(client, db_session):
    """Дефолт — рубильник ВЫКЛЮЧЕН: штатно каждый протокол туннелируется сам,
    и саба не должна ничего резать без явного включения."""
    node, cfg, sub = _setup(db_session, node_name="relay-ru-3")
    _mk_exit_link(db_session, node)
    dev = _mk_device_with_creds(
        db_session,
        sub,
        cfg,
        token="tok-default-off",
        protos_uris=[
            ("vless-reality", "vless://reality-uri"),
            ("shadowtls+shadowsocks", "ss://legacy-uri"),
        ],
    )

    resp = client.get(f"/api/sub/{dev.sub_token}")

    assert resp.status_code == 200
    assert "ss://legacy-uri" in _uris(resp)


def test_legacy_sub_token_branch_also_filters(client, db_session, monkeypatch):
    """Легаси-ветка (токен подписки, а не девайса) фильтрует так же."""
    monkeypatch.setenv("SUB_FILTER_TUNNEL_BLIND", "1")
    node, cfg, sub = _setup(db_session, node_name="relay-ru-4")
    _mk_exit_link(db_session, node)
    sub.sub_token = "legacy-tok"
    for proto, uri in (
        ("vless-xhttp", "vless://xhttp-uri"),
        ("shadowtls+shadowsocks", "ss://blind-uri"),
    ):
        db_session.add(
            models.Credential(
                subscription_id=sub.id,
                config_id=cfg.id,
                node_id=node.id,
                proto=proto,
                config_text=uri,
                is_active=True,
            )
        )
    db_session.commit()

    resp = client.get("/api/sub/legacy-tok")

    assert resp.status_code == 200
    uris = _uris(resp)
    assert "vless://xhttp-uri" in uris
    assert "ss://blind-uri" not in uris


def test_ws_cdn_is_not_filtered(client, db_session, monkeypatch):
    """ws-cdn остаётся даже с включённым рубильником: он под xray, sockopt на
    wgN у него есть, и с 2026-07-28 есть RU-правила. Регресс-гард на случай,
    если кто-то расширит фильтр «за компанию»."""
    monkeypatch.setenv("SUB_FILTER_TUNNEL_BLIND", "1")
    node, cfg, sub = _setup(db_session, node_name="relay-ru-5")
    _mk_exit_link(db_session, node)
    dev = _mk_device_with_creds(
        db_session,
        sub,
        cfg,
        token="tok-wscdn",
        protos_uris=[("vless-ws-cdn", "vless://ws-cdn-uri")],
    )

    resp = client.get(f"/api/sub/{dev.sub_token}")

    assert resp.status_code == 200
    assert "vless://ws-cdn-uri" in _uris(resp)
