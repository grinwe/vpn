"""finding #97 — skip ssh-стадии не должен выглядеть как реальная недоступность.

При отсутствии paramiko или файла ssh-ключа ``_probe_ssh`` отдаёт check со
статусом ``skip`` (не смогли проверить). ``PathProbeResult.ssh_skipped``
позволяет вызывающему (reachability-тик) отличить это от «проверили —
недоступно» и НЕ метить цель unreachable по конфиг-ошибке контроллера.
"""
from __future__ import annotations

from app.services import diagnostics


def test_probe_ssh_missing_key_is_skip(monkeypatch, tmp_path):
    """Нет файла ключа → ssh-стадия = skip (а не fail), ssh_ok=False."""
    missing = tmp_path / "no_such_key"
    monkeypatch.setenv("ANSIBLE_PRIVATE_KEY_FILE", str(missing))
    monkeypatch.delenv("PROVISIONING_SSH_KEY", raising=False)

    ok, check = diagnostics._probe_ssh("198.51.100.7", 22)

    assert ok is False
    assert check["status"] == "skip"


def test_run_probe_sets_ssh_skipped_flag(monkeypatch):
    """run_local_path_probe помечает ssh_skipped, когда ssh-чек = skip.

    ping/tcp замоканы, чтобы не ходить в сеть; проверяем только проброс флага.
    """
    monkeypatch.setattr(
        diagnostics, "_probe_ping",
        lambda host, **k: diagnostics._check("ping", "fail", message="down"),
    )
    monkeypatch.setattr(
        diagnostics, "_probe_tcp",
        lambda host, port, **k: (False, diagnostics._check("tcp", "fail")),
    )
    monkeypatch.setattr(
        diagnostics, "_probe_ssh",
        lambda host, ssh_port: (False, diagnostics._check("ssh", "skip", message="нет ключа")),
    )
    monkeypatch.setattr(
        diagnostics, "_probe_traceroute",
        lambda host, **k: diagnostics._check("traceroute", "info"),
    )

    res = diagnostics.run_local_path_probe("198.51.100.7", ssh_port=22)

    assert res.ssh_ok is False
    assert res.ssh_skipped is True


def test_run_probe_real_ssh_fail_not_skipped(monkeypatch):
    """Реальный ssh-fail (проверили — недоступно) не выставляет ssh_skipped."""
    monkeypatch.setattr(
        diagnostics, "_probe_ping",
        lambda host, **k: diagnostics._check("ping", "ok"),
    )
    monkeypatch.setattr(
        diagnostics, "_probe_tcp",
        lambda host, port, **k: (True, diagnostics._check("tcp", "ok")),
    )
    monkeypatch.setattr(
        diagnostics, "_probe_ssh",
        lambda host, ssh_port: (False, diagnostics._check("ssh", "fail", message="refused")),
    )
    monkeypatch.setattr(
        diagnostics, "_probe_traceroute",
        lambda host, **k: diagnostics._check("traceroute", "info"),
    )

    res = diagnostics.run_local_path_probe("198.51.100.7", ssh_port=22)

    assert res.ssh_ok is False
    assert res.ssh_skipped is False
