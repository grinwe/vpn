"""Audit-fix wave 3 — finding 118.

Чтение лога sharing-нарушений должно быть атомарным: rename (mv) в
уникальное имя вместо `cat && truncate -s 0`, иначе события, дописанные
энфорсером между cat и truncate, стираются непрочитанными.

Тесты чисто юнитовые: paramiko и `_ssh_run` подменяются, БД не нужна.
"""
from __future__ import annotations

import sys
import types

from app.services import traffic_stats


def _install_fake_paramiko(monkeypatch):
    """Подсунуть фейковый paramiko, чтобы collect_node_stats дошёл до SSH."""

    class _FakeClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, *a, **k):
            pass

        def close(self):
            pass

    fake = types.ModuleType("paramiko")
    fake.Ed25519Key = types.SimpleNamespace(
        from_private_key_file=lambda path: object()
    )
    # Сетевой аудит (finding 4): _load_provisioning_pkey перебирает
    # (Ed25519Key, RSAKey, ECDSAKey) — само построение этого кортежа читает
    # все три атрибута модуля, поэтому RSAKey/ECDSAKey обязаны существовать на
    # фейке, иначе AttributeError ещё до попытки загрузки. Ed25519 подходит
    # первым, так что эти загрузчики не вызываются.
    fake.RSAKey = types.SimpleNamespace(
        from_private_key_file=lambda path: object()
    )
    fake.ECDSAKey = types.SimpleNamespace(
        from_private_key_file=lambda path: object()
    )
    fake.SSHClient = _FakeClient
    fake.AutoAddPolicy = lambda: object()
    monkeypatch.setitem(sys.modules, "paramiko", fake)
    # ключ существует
    monkeypatch.setattr(traffic_stats.os.path, "exists", lambda p: True)


def _make_ssh_stub(recorded, violation_output):
    """Фейковый _ssh_run: statsquery отдаёт 'протокол не установлен',
    команда чтения нарушений — переданный JSONL."""

    def _fake(client, command):
        recorded.append(command)
        if "sharing_violations.jsonl" in command:
            return 0, violation_output, ""
        # statsquery per protocol → exit!=0 (протокол не установлен)
        return 1, "", "connection refused"

    return _fake


def test_violation_read_is_atomic_rename(monkeypatch):
    """Команда чтения лога нарушений использует mv (атомарный rename),
    а не cat && truncate — иначе теряются дописанные события."""
    _install_fake_paramiko(monkeypatch)
    monkeypatch.setenv("SHARING_ENFORCEMENT_ENABLED", "1")

    recorded: list[str] = []
    monkeypatch.setattr(
        traffic_stats, "_ssh_run", _make_ssh_stub(recorded, "")
    )

    node = types.SimpleNamespace(id=1, name="n1", host="1.2.3.4", ssh_port=22)
    traffic_stats.collect_node_stats(node)

    viol_cmds = [c for c in recorded if "sharing_violations.jsonl" in c]
    assert viol_cmds, "команда чтения нарушений не была вызвана"
    cmd = viol_cmds[0]
    assert "mv " in cmd, "чтение должно забирать файл через rename (mv)"
    assert "truncate" not in cmd, "truncate теряет дописанные между cat/truncate события"


def test_violations_parsed_after_rename(monkeypatch):
    """После атомарного забора строки JSONL корректно парсятся в
    SharingViolation."""
    _install_fake_paramiko(monkeypatch)
    monkeypatch.setenv("SHARING_ENFORCEMENT_ENABLED", "1")

    jsonl = (
        '{"ts":"2026-07-04T10:00:00Z","email":"a@x","ips":["1.1.1.1","2.2.2.2"],'
        '"ip_count":2,"action":"warn","severity":"warning"}\n'
        '{"ts":"2026-07-04T10:01:00Z","email":"b@x","ips":["3.3.3.3"],'
        '"ip_count":1,"action":"kick","severity":"kick"}'
    )
    recorded: list[str] = []
    monkeypatch.setattr(
        traffic_stats, "_ssh_run", _make_ssh_stub(recorded, jsonl)
    )

    node = types.SimpleNamespace(id=7, name="n7", host="1.2.3.4", ssh_port=22)
    result = traffic_stats.collect_node_stats(node)

    assert len(result.sharing_violations) == 2
    first = result.sharing_violations[0]
    assert first.email == "a@x"
    assert first.ip_count == 2
    assert first.severity == "warning"
    assert result.sharing_violations[1].severity == "kick"


def test_violations_skipped_when_flag_off(monkeypatch):
    """При SHARING_ENFORCEMENT_ENABLED=0 команда чтения не выполняется."""
    _install_fake_paramiko(monkeypatch)
    monkeypatch.setenv("SHARING_ENFORCEMENT_ENABLED", "0")

    recorded: list[str] = []
    monkeypatch.setattr(
        traffic_stats, "_ssh_run", _make_ssh_stub(recorded, "irrelevant")
    )

    node = types.SimpleNamespace(id=3, name="n3", host="1.2.3.4", ssh_port=22)
    result = traffic_stats.collect_node_stats(node)

    assert not any("sharing_violations.jsonl" in c for c in recorded)
    assert result.sharing_violations == []
