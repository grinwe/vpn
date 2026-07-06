"""Regression для audit-находки #102: relay_link_health коммитит по мере
обхода, а не одним commit в конце.

Симптом: RQ-таймаут тика (120с) при нескольких лежащих/медленных relay
(до ~10с SSH-таймаута на каждый) убивал джоб до финального commit —
и обновления ВСЕХ relay, включая уже успешно опрошенных, терялись.
last_observed_at всего флота протухал разом, а _auto_diagnose_stale_links
молча отключался именно когда мониторинг нужнее всего.

Фикс: per-relay commit + обход relay в порядке last_observed_at ASC
(самый протухший первым, чтобы при повторных kill не голодали одни и те же).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import models
from app.db import SessionLocal
from app.security import encrypt
from app.services import relay_link_health

from .factories import make_node


def _make_exit(db, *, name, host, pubkey):
    exit_node = models.WGExitNode(
        name=name,
        region="eu",
        host=host,
        status=models.WGExitNodeStatus.active,
        is_active=True,
        wg_public_key=pubkey,
    )
    db.add(exit_node)
    db.commit()
    db.refresh(exit_node)
    return exit_node


def _attach(db, relay, exit_node, *, iface, addr):
    link = models.RelayExitLink(
        relay_node_id=relay.id,
        exit_id=exit_node.id,
        wg_interface_name=iface,
        wg_client_private_key_enc=encrypt("dummy-privkey"),
        wg_client_public_key=f"cpub-{iface}",
        wg_client_address_v4=addr,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def test_partial_progress_survives_mid_pass_kill(db_session, monkeypatch):
    """Первый (протухший) relay опрашивается успешно, второй симулирует
    kill-horse (BaseException минует ``except Exception``). Убеждаемся, что
    данные первого relay уже закоммичены — их видно из свежей сессии.
    """
    # relay-A: last_observed_at=None → самый протухший → обходится ПЕРВЫМ.
    relay_a = make_node(db_session, name="relay-A", host="10.0.0.10")
    exit_a = _make_exit(db_session, name="exit-A", host="203.0.113.10",
                        pubkey="EXITPUB_A")
    link_a = _attach(db_session, relay_a, exit_a, iface="wg0",
                     addr="10.77.0.5/32")

    # relay-B: свежий last_observed_at → обходится ВТОРЫМ и «убивает» джоб.
    relay_b = make_node(db_session, name="relay-B", host="10.0.0.11")
    exit_b = _make_exit(db_session, name="exit-B", host="203.0.113.11",
                        pubkey="EXITPUB_B")
    link_b = _attach(db_session, relay_b, exit_b, iface="wg0",
                     addr="10.78.0.5/32")
    link_b.last_observed_at = datetime.utcnow()
    db_session.commit()

    order: list[str] = []

    def fake_collect(relay):
        order.append(relay.name)
        if relay.name == "relay-A":
            # Валидный dump, матчащий (iface, exit_pub).
            return {("wg0", "EXITPUB_A"): {"handshake": 1_700_000_000,
                                           "rx": 111, "tx": 222}}
        # Симулируем kill-horse: BaseException не ловится except Exception.
        raise SystemExit("horse killed mid-pass")

    monkeypatch.setattr(relay_link_health, "_collect_relay_wg_state",
                        fake_collect)

    with pytest.raises(SystemExit):
        relay_link_health.collect_all_relay_links(db_session)

    # Ordering: протухший relay-A опрошен до relay-B.
    assert order == ["relay-A", "relay-B"]

    # Свежая сессия: прогресс relay-A пережил «kill» благодаря per-relay
    # commit. Без фикса (один commit в конце) здесь было бы всё NULL.
    verify = SessionLocal()
    try:
        persisted = verify.get(models.RelayExitLink, link_a.id)
        assert persisted.last_rx_bytes == 111
        assert persisted.last_tx_bytes == 222
        assert persisted.last_handshake_at is not None
        assert persisted.last_observed_at is not None
    finally:
        verify.close()


def test_stalest_relay_visited_first(db_session, monkeypatch):
    """При обходе relay сортируются по last_observed_at ASC: relay с самым
    старым наблюдением идёт первым, чтобы не голодал под повторными kill.
    """
    now = datetime.utcnow()
    relay_fresh = make_node(db_session, name="relay-fresh", host="10.0.0.20")
    relay_old = make_node(db_session, name="relay-old", host="10.0.0.21")
    ex1 = _make_exit(db_session, name="ex1", host="203.0.113.20",
                     pubkey="P1")
    ex2 = _make_exit(db_session, name="ex2", host="203.0.113.21",
                     pubkey="P2")
    l_fresh = _attach(db_session, relay_fresh, ex1, iface="wg0",
                      addr="10.79.0.5/32")
    l_old = _attach(db_session, relay_old, ex2, iface="wg0",
                    addr="10.80.0.5/32")
    l_fresh.last_observed_at = now
    l_old.last_observed_at = now - timedelta(hours=1)
    db_session.commit()

    order: list[str] = []

    def fake_collect(relay):
        order.append(relay.name)
        return {}

    monkeypatch.setattr(relay_link_health, "_collect_relay_wg_state",
                        fake_collect)

    relay_link_health.collect_all_relay_links(db_session)

    assert order == ["relay-old", "relay-fresh"]
