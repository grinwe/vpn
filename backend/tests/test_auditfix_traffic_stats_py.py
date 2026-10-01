"""Аудит-фикс в backend/app/services/traffic_stats.py (находка 248).

Раньше ``collect_all_active_nodes`` обходил ноды строго последовательно
и делал ОДИН ``session.commit()`` после всего цикла: при kill'е джобы по
job_timeout=120s (зависшая SSH-сессия) терялись все уже собранные сэмплы
тика, а хвост нод вообще не опрашивался. Теперь:

* сбор параллельный (ThreadPoolExecutor), записи в сессию — только из
  главного потока;
* per-node commit — частичный прогресс переживает kill;
* wall-clock-бюджет ``TRAFFIC_STATS_BUDGET_SEC`` — недособранный хвост
  откладывается до следующего тика вместо смерти всего тика.

SSH здесь не поднимаем — ``collect_node_stats`` монкипатчится, проверяем
именно оркестровку (commit-семантику и бюджет).
"""
from __future__ import annotations

import threading

from sqlalchemy.orm import Session

from app import models
from app.services import traffic_stats
from tests.factories import make_node


def _fake_result(uplink: int = 1000, downlink: int = 2000, users: int = 2) -> traffic_stats.NodeStatsResult:
    res = traffic_stats.NodeStatsResult(
        uplink_bytes=uplink,
        downlink_bytes=downlink,
        active_users=users,
    )
    res.per_protocol["vless-reality"] = traffic_stats.ProtocolStats(
        uplink=uplink, downlink=downlink, users={f"u{i}" for i in range(users)},
    )
    return res


def test_collect_all_nodes_parallel_partial_failure(db_session: Session, monkeypatch) -> None:
    """Падение SSH на одной ноде не мешает остальным; сэмплы закоммичены."""
    node_ok1 = make_node(db_session, name="ts-ok-1", host="192.0.2.1")
    node_bad = make_node(db_session, name="ts-bad", host="192.0.2.2")
    node_ok2 = make_node(db_session, name="ts-ok-2", host="192.0.2.3")

    def fake_collect(ref):
        if ref.host == node_bad.host:
            raise RuntimeError("ssh connect timeout (simulated)")
        return _fake_result()

    monkeypatch.setattr(traffic_stats, "collect_node_stats", fake_collect)
    # Сетевой аудит (finding 4) добавил preflight-загрузку provisioning-ключа
    # ОДИН раз до старта потоков. В юнит-окружении ключа нет — мокаем загрузку и
    # os.path.exists, иначе collect_all_active_nodes короткозамыкает в [] ещё до
    # collect_node_stats и оркестровка (ради которой тест) не проверяется.
    monkeypatch.setattr(traffic_stats, "_load_provisioning_pkey", lambda p: object())
    monkeypatch.setattr(traffic_stats.os.path, "exists", lambda p: True)

    summaries = traffic_stats.collect_all_active_nodes(db_session, interval_seconds=300)

    got_ids = {s["node_id"] for s in summaries}
    assert got_ids == {node_ok1.id, node_ok2.id}

    # Сэмплы реально закоммичены — видны из независимой сессии.
    from app.db import SessionLocal

    other = SessionLocal()
    try:
        rows = other.query(models.NodeTrafficSample).all()
        assert {r.node_id for r in rows} == {node_ok1.id, node_ok2.id}
        for r in rows:
            assert r.uplink_bytes == 1000
            assert r.downlink_bytes == 2000
            assert r.active_users == 2
    finally:
        other.close()


def test_collect_all_nodes_budget_defers_tail_keeps_partial(db_session: Session, monkeypatch) -> None:
    """Бюджет исчерпан → быстрая нода уже закоммичена, зависшая отложена.

    Это ключевая регрессия находки 248: старый код коммитил один раз
    после всего цикла, и зависшая SSH-сессия хоронила ВСЕ сэмплы тика.
    """
    node_fast = make_node(db_session, name="ts-fast", host="192.0.2.10")
    node_hang = make_node(db_session, name="ts-hang", host="192.0.2.11")

    release = threading.Event()

    def fake_collect(ref):
        if ref.host == node_hang.host:
            # «Зависшая» SSH-сессия: держим поток дольше бюджета.
            release.wait(timeout=10)
            return _fake_result()
        return _fake_result(uplink=111, downlink=222, users=1)

    monkeypatch.setattr(traffic_stats, "collect_node_stats", fake_collect)
    # См. коммент выше: мокаем preflight-загрузку ключа, иначе короткое
    # замыкание в [] до старта параллельного сбора.
    monkeypatch.setattr(traffic_stats, "_load_provisioning_pkey", lambda p: object())
    monkeypatch.setattr(traffic_stats.os.path, "exists", lambda p: True)
    monkeypatch.setenv("TRAFFIC_STATS_BUDGET_SEC", "1")

    try:
        summaries = traffic_stats.collect_all_active_nodes(db_session, interval_seconds=300)
    finally:
        release.set()  # отпускаем фоновый поток, чтобы тест не ждал 10с

    # Быстрая нода собрана и закоммичена, зависшая — отложена.
    assert [s["node_id"] for s in summaries] == [node_fast.id]

    from app.db import SessionLocal

    other = SessionLocal()
    try:
        rows = other.query(models.NodeTrafficSample).all()
        assert len(rows) == 1
        assert rows[0].node_id == node_fast.id
        assert rows[0].uplink_bytes == 111
    finally:
        other.close()


def test_collect_all_nodes_empty_fleet(db_session: Session) -> None:
    """Пустой флот — пустой список, без обращения к SSH/пулу."""
    assert traffic_stats.collect_all_active_nodes(db_session, interval_seconds=300) == []
