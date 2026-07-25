"""Application-level secret encryption.

We store sensitive material (VPN credentials, cloud provider API tokens) in
the database. To limit the blast radius of a DB leak we encrypt them with
Fernet symmetric encryption, keyed by ``APP_SECRET_KEY`` from the environment.

Без ключа приложение НЕ СТАРТУЕТ (аудит 2026-07-25). Раньше здесь был тихий
fallback на plaintext с warning'ом в лог: одна потерянная переменная в деплое —
и все новые секреты ложились в базу открытым текстом, причём снаружи всё
выглядело рабочим, поэтому заметить это было практически нечем (в проде так
осело 28 кредов). Для локальной разработки/скриптов форточка осталась, но
теперь она ЯВНАЯ — ``ALLOW_PLAINTEXT_SECRETS=1``.

Зашифрованное значение префиксуется ``enc:v1:``, поэтому в одной колонке могут
сосуществовать зашифрованные и легаси-plaintext значения (нужно для постепенной
миграции и для чтения старых строк).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


def _derive_key(raw: str) -> bytes:
    """Derive a 32-byte url-safe base64 key from an arbitrary secret string."""
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


_ALLOW_PLAINTEXT_ENV = "ALLOW_PLAINTEXT_SECRETS"
_TRUTHY = ("1", "true", "on", "yes")


@lru_cache(maxsize=1)
def _cipher() -> Fernet | None:
    raw = os.getenv("APP_SECRET_KEY")
    if raw:
        return Fernet(_derive_key(raw))
    if os.getenv(_ALLOW_PLAINTEXT_ENV, "").strip().lower() in _TRUTHY:
        logger.warning(
            "APP_SECRET_KEY не задан, но выставлен %s — секреты в БД будут "
            "храниться ОТКРЫТЫМ ТЕКСТОМ. Только для локальной разработки.",
            _ALLOW_PLAINTEXT_ENV,
        )
        return None
    raise RuntimeError(
        "APP_SECRET_KEY не задан — отказываюсь работать, иначе секреты (креды "
        "клиентов, токены провайдеров, приватные ключи нод) молча лягут в БД "
        "открытым текстом. Задай APP_SECRET_KEY; для локальной разработки — "
        f"{_ALLOW_PLAINTEXT_ENV}=1."
    )


def assert_secrets_configured() -> None:
    """Проверка на старте процесса: падаем сразу, а не на первом секрете.

    Иначе мисконфиг всплыл бы уже в рантайме — на первой выдаче кредов, посреди
    провижининга, — и часть работы осталась бы наполовину сделанной.
    """
    _cipher()


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    cipher = _cipher()
    if cipher is None:
        return value
    token = cipher.encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        # Legacy / unencrypted payload — return as-is.
        return value
    cipher = _cipher()
    if cipher is None:
        logger.error("Encountered encrypted value but APP_SECRET_KEY is not set")
        return None
    payload = value[len(_PREFIX):]
    try:
        return cipher.decrypt(payload.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.exception("Failed to decrypt value — wrong APP_SECRET_KEY?")
        return None


# ── Control-channel HMAC ────────────────────────────────────────────────
# Custom-клиент (Phase B) шлёт сигналы на /api/client/report-failure через
# CF Worker. Чтобы бэк мог найти юзера по сигналу не передавая sub_token в
# plain, клиент шлёт `client_id_hmac = HMAC(APP_SECRET_KEY, sub_token)[:12]`
# (base64-url, 16 chars). Backend ищет Device по индексу client_id_hmac.
#
# Длина 12 байт = 96 бит — достаточно против brute force в обозримые сроки
# (~7.9e28 комбинаций). Конструкция HMAC-SHA256 криптографически stable —
# зная client_id_hmac, восстановить sub_token нельзя (вторая препятствие
# к злоупотреблениям если client_id куда-то утёк).
#
# APP_SECRET_KEY переиспользуется намеренно — он уже доступен в worker/
# backend контейнерах через env, не требует новой vault-переменной. При
# смене APP_SECRET_KEY все client_id_hmac инвалидируются — это редкое
# событие (compromise scenario), там придётся перевыпустить всем
# sub_token'ы в любом случае.

_CLIENT_ID_HMAC_LEN = 12  # bytes → 16 chars urlsafe-base64 без padding


def compute_client_id_hmac(sub_token: str | None) -> str | None:
    """Вернуть `client_id_hmac` для control-channel'а.

    None в → None out (для Device без sub_token, например только что
    создан и pending'ом ждёт provisioning'а). Иначе — 16-символьная
    urlsafe-base64 строка без padding (12 байт HMAC-SHA256 → 16 chars).

    Идемпотентен: дважды вызвать с тем же sub_token → тот же результат,
    что критично для DB-индекса (UNIQUE по client_id_hmac).

    Если APP_SECRET_KEY не задан — fallback на пустую строку и warning;
    в этом случае control-channel не работает (все client_id одинаковые),
    но запуск приложения не падает. Production должен задавать ключ.
    """
    if sub_token is None or sub_token == "":
        return None
    raw = os.getenv("APP_SECRET_KEY")
    if not raw:
        logger.warning(
            "compute_client_id_hmac: APP_SECRET_KEY not set, client_id will be empty"
        )
        return ""
    mac = hmac.new(
        raw.encode("utf-8"),
        sub_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()[:_CLIENT_ID_HMAC_LEN]
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
