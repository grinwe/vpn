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


def _provisioning_key_works(host: str, *, port: int = 22) -> bool:
    """Проба доступа по provisioning-ключу: True, если пускает и команда прошла.

    Нужна, чтобы отличить (а) password-auth отключён после bootstrap'а (ключ
    реально стоит) от (б) хостер выдал неверный/устаревший root-пароль (ключ НЕ
    установлен) — оба дают одинаковый ``AuthenticationException`` при парольном
    входе. Best-effort: любой сбой (ключа нет, сеть, ключ не подошёл) → False."""
    key_path = os.getenv("ANSIBLE_PRIVATE_KEY_FILE") or os.getenv(
        "PROVISIONING_SSH_KEY"
    )
    if not key_path or not os.path.exists(key_path):
        return False
    try:
        import paramiko
    except ImportError:
        return False
    pkey = None
    for loader in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            pkey = loader.from_private_key_file(key_path)
            break
        except Exception:  # noqa: BLE001 — не тот тип ключа → пробуем дальше
            continue
    if pkey is None:
        return False
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=port, username=SSH_USER, pkey=pkey,
            timeout=15, banner_timeout=20, auth_timeout=20,
            look_for_keys=False, allow_agent=False,
        )
        _in, out, _err = client.exec_command("true", timeout=10)
        return out.channel.recv_exit_status() == 0
    except Exception:  # noqa: BLE001 — ключ не подошёл / нода недоступна
        return False
    finally:
        client.close()


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
            # Пароль не подошёл — причина НЕОДНОЗНАЧНА: либо (а) password-auth уже
            # отключён после bootstrap_node (ключ реально стоит), либо (б) хостер
            # выдал/сохранил неверный или устаревший root-пароль (ключ НЕ
            # установлен, ansible упадёт с publickey-denied). Пробуем ключ, чтобы
            # отличить (а) от (б) и не писать успокаивающий, но ложный лог.
            if _provisioning_key_works(host, port=port):
                logger.info(
                    "password auth rejected on %s, but provisioning key works "
                    "— key already present (password-auth off)", host,
                )
                return True
            logger.warning(
                "password auth rejected on %s AND provisioning key does NOT "
                "work — stale/invalid root password, key NOT installed; "
                "ansible will likely fail with 'Permission denied (publickey)'",
                host,
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


def reboot_via_ssh(host: str, *, port: int = 22) -> bool:
    """Graceful reboot по SSH через provisioning-ключ (нода уже с нашим ключом
    после bootstrap'а). True = команда reboot отправлена. Best-effort: фолбэк к
    API-reboot и единственный путь для нод без cloud-API. Reboot рвёт сессию —
    обрыв соединения после exec считаем успехом, не ошибкой."""
    key_path = os.getenv("ANSIBLE_PRIVATE_KEY_FILE") or os.getenv(
        "PROVISIONING_SSH_KEY"
    )
    if not key_path or not os.path.exists(key_path):
        logger.warning("reboot_via_ssh: provisioning key unavailable")
        return False
    try:
        import paramiko
    except ImportError:
        logger.warning("reboot_via_ssh: paramiko not installed")
        return False
    pkey = None
    for loader in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            pkey = loader.from_private_key_file(key_path)
            break
        except Exception:  # noqa: BLE001 — не тот тип ключа → пробуем дальше
            continue
    if pkey is None:
        logger.warning("reboot_via_ssh: could not load provisioning key %s", key_path)
        return False
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=port, username=SSH_USER, pkey=pkey,
            timeout=15, banner_timeout=20, auth_timeout=20,
            look_for_keys=False, allow_agent=False,
        )
        _in, out, _err = client.exec_command("systemctl reboot || reboot", timeout=10)
        try:
            out.channel.recv_exit_status()  # дожидаемся отправки (или обрыв = reboot пошёл)
        except Exception:  # noqa: BLE001 — соединение оборвалось из-за reboot
            pass
        logger.info("reboot_via_ssh: reboot sent to %s", host)
        return True
    except Exception as exc:  # noqa: BLE001 — нода недоступна / ключ не подошёл
        logger.warning("reboot_via_ssh failed for %s: %r", host, exc)
        return False
    finally:
        client.close()
