"""finding #6 (сетевой аудит) — фильтрованный ICMP не должен читаться как блэкаут.

``_probe_ping`` теперь отдаёт 'warn' (а не 'fail') при отсутствии ICMP-ответа,
а ``run_local_path_probe`` строит summary по факту tcp:ssh/ssh, а не по ICMP.
Проверяем: ping не красный при фильтрованном ICMP; summary не заявляет полный
блэкаут; ``ping_ok`` не считает no-reply pingable.
"""
from __future__ import annotations

from app.services import diagnostics


def test_ping_no_reply_is_warn_not_fail(monkeypatch):
    """100% loss → статус 'warn' с флагом no_reply, а не 'fail'."""

    class _CP:
        stdout = "3 packets transmitted, 0 received, 100% packet loss"
        returncode = 1

    monkeypatch.setattr(diagnostics, "_run", lambda cmd, timeout: _CP())

    chk = diagnostics._probe_ping("198.51.100.7")

    assert chk["status"] == "warn"
    assert chk["details"].get("no_reply") is True


def test_ping_timeout_is_warn_not_fail(monkeypatch):
    """ping timeout (нет ни одного ответа) → 'warn'+no_reply, не 'fail'."""
    import subprocess

    def _raise(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(diagnostics, "_run", _raise)

    chk = diagnostics._probe_ping("198.51.100.7")

    assert chk["status"] == "warn"
    assert chk["details"].get("no_reply") is True


def _mock_stages(monkeypatch, *, ping_check, tcp_ok, ssh_ok):
    monkeypatch.setattr(diagnostics, "_probe_ping", lambda host, **k: ping_check)
    monkeypatch.setattr(
        diagnostics, "_probe_tcp",
        lambda host, port, **k: (tcp_ok, diagnostics._check("tcp", "ok" if tcp_ok else "fail")),
    )
    monkeypatch.setattr(
        diagnostics, "_probe_ssh",
        lambda host, ssh_port: (
            ssh_ok, diagnostics._check("ssh", "ok" if ssh_ok else "fail"),
        ),
    )
    monkeypatch.setattr(
        diagnostics, "_probe_traceroute",
        lambda host, **k: diagnostics._check("traceroute", "info"),
    )


def test_summary_no_blackout_claim_on_filtered_icmp(monkeypatch):
    """ICMP без ответа + ssh закрыт → summary НЕ говорит «ни ping, ни ssh»."""
    ping = diagnostics._check("ping", "warn", message="filtered", details={"no_reply": True})
    _mock_stages(monkeypatch, ping_check=ping, tcp_ok=False, ssh_ok=False)

    res = diagnostics.run_local_path_probe("198.51.100.7", ssh_port=22)

    assert res.ping_ok is False  # no-reply не считается pingable
    assert "ни ping, ни ssh" not in res.summary
    assert "SSH недоступен" in res.summary


def test_summary_tcp_ssh_open_but_ssh_down(monkeypatch):
    """tcp:ssh открыт, ssh-хендшейк не прошёл → summary про сервис ssh, не сеть."""
    ping = diagnostics._check("ping", "warn", message="filtered", details={"no_reply": True})
    _mock_stages(monkeypatch, ping_check=ping, tcp_ok=True, ssh_ok=False)

    res = diagnostics.run_local_path_probe("198.51.100.7", ssh_port=22)

    assert "tcp:ssh открыт" in res.summary


def test_ping_ok_true_only_on_real_reply(monkeypatch):
    """Реальный ICMP-ответ (ok) → ping_ok True; ssh закрыт → summary про ping."""
    ping = diagnostics._check("ping", "ok", latency_ms=5, message="avg 5.0 ms")
    _mock_stages(monkeypatch, ping_check=ping, tcp_ok=False, ssh_ok=False)

    res = diagnostics.run_local_path_probe("198.51.100.7", ssh_port=22)

    assert res.ping_ok is True
    assert "host пингуется" in res.summary
