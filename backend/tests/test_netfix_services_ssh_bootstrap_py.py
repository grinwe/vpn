"""Netfix-аудит #6: ensure_provisioning_key не должен трактовать неверный
root-пароль как «ключ уже установлен».

При провале парольной аутентификации функция обязана дизамбигуировать причину
пробой доступа по provisioning-ключу:
- ключ пускает  → (а) password-auth отключён, ключ реально стоит → True;
- ключ НЕ пускает → (б) неверный/устаревший пароль, ключ НЕ установлен → False
  с явным warning (а не успокаивающим «assuming key present»).
"""
from __future__ import annotations

import logging
import sys
import types

import pytest

from app.services import ssh_bootstrap


class _FakeAuthError(Exception):
    pass


class _FakeChannel:
    def recv_exit_status(self) -> int:
        return 0


class _FakeStd:
    channel = _FakeChannel()

    def read(self) -> bytes:  # pragma: no cover — не используется на этом пути
        return b""


class _FakeKey:
    @classmethod
    def from_private_key_file(cls, path):  # noqa: ANN001
        return cls()


class _FakeSSHClient:
    # управляется классовым флагом: пускает ли provisioning-ключ
    key_works = True

    def __init__(self) -> None:
        self._by_key = False

    def set_missing_host_key_policy(self, policy) -> None:  # noqa: ANN001
        pass

    def connect(self, **kw) -> None:  # noqa: ANN003
        if kw.get("password") is not None:
            # парольный вход всегда отвергается — воспроизводим спорный случай
            raise _FakeAuthError("password rejected")
        # вход по ключу (pkey задан)
        self._by_key = True
        if not _FakeSSHClient.key_works:
            raise _FakeAuthError("publickey rejected")

    def exec_command(self, cmd, timeout=None):  # noqa: ANN001, ANN003
        return _FakeStd(), _FakeStd(), _FakeStd()

    def close(self) -> None:
        pass


def _install_fake_paramiko(monkeypatch, *, key_works: bool) -> None:
    _FakeSSHClient.key_works = key_works
    fake = types.ModuleType("paramiko")
    fake.SSHClient = _FakeSSHClient
    fake.AutoAddPolicy = lambda: object()
    fake.AuthenticationException = _FakeAuthError
    fake.Ed25519Key = _FakeKey
    fake.RSAKey = _FakeKey
    fake.ECDSAKey = _FakeKey
    monkeypatch.setitem(sys.modules, "paramiko", fake)


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    p = tmp_path / "provisioning_key"
    p.write_text("dummy")
    monkeypatch.setenv("ANSIBLE_PRIVATE_KEY_FILE", str(p))
    monkeypatch.setattr(
        ssh_bootstrap, "provisioning_pubkey", lambda: "ssh-ed25519 AAAAdummy"
    )
    return p


def test_auth_fail_but_key_works_reports_present(keyfile, monkeypatch, caplog):
    """Случай (а): пароль отвергнут, но ключ пускает → True, без warning."""
    _install_fake_paramiko(monkeypatch, key_works=True)
    # alembic fileConfig(disable_existing_loggers) на старте харнесса глушит
    # уже созданный логгер модуля — ре-активируем и целимся в него caplog'ом,
    # иначе записи не доходят и негативная проверка ложно-зелёная.
    logging.getLogger(ssh_bootstrap.__name__).disabled = False
    with caplog.at_level("WARNING", logger=ssh_bootstrap.__name__):
        ok = ssh_bootstrap.ensure_provisioning_key("1.2.3.4", "badpass")
    assert ok is True
    # не должно быть warning об отсутствии ключа
    assert not any("NOT installed" in r.getMessage() for r in caplog.records)


def test_auth_fail_and_key_fails_warns_not_installed(keyfile, monkeypatch, caplog):
    """Случай (б): пароль отвергнут И ключ не пускает → False + явный warning."""
    _install_fake_paramiko(monkeypatch, key_works=False)
    # См. коммент выше: ре-активируем заглушённый alembic'ом логгер модуля.
    logging.getLogger(ssh_bootstrap.__name__).disabled = False
    with caplog.at_level("WARNING", logger=ssh_bootstrap.__name__):
        ok = ssh_bootstrap.ensure_provisioning_key("1.2.3.4", "stalepass")
    assert ok is False
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "NOT installed" in joined
