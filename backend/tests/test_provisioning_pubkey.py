"""provisioning_pubkey() — fallback на PROVISIONING_SSH_KEY.

env.j2 прокидывает путь к provisioning-ключу как PROVISIONING_SSH_KEY, а не
ANSIBLE_PRIVATE_KEY_FILE. Без fallback'а VDSina-spawn падал (нет pubkey для
авто-регистрации ssh-ключа на боксе).
"""
from __future__ import annotations


from app.services import ssh_bootstrap


def test_pubkey_falls_back_to_provisioning_ssh_key(tmp_path, monkeypatch):
    key = tmp_path / "provisioning_key"
    key.write_text("PRIVATE-KEY-STUB\n")
    (tmp_path / "provisioning_key.pub").write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc vpn-provisioning\n"
    )
    monkeypatch.delenv("ANSIBLE_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.setenv("PROVISIONING_SSH_KEY", str(key))

    # .pub читается → коммент срезан, остаётся "<type> <base64>"
    assert (
        ssh_bootstrap.provisioning_pubkey()
        == "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc"
    )


def test_pubkey_none_when_no_key_env(monkeypatch):
    monkeypatch.delenv("ANSIBLE_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.delenv("PROVISIONING_SSH_KEY", raising=False)
    assert ssh_bootstrap.provisioning_pubkey() is None
