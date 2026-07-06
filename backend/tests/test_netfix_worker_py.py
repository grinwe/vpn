"""Сетевой аудит — фиксы worker.py (reachability-тик).

Покрывают чистые хелперы, введённые фиксами:

* Finding #1 — ``_node_vpn_tcp_ports`` / ``_probe_port_open``: reachability-тик
  при живом SSH дополнительно проверяет TCP VPN-порт(ы) ноды, чтобы упавший
  xray на живой по SSH ноде читался как ``degraded``, а не «зелёная». UDP
  (hysteria2) в TCP-пробу не попадает — нет ложного degraded на hysteria-only.
* Finding #3 — ``_controller_has_network``: self-check связности контроллера
  перед пер-нодовыми пробами, чтобы сетевой сбой воркера не пометил весь флот
  ложным DOWN.

Findings #2 (flapping) и #7 (consecutive-fail) исправлены внутри тела тика
(gap-guard) / требуют схемной колонки — см. отчёт; здесь юнит-тестируются
чистые хелперы без БД.
"""
from __future__ import annotations

from types import SimpleNamespace

from app import worker


def _cfg(protocol: str, port: int, enabled: bool = True):
    # protocol как объект с .value (как у Enum) — хелпер читает getattr(.,'value').
    return SimpleNamespace(
        protocol=SimpleNamespace(value=protocol), port=port, is_enabled=enabled
    )


# ── Finding #1: _node_vpn_tcp_ports ──────────────────────────────────────

def test_vpn_tcp_ports_excludes_udp_hysteria_and_disabled():
    node = SimpleNamespace(
        configs=[
            _cfg("vless-reality", 443),
            _cfg("vless-xhttp", 443),      # дубль порта — схлопывается
            _cfg("vless-ws-cdn", 8443),
            _cfg("hysteria2", 443),        # UDP — исключается
            _cfg("shadowtls+shadowsocks", 9443, enabled=False),  # выключен
        ]
    )
    assert worker._node_vpn_tcp_ports(node) == [443, 8443]


def test_vpn_tcp_ports_hysteria_only_yields_empty():
    # hysteria-only нода: TCP-портов нет → degraded по TCP не сработает.
    node = SimpleNamespace(configs=[_cfg("hysteria2", 443)])
    assert worker._node_vpn_tcp_ports(node) == []


def test_vpn_tcp_ports_env_override(monkeypatch):
    monkeypatch.setenv("NODE_VPN_PROBE_PORTS", "443, 8443 , нехочу, 443")
    node = SimpleNamespace(configs=[_cfg("hysteria2", 443)])  # игнорируется override-ом
    assert worker._node_vpn_tcp_ports(node) == [443, 8443]


def test_vpn_tcp_ports_no_configs():
    assert worker._node_vpn_tcp_ports(SimpleNamespace(configs=None)) == []


# ── Finding #1: _probe_port_open ─────────────────────────────────────────

def _probe(checks):
    return SimpleNamespace(checks=checks)


def test_probe_port_open_true_on_ok():
    probe = _probe([{"name": "tcp:443", "status": "ok"}])
    assert worker._probe_port_open(probe, 443) is True


def test_probe_port_open_false_on_fail():
    probe = _probe([{"name": "tcp:443", "status": "fail"}])
    assert worker._probe_port_open(probe, 443) is False


def test_probe_port_open_true_when_not_probed():
    # Порт не пробовали (записи нет) — НЕ считаем закрытым (без ложного degraded).
    probe = _probe([{"name": "tcp:22 (ssh)", "status": "ok"}])
    assert worker._probe_port_open(probe, 443) is True


def test_degraded_requires_all_ports_closed():
    # Логика тика: degraded только когда ЗАКРЫТЫ ВСЕ порты.
    vpn_ports = [443, 8443]
    probe = _probe([
        {"name": "tcp:443", "status": "fail"},
        {"name": "tcp:8443", "status": "ok"},  # один открыт
    ])
    degraded = bool(vpn_ports) and all(
        not worker._probe_port_open(probe, p) for p in vpn_ports
    )
    assert degraded is False

    probe_all_down = _probe([
        {"name": "tcp:443", "status": "fail"},
        {"name": "tcp:8443", "status": "fail"},
    ])
    degraded_all = bool(vpn_ports) and all(
        not worker._probe_port_open(probe_all_down, p) for p in vpn_ports
    )
    assert degraded_all is True


# ── Finding #3: _controller_has_network ──────────────────────────────────

def test_controller_network_disabled_when_anchors_empty(monkeypatch):
    monkeypatch.setenv("NODE_CONTROLLER_ANCHORS", "")
    assert worker._controller_has_network() is True


def test_controller_network_false_when_all_anchors_fail(monkeypatch):
    monkeypatch.setenv("NODE_CONTROLLER_ANCHORS", "1.1.1.1:443,8.8.8.8:443")

    import socket as _socket

    def _boom(*a, **k):
        raise OSError("no route to host")

    monkeypatch.setattr(_socket, "create_connection", _boom)
    assert worker._controller_has_network() is False


def test_controller_network_true_when_one_anchor_ok(monkeypatch):
    monkeypatch.setenv("NODE_CONTROLLER_ANCHORS", "1.1.1.1:443,8.8.8.8:443")
    calls = {"n": 0}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _maybe(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("first anchor down")
        return _Conn()

    import socket as _socket
    monkeypatch.setattr(_socket, "create_connection", _maybe)
    assert worker._controller_has_network() is True
