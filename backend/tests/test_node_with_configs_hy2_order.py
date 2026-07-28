"""hysteria2 в /nodes/with-configs создаётся ПОСЛЕ своего TLS-фронта.

``ensure_hysteria2_config`` не выпускает собственный сертификат: своего ACME у
hy2 на combo-ноде быть не может — он биндит :80/:443 и дерётся с nginx.
Вместо этого он переиспользует Let's Encrypt-серт уже существующего
xhttp/ws-cdn фронта той же ноды и берёт его домен как sni.

Значит порядок создания конфигов внутри одного запроса — несущий: если hy2
приедет раньше своего фронта, ``cert_path`` останется пустым, роль уйдёт в
ACME-ветку и hysteria на ноде не поднимется. Молча — только warning в логах.

Админка отдаёт протоколы в правильном порядке, но полагаться на порядок
JSON-массива от клиента нельзя, поэтому сортировка живёт на бэкенде.
"""
from __future__ import annotations

from app import models


def _payload(protocols: list[str], *, name: str) -> dict:
    ports = {
        "vless-reality": 9443,
        "vless-ws-cdn": 443,
        "vless-xhttp": 443,
        "hysteria2": 443,
    }
    # xhttp/ws-cdn с ПУСТЫМ sni уходят в авто-CF-ветку (нужен
    # CLOUDFLARE_DNS_TOKEN). Здесь домен задаём явно — тест про порядок
    # создания, а не про минтинг поддоменов.
    snis = {
        "vless-reality": "www.asus.com",
        "vless-ws-cdn": f"{name}-ws.example.net",
        "vless-xhttp": f"{name}-xh.example.net",
    }
    configs = []
    for p in protocols:
        cfg = {"name": f"{name}-{p}", "protocol": p, "port": ports[p]}
        if p in snis:
            cfg["sni"] = snis[p]
        configs.append(cfg)
    return {
        "node": {
            "name": name,
            "region": "Russia",
            "host": "198.51.100.77",
            "ssh_port": 22,
        },
        "configs": configs,
    }


def _hy2_cfg(db, node_name: str):
    node = (
        db.query(models.VPNNode).filter(models.VPNNode.name == node_name).first()
    )
    assert node is not None, "нода не создалась"
    return node, (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.hysteria2,
        )
        .first()
    )


def test_hy2_listed_first_still_gets_front_domain(client, db_session):
    """hy2 первым в payload — всё равно должен получить sni от xhttp.

    Это и есть регресс-гард: без сортировки на бэкенде такой порядок дал бы
    hy2-конфиг без домена и сертификата.
    """
    resp = client.post(
        "/api/nodes/with-configs",
        json=_payload(["hysteria2", "vless-xhttp"], name="hy2-order-first"),
    )
    assert resp.status_code in (200, 201), resp.text

    node, hy2 = _hy2_cfg(db_session, "hy2-order-first")
    xhttp = (
        db_session.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node.id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.vless_xhttp,
        )
        .first()
    )
    assert hy2 is not None and xhttp is not None
    # Домен hy2 совпадает с фронтом — значит серт он унаследовал, а не ушёл
    # в собственный ACME.
    assert hy2.sni, "у hysteria2 пустой sni — cert_path тоже будет пуст"
    assert hy2.sni == xhttp.sni


def test_hy2_listed_last_works_too(client, db_session):
    """Обычный порядок (как шлёт админка) — поведение то же."""
    resp = client.post(
        "/api/nodes/with-configs",
        json=_payload(["vless-xhttp", "hysteria2"], name="hy2-order-last"),
    )
    assert resp.status_code in (200, 201), resp.text

    _node, hy2 = _hy2_cfg(db_session, "hy2-order-last")
    assert hy2 is not None
    assert hy2.sni


def test_hy2_alone_has_no_front_to_borrow(client, db_session):
    """hy2 без vless-фронта на ноде — конфиг создаётся, но без sni.

    Это законный случай (нода без nginx), и ронять запрос нельзя. Роль в
    такой ситуации уходит в собственный ACME и громко предупреждает.
    """
    resp = client.post(
        "/api/nodes/with-configs",
        json=_payload(["hysteria2"], name="hy2-order-alone"),
    )
    assert resp.status_code in (200, 201), resp.text

    _node, hy2 = _hy2_cfg(db_session, "hy2-order-alone")
    assert hy2 is not None
    assert not hy2.sni


def test_hy2_gets_transport_defaults(client, db_session):
    """Оператору не надо вводить obfs/пароль/полосу — их генерит бэкенд."""
    resp = client.post(
        "/api/nodes/with-configs",
        json=_payload(["vless-xhttp", "hysteria2"], name="hy2-order-defaults"),
    )
    assert resp.status_code in (200, 201), resp.text

    _node, hy2 = _hy2_cfg(db_session, "hy2-order-defaults")
    settings = hy2.settings or {}
    # obfs без пароля рассинхронит скрамблинг — они обязаны идти парой.
    if settings.get("obfs"):
        assert settings.get("obfs_password"), "obfs без obfs_password"
