"""Application-level secret encryption.

We store sensitive material (VPN credentials, cloud provider API tokens) in
the database. To limit the blast radius of a DB leak we encrypt them with
Fernet symmetric encryption, keyed by ``APP_SECRET_KEY`` from the environment.

If the key is not configured we fall back to plaintext but log a warning —
this keeps local development painless while making the prod misconfiguration
visible in logs. The encrypted payload is prefixed with ``enc:v1:`` so we can
mix encrypted and legacy plaintext values in the same column during a rolling
rollout.
"""
from __future__ import annotations

import base64
import hashlib
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


@lru_cache(maxsize=1)
def _cipher() -> Fernet | None:
    raw = os.getenv("APP_SECRET_KEY")
    if not raw:
        logger.warning(
            "APP_SECRET_KEY is not set — secrets in the DB will be stored in plaintext. "
            "DO NOT run like this in production."
        )
        return None
    return Fernet(_derive_key(raw))


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
