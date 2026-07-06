"""Network-audit fixes for services/traffic_stats.py.

Покрывает четыре находки сетевого аудита:
  1. `2>/dev/null` убран; реальный сбой xray (exit!=0, не «refused»)
     кладётся в ProtocolStats.error, а не проглатывается как ноль.
  2. Ноды сабмитятся в порядке давности сэмпла (never-sampled первыми);
     повторный отсев по бюджету поднимает счётчик пропусков.
  4. Перебор типов provisioning-ключа (ed25519/rsa/ecdsa) вместо только
     Ed25519 — смена типа ключа больше не глушит сбор по всему флоту.
  7. banner/auth таймауты подняты до 20с (паритет с ssh_bootstrap).

Тесты юнитовые: paramiko и `_ssh_run` подменяются; для finding 2
используется минимальная фейковая сессия.
"""
from __future__ import annotations

import sys
import types

import pytest

from app.services import traffic_stats


# ── общая инфраструктура ────────────────────────────────────────────

def _install_fake_paramiko(monkeypatch, *, connect_sink=None,
                           ed25519_ok=True, rsa_ok=False, ecdsa_ok=False):
    """Фейковый paramiko. connect_sink (если задан) получит kwargs connect."""

    class _FakeClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, *a, **k):
            if connect_sink is not None:
                connect_sink.update(k)

        def close(self):
            pass

    def _loader(ok):
        def _from(path):
            if not ok:
                raise ValueError("not this key type")
            return object()
        return types.SimpleNamespace(from_private_key_file=_from)

    fake = types.ModuleType("paramiko")
    fake.Ed25519Key = _loader(ed25519_ok)
    fake.RSAKey = _loader(rsa_ok)
    fake.ECDSAKey = _loader(ecdsa_ok)
    fake.SSHClient = _FakeClient
    fake.AutoAddPolicy = lambda: object()
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    monkeypatch.setattr(traffic_stats.os.path, "exists", lambda p: True)


# ── finding 1: диагностика xray ────────────────────────────────────

def test_statsquery_cmd_has_no_dev_null(monkeypatch):
    """Команда statsquery больше НЕ редиректит stderr в /dev/null —
    иначе реальный сбой xray неотличим от отсутствия протокола."""
    _install_fake_paramiko(monkeypatch)
    recorded: list[str] = []

    def _fake(client, command):
        recorded.append(command)
        return 1, "", "connection refused"

    monkeypatch.setattr(traffic_stats, "_ssh_run", _fake)
    node = types.SimpleNamespace(id=1, name="n1", host="1.2.3.4", ssh_port=22)
    traffic_stats.collect_node_stats(node)

    stats_cmds = [c for c in recorded if "statsquery" in c]
    assert stats_cmds, "statsquery не вызван"
    assert all("2>/dev/null" not in c for c in stats_cmds)


def test_connection_refused_is_not_an_error(monkeypatch):
    """exit!=0 с «connection refused» = протокол не установлен →
    пустой ProtocolStats БЕЗ error."""
    _install_fake_paramiko(monkeypatch)
    monkeypatch.setattr(
        traffic_stats, "_ssh_run",
        lambda c, cmd: (1, "", "failed to dial: connection refused"),
    )
    node = types.SimpleNamespace(id=1, name="n1", host="1.2.3.4", ssh_port=22)
    res = traffic_stats.collect_node_stats(node)
    for proto, _ in traffic_stats.KNOWN_PROTOCOL_PORTS:
        assert res.per_protocol[proto].error is None


def test_real_xray_failure_surfaces_error(monkeypatch):
    """exit!=0 с иным stderr (реальный сбой xray/gRPC) → error заполнен,
    чтобы деградация была видна в details._errors, а не как ноль."""
    _install_fake_paramiko(monkeypatch)
    monkeypatch.setattr(
        traffic_stats, "_ssh_run",
        lambda c, cmd: (1, "", "rpc error: context deadline exceeded"),
    )
    node = types.SimpleNamespace(id=1, name="n1", host="1.2.3.4", ssh_port=22)
    res = traffic_stats.collect_node_stats(node)
    details = res.to_details()
    assert "_errors" in details
    assert any("deadline" in e for e in details["_errors"].values())


# ── finding 4: перебор типов ключа ─────────────────────────────────

def test_load_pkey_tries_loaders_in_order(monkeypatch):
    """_load_provisioning_pkey возвращает первый подошедший загрузчик."""
    _install_fake_paramiko(monkeypatch, ed25519_ok=False, rsa_ok=True)
    key = traffic_stats._load_provisioning_pkey("/x")
    assert key is not None


def test_load_pkey_all_fail_raises_clear_error(monkeypatch):
    """Если ни ed25519/rsa/ecdsa не подошёл — один внятный RuntimeError
    про тип ключа (а не N молчаливых «collect failed»)."""
    _install_fake_paramiko(
        monkeypatch, ed25519_ok=False, rsa_ok=False, ecdsa_ok=False
    )
    with pytest.raises(RuntimeError, match="unsupported"):
        traffic_stats._load_provisioning_pkey("/x")


def test_collect_falls_back_to_rsa_key(monkeypatch):
    """collect_node_stats работает, когда provisioning_key — RSA
    (Ed25519 не подошёл): раньше падало бы по всему флоту."""
    _install_fake_paramiko(monkeypatch, ed25519_ok=False, rsa_ok=True)
    monkeypatch.setattr(
        traffic_stats, "_ssh_run",
        lambda c, cmd: (0, '{"stat": []}', ""),
    )
    node = types.SimpleNamespace(id=1, name="n1", host="1.2.3.4", ssh_port=22)
    res = traffic_stats.collect_node_stats(node)
    assert isinstance(res, traffic_stats.NodeStatsResult)


# ── finding 7: SSH-таймауты ────────────────────────────────────────

def test_banner_and_auth_timeouts_are_20s(monkeypatch):
    """connect вызывается с banner/auth timeout=20 (паритет с
    ssh_bootstrap), connect timeout=15."""
    assert traffic_stats.SSH_BANNER_TIMEOUT == 20
    assert traffic_stats.SSH_AUTH_TIMEOUT == 20
    assert traffic_stats.SSH_CONNECT_TIMEOUT == 15

    sink: dict = {}
    _install_fake_paramiko(monkeypatch, connect_sink=sink)
    monkeypatch.setattr(
        traffic_stats, "_ssh_run",
        lambda c, cmd: (0, '{"stat": []}', ""),
    )
    node = types.SimpleNamespace(id=1, name="n1", host="1.2.3.4", ssh_port=22)
    traffic_stats.collect_node_stats(node)
    assert sink["banner_timeout"] == 20
    assert sink["auth_timeout"] == 20
    assert sink["timeout"] == 15


# ── finding 2: порядок сабмита по давности сэмпла ──────────────────

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def group_by(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Минимальная сессия: query(VPNNode) → ноды, query(col, func) → last_seen."""

    def __init__(self, nodes, last_seen_rows):
        self._nodes = nodes
        self._last = last_seen_rows
        self.is_active = True

    def query(self, *args):
        # query(models.VPNNode) — один аргумент; агрегат last_seen — два.
        return _FakeQuery(self._nodes if len(args) == 1 else self._last)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_never_sampled_nodes_submitted_first(monkeypatch):
    """Ноды без сэмплов и с самым старым сэмплом опрашиваются раньше
    недавно собранных — отсев по бюджету не бьёт всегда по одним и тем же."""
    from datetime import datetime, timedelta

    from app import models

    now = datetime(2026, 7, 4, 12, 0, 0)
    nodes = [
        types.SimpleNamespace(id=1, name="recent", host="h1", ssh_port=22,
                              is_active=True, status=models.VPNNodeStatus.active),
        types.SimpleNamespace(id=2, name="never", host="h2", ssh_port=22,
                              is_active=True, status=models.VPNNodeStatus.active),
        types.SimpleNamespace(id=3, name="old", host="h3", ssh_port=22,
                              is_active=True, status=models.VPNNodeStatus.active),
    ]
    last_seen = [(1, now - timedelta(minutes=5)), (3, now - timedelta(hours=6))]
    session = _FakeSession(nodes, last_seen)

    # 1 воркер → порядок исполнения = порядок сабмита (детерминированно).
    monkeypatch.setenv("TRAFFIC_STATS_SSH_WORKERS", "1")
    monkeypatch.setattr(traffic_stats, "_load_provisioning_pkey", lambda p: object())
    monkeypatch.setattr(traffic_stats.os.path, "exists", lambda p: True)

    order: list[int] = []

    def _fake_collect(ref):
        order.append(ref.id)
        return traffic_stats.NodeStatsResult()

    monkeypatch.setattr(traffic_stats, "collect_node_stats", _fake_collect)
    monkeypatch.setattr(
        traffic_stats, "_persist_node_result",
        lambda s, ref, r, i: {"node_id": ref.id, "node": ref.name,
                              "active_users": 0},
    )

    traffic_stats.collect_all_active_nodes(session, 300)

    # never (id=2) и old (id=3) должны идти раньше recent (id=1).
    assert order.index(2) < order.index(1)
    assert order.index(3) < order.index(1)


def test_preflight_bad_key_type_returns_empty_and_logs_once(monkeypatch, caplog):
    """Неподдерживаемый тип ключа → один явный error-лог и пустой
    результат, а не N молчаливых «collect failed» по нодам."""
    from app import models

    nodes = [
        types.SimpleNamespace(id=i, name=f"n{i}", host=f"h{i}", ssh_port=22,
                              is_active=True, status=models.VPNNodeStatus.active)
        for i in range(1, 4)
    ]
    session = _FakeSession(nodes, [])
    monkeypatch.setattr(traffic_stats.os.path, "exists", lambda p: True)
    _install_fake_paramiko(
        monkeypatch, ed25519_ok=False, rsa_ok=False, ecdsa_ok=False
    )

    import logging
    # alembic fileConfig(disable_existing_loggers) на старте харнесса глушит
    # уже созданный логгер модуля — ре-активируем и целимся в него caplog'ом,
    # иначе error-запись про тип ключа не доходит и len(...)==1 ложно падает.
    logging.getLogger(traffic_stats.__name__).disabled = False
    with caplog.at_level(logging.ERROR, logger=traffic_stats.__name__):
        out = traffic_stats.collect_all_active_nodes(session, 300)
    assert out == []
    key_type_logs = [r for r in caplog.records if "unsupported" in r.getMessage()]
    assert len(key_type_logs) == 1
