"""Scoped API token authentication.

Two kinds of callers hit this backend:

* **Admins** — humans and the bot — send ``X-Admin-Token`` and have every
  scope implicitly. The admin token is a single shared secret in env.
* **Machines** — probe rigs, node traffic collectors — send
  ``X-Api-Token`` and are limited to the scopes listed on their token row
  in ``api_tokens``. Only the SHA-256 hash is stored; the plaintext is
  shown once at creation and never again.

``require_scope("probe:read")`` is used as a FastAPI dependency. It
returns an :class:`AuthPrincipal` the route can inspect for audit logging
(``principal.label`` → ``"admin"`` or ``"token:<name>"``).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from .time_utils import utcnow
from typing import Callable

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .db import SessionLocal

# Canonical scope identifiers. Keep this list the single source of truth —
# routes reference the constants, the admin UI renders the list, tests
# assert against it.
SCOPE_PROBE_READ = "probe:read"
SCOPE_PROBE_WRITE = "probe:write"
SCOPE_TRAFFIC_WRITE = "traffic:write"

ALL_SCOPES: tuple[str, ...] = (
    SCOPE_PROBE_READ,
    SCOPE_PROBE_WRITE,
    SCOPE_TRAFFIC_WRITE,
)


@dataclass
class AuthPrincipal:
    kind: str  # "admin" | "token"
    label: str  # "admin" | "token:<name>"
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        # Admin is all-scopes by construction; scoped tokens must list it.
        return self.kind == "admin" or scope in self.scopes


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str]:
    """Return ``(plaintext, hash)``. Caller must display plaintext once."""
    plaintext = secrets.token_urlsafe(32)
    return plaintext, _hash_token(plaintext)


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _resolve_principal(
    x_admin_token: str | None,
    x_api_token: str | None,
    db: Session,
) -> AuthPrincipal:
    settings = get_settings()

    # Admin path — constant-time compare, identical to legacy require_admin.
    if x_admin_token:
        if not settings.admin_api_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="admin token missing",
            )
        if not hmac.compare_digest(x_admin_token, settings.admin_api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid admin token",
            )
        return AuthPrincipal(kind="admin", label="admin", scopes=frozenset(ALL_SCOPES))

    # Scoped token path — hash lookup, check is_active, update last_used_at.
    if x_api_token:
        token_hash = _hash_token(x_api_token)
        row = (
            db.query(models.ApiToken)
            .filter(models.ApiToken.token_hash == token_hash)
            .one_or_none()
        )
        if row is None or not row.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid api token",
            )
        # Best-effort last_used timestamp. Don't let a commit failure here
        # block the actual request.
        try:
            row.last_used_at = utcnow()
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return AuthPrincipal(
            kind="token",
            label=f"token:{row.name}",
            scopes=frozenset(row.scopes or []),
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing auth token",
    )


def require_admin(x_admin_token: str | None = Header(default=None)) -> str:
    """Shared-secret admin auth. Used by legacy routes that don't need scopes."""
    settings = get_settings()
    if not settings.admin_api_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin token missing",
        )
    if not x_admin_token or not hmac.compare_digest(
        x_admin_token.encode("utf-8"),
        settings.admin_api_token.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin token",
        )
    return x_admin_token


def optional_admin(x_admin_token: str | None = Header(default=None)) -> str | None:
    """Like require_admin, but returns None if no header was sent."""
    if x_admin_token:
        return require_admin(x_admin_token)
    return None


def require_scope(scope: str) -> Callable[..., AuthPrincipal]:
    """FastAPI dependency factory enforcing a single scope."""

    def _dep(
        x_admin_token: str | None = Header(default=None),
        x_api_token: str | None = Header(default=None),
        db: Session = Depends(_get_db),
    ) -> AuthPrincipal:
        principal = _resolve_principal(x_admin_token, x_api_token, db)
        if not principal.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"token lacks required scope: {scope}",
            )
        return principal

    return _dep
