"""First-connect SSH-ключ для cloud-нод без инъекции ключа (4vps).

4vps ``buyServer`` НЕ принимает SSH-ключ — свежая нода поднимается только с
``root`` + паролем (его отдаёт API, мы храним в
``VPNNode.provider_root_password_enc``). Ansible ходит по ключу
``provisioning_key`` → без ключа в ``authorized_keys`` он не зайдёт
(``Permission denied (publickey,password)``). Поэтому ПЕРЕД ``site.yml``
заходим на ноду по root-паролю и дописываем наш публичный ключ.

Запускается в worker-контейнере (там примонтирован приватный
``provisioning_key`` = ``ANSIBLE_PRIVATE_KEY_FILE``, из него деривим публичную
половину — без отдельного хранения и дрейфа). Идемпотентно (``grep -qxF``) и
терпимо к провалу: если на ноде уже отключён password-auth (прошла
``bootstrap_node``) — ключ и так стоит, просто выходим.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

SSH_USER = "root"
# Свежая нода / после reinstall поднимает sshd не сразу — ретраим подключение.
_CONNECT_RETRIES = 12
_CONNECT_INTERVAL = 10  # ≈2 мин суммарно ожидания SSH


def provisioning_pubkey() -> str | None:
    """Публичная половина provisioning-ключа.

    Сначала ``<key>.pub`` (если лежит рядом — не требует passphrase), затем
    дерив из приватного файла через paramiko (для passphrase-less ключа)."""
    # env.j2 прокидывает путь как PROVISIONING_SSH_KEY (не ANSIBLE_PRIVATE_KEY_FILE)
    # — даём тот же fallback, что diagnostics/traffic_stats/relay_link_health/
    # subscriptions. Без него VDSina-spawn падал: _ensure_key_id не мог достать
    # pubkey для авто-регистрации ключа на боксе (ssh-ключ не задан → 400).
    key_path = os.getenv("ANSIBLE_PRIVATE_KEY_FILE") or os.getenv(
        "PROVISIONING_SSH_KEY"
    )
    if not key_path:
        return None
    pub_path = f"{key_path}.pub"
    if os.path.exists(pub_path):
        try:
            with open(pub_path, encoding="utf-8") as fh:
                line = fh.read().strip()
            if line:
                # обрезаем коммент — оставляем "<type> <base64>"
                parts = line.split()
                return f"{parts[0]} {parts[1]}" if len(parts) >= 2 else line
        except OSError:
            pass
    if not os.path.exists(key_path):
        return None
    try:
        import paramiko
    except ImportError:
        return None
    for loader in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            k = loader.from_private_key_file(key_path)
            return f"{k.get_name()} {k.get_base64()}"
        except Exception:  # noqa: BLE001 — не тот тип / passphrase → пробуем дальше
            continue
    return None


def ensure_provisioning_key(host: str, password: str, *, port: int = 22) -> bool:
    """Best-effort: зайти на ``host`` по root-паролю и дописать
    provisioning-pubkey в ``~/.ssh/authorized_keys``. Возвращает True, если
    ключ установлен. Ретраит подключение (нода после заказа/reinstall поднимает
    SSH не сразу). Никогда не бросает — bootstrap не должен падать из-за этого.
    """
    pubkey = provisioning_pubkey()
    if not pubkey:
        logger.warning(
            "ensure_provisioning_key: provisioning pubkey unavailable "
            "(ANSIBLE_PRIVATE_KEY_FILE missing or unreadable)"
        )
        return False
    if not password:
        return False
    try:
        import paramiko
    except ImportError:
        logger.warning("ensure_provisioning_key: paramiko not installed")
        return False

    # single-quote-safe: pubkey = "<type> <base64>" (без коммента, без кавычек).
    cmd = (
        "install -d -m700 ~/.ssh && touch ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys && "
        f"grep -qxF '{pubkey}' ~/.ssh/authorized_keys || "
        f"echo '{pubkey}' >> ~/.ssh/authorized_keys"
    )
    last_err: str | None = None
    for attempt in range(1, _CONNECT_RETRIES + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=port,
                username=SSH_USER,
                password=password,
                timeout=15,
                banner_timeout=20,
                auth_timeout=20,
                look_for_keys=False,
                allow_agent=False,
            )
            _stdin, stdout, stderr = client.exec_command(cmd, timeout=20)
            rc = stdout.channel.recv_exit_status()
            if rc == 0:
                logger.info(
                    "provisioning key installed on %s (attempt %d/%d)",
                    host, attempt, _CONNECT_RETRIES,
                )
                return True
            last_err = stderr.read().decode("utf-8", "replace")[:200]
            logger.warning("key-install cmd rc=%s on %s: %s", rc, host, last_err)
            return False
        except paramiko.AuthenticationException:
            # Пароль не подошёл → password-auth уже отключён (нода прошла
            # bootstrap_node) ⇒ ключ уже стоит. Не ретраим, не считаем ошибкой.
            logger.info(
                "password auth rejected on %s — assuming provisioning key "
                "already present", host,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — SSH ещё не поднялся / сеть
            last_err = repr(exc)
            if attempt < _CONNECT_RETRIES:
                time.sleep(_CONNECT_INTERVAL)
        finally:
            client.close()
    logger.warning(
        "ensure_provisioning_key gave up on %s after %d attempts: %s",
        host, _CONNECT_RETRIES, last_err,
    )
    return False
