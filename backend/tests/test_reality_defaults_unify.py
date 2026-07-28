"""Reality на новой ноде создаётся сразу унифицированным и с SNI из пула.

Два бага, которые это закрывает.

1. ``public_port`` не проставлял НИКТО. Весь флот унифицировали на :443
   разовым скриптом, а нода, заведённая через админку, молча получалась
   неунифицированной: reality торчал наружу на 9443 мимо общего :443, то есть
   отличался от остальных десяти. Признак режима — ровно ``public_port``: по
   нему provisioning выставляет роли ``reality_stream_unify`` и подставляет
   443 в клиентский URI.

2. Форма админки навязывала ``www.asus.com``. Этот домен жил в дефолтах с 0.1
   и давно разошёлся с продом (там ozon/ya/wb/kinopoisk/avito/rbc/yandex), а
   пул ротировали 2026-07-22 после регионального DPI в Яр/Туле. Пустой SNI
   означает «выбери из регионального пула наименее используемый».
"""
from __future__ import annotations

from app import models
from app.services.node_spawner import (
    REALITY_DEST_POOLS,
    UNIFIED_REALITY_LISTEN_PORT,
    ensure_reality_config,
)

from .factories import make_config, make_node


def _reality(db, node_id: int):
    return (
        db.query(models.VPNConfig)
        .filter(
            models.VPNConfig.node_id == node_id,
            models.VPNConfig.protocol == models.VPNConfigProtocol.vless_reality,
        )
        .first()
    )


def test_reality_alone_stays_on_its_own_port(db_session):
    """Нода без TCP-фронта (шаблон «Reality only») — обычный режим.

    Вешать stream не на что: nginx там никто не ставит, поэтому unify
    включать нельзя.
    """
    node = make_node(db_session, name="ru-solo-reality", host="198.51.100.31")
    cfg = ensure_reality_config(db_session, node)

    assert (cfg.settings or {}).get("public_port") is None
    assert cfg.port != UNIFIED_REALITY_LISTEN_PORT or cfg.port == 443


def test_reality_with_tcp_front_gets_unified(db_session):
    """Есть xhttp → reality уходит на loopback, наружу отдаётся 443."""
    node = make_node(db_session, name="ru-combo-1", host="198.51.100.32")
    make_config(
        db_session,
        node,
        name="xhttp",
        protocol=models.VPNConfigProtocol.vless_xhttp,
        port=443,
        sni="front.example.net",
    )
    db_session.refresh(node)

    cfg = ensure_reality_config(db_session, node)

    assert (cfg.settings or {})["public_port"] == 443
    # Внутренний порт ОБЯЗАН отличаться от 443: nginx держит 0.0.0.0:443,
    # и 127.0.0.1:443 — тот же сокет, второй процесс не поднимется.
    assert cfg.port == UNIFIED_REALITY_LISTEN_PORT
    assert cfg.port != 443


def test_ws_cdn_also_counts_as_front(db_session):
    node = make_node(db_session, name="ru-combo-2", host="198.51.100.33")
    make_config(
        db_session,
        node,
        name="ws",
        protocol=models.VPNConfigProtocol.vless_ws_cdn,
        port=443,
        sni="ws.example.net",
    )
    db_session.refresh(node)

    cfg = ensure_reality_config(db_session, node)
    assert (cfg.settings or {})["public_port"] == 443


def test_explicit_port_wins(db_session):
    """Явно заданный порт уважается — оператор может знать, что делает."""
    node = make_node(db_session, name="ru-combo-3", host="198.51.100.34")
    make_config(
        db_session,
        node,
        name="xhttp",
        protocol=models.VPNConfigProtocol.vless_xhttp,
        port=443,
        sni="front3.example.net",
    )
    db_session.refresh(node)

    cfg = ensure_reality_config(db_session, node, port=8443)
    assert cfg.port == 8443
    assert (cfg.settings or {})["public_port"] == 443


def test_sni_comes_from_regional_pool_not_hardcode(db_session):
    """Пустой SNI → домен из пула по стране ДЦ, а не легаси-хардкод."""
    node = make_node(db_session, name="ru-pool-1", region="Russia",
                     host="198.51.100.35")
    cfg = ensure_reality_config(db_session, node)

    assert cfg.sni in REALITY_DEST_POOLS["ru"], cfg.sni
    assert cfg.sni != "www.asus.com", "вернулся легаси-хардкод из формы"
    # dest выводится из sni, а не задаётся отдельно.
    assert cfg.fallback == f"{cfg.sni}:443"
    assert (cfg.settings or {})["dest"] == f"{cfg.sni}:443"


def test_explicit_sni_wins(db_session):
    node = make_node(db_session, name="ru-pool-2", host="198.51.100.36")
    cfg = ensure_reality_config(db_session, node, sni="www.example.org")
    assert cfg.sni == "www.example.org"
    assert cfg.fallback == "www.example.org:443"


def test_with_configs_creates_reality_after_front(client, db_session):
    """Сквозной путь формы: порядок в payload не должен решать.

    reality идёт в CREATE_PROTOS первым, но unify зависит от уже созданного
    фронта — значит бэкенд обязан разложить порядок сам.
    """
    resp = client.post(
        "/api/nodes/with-configs",
        json={
            "node": {
                "name": "ru-order-unify",
                "region": "Russia",
                "host": "198.51.100.37",
                "ssh_port": 22,
            },
            "configs": [
                {"name": "r", "protocol": "vless-reality", "port": 9443},
                {"name": "x", "protocol": "vless-xhttp", "port": 443,
                 "sni": "order-front.example.net"},
            ],
        },
    )
    assert resp.status_code in (200, 201), resp.text

    node = (
        db_session.query(models.VPNNode)
        .filter(models.VPNNode.name == "ru-order-unify")
        .first()
    )
    cfg = _reality(db_session, node.id)
    assert cfg is not None
    assert (cfg.settings or {})["public_port"] == 443, (
        "reality создался раньше фронта — унификация не включилась"
    )
