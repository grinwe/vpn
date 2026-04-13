"""Shared helpers and dependencies for the split /api router package.

Everything here is framework glue (``get_db`` FastAPI dep, audit-log writer,
actor resolution, subscription/invoice helpers used by multiple sub-routers).
Sub-modules (nodes, users, invoices, ...) import from this module rather than
from each other to keep the dependency graph a DAG.

Keep route handlers OUT of this file — it is intentionally helpers-only so
``from ._common import *`` stays safe and the sub-routers keep a single
obvious owner for each URL prefix.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import get_settings
from ..db import SessionLocal
from ..services.provisioning import ProvisioningOrchestrator

logger = logging.getLogger("app.api")

# Cache the settings singleton on import so every request handler that needs
# the custom actor header name doesn't redo the env lookup.
_settings = get_settings()
ADMIN_ACTOR_HEADER = _settings.admin_actor_header


def get_db():
    """FastAPI dependency — one ``SessionLocal`` per request, closed on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _audit(
    db: Session,
    actor: str,
    action: str,
    target_type: str,
    target_id: int | None,
    *,
    metadata: dict[str, Any] | None = None,
    actor_type: models.AuditActor = models.AuditActor.system,
) -> models.AuditLog:
    """Write an AuditLog row and commit.

    Commits on its own because most callers just want fire-and-forget
    audit trail writes — if you need to bundle the audit row with other
    changes into one transaction, either build the AuditLog row by hand
    or re-order your ``db.commit()`` around this call.
    """
    log = models.AuditLog(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        extra=metadata,
        actor_type=actor_type,
    )
    db.add(log)
    db.commit()
    return log


def _resolve_admin_actor(actor_header: str | None) -> tuple[str, models.AuditActor]:
    """Resolve the ``X-Admin-Actor`` header to an audit (actor, actor_type) pair.

    Missing header falls back to the literal ``"admin"`` — we always
    know the caller is an admin (the routes are gated on ``require_admin``)
    but we don't always know *which* operator, so the header is optional.
    """
    if actor_header:
        return actor_header, models.AuditActor.admin
    return "admin", models.AuditActor.admin


def _get_or_create_user(
    db: Session, telegram_id: str, email: str | None = None
) -> models.User:
    """Look up a user by ``telegram_id``, creating them on first sight.

    Used by every entry point where the bot/webapp is allowed to implicitly
    create users (invoice creation, subscription creation). Writes an
    audit row on creation so we can trace where each user came from.
    """
    user = db.query(models.User).filter_by(telegram_id=telegram_id).first()
    if not user:
        user = models.User(telegram_id=telegram_id, email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
        _audit(db, telegram_id, "user_created", "user", user.id)
    return user


def _get_user_from_payload(
    db: Session,
    user_id: int | None,
    telegram_id: str | None,
    email: str | None = None,
) -> models.User:
    """Resolve a user from whichever identifier the caller supplied.

    Numeric ``user_id`` is used as-is (404 if missing). A string
    ``telegram_id`` is routed through ``_get_or_create_user`` so the
    first invoice/subscription for a new bot user also bootstraps the
    User row. Raises 400 if both are missing — every endpoint calling
    this one genuinely needs *some* identifier.
    """
    if user_id is not None:
        user = db.get(models.User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    if telegram_id:
        return _get_or_create_user(db, telegram_id, email)

    raise HTTPException(status_code=400, detail="user_id or telegram_id is required")


def _create_subscription_for_user(
    db: Session,
    user: models.User,
    plan: models.Plan,
    node_id: int | None = None,
    device_name: str | None = None,
) -> tuple[models.Subscription, models.ProvisioningTask]:
    """Run the subscription-provisioning orchestrator for ``user`` + ``plan``.

    Shared by ``POST /subscriptions`` (direct create) and the invoice
    paid flow (``_mark_invoice_paid_core``). Wraps the orchestrator call
    in an audit write and 500-ifies any unexpected failure so the
    caller doesn't leak a half-committed sub.
    """
    orchestrator = ProvisioningOrchestrator(db)
    try:
        sub, task = orchestrator.provision_subscription(
            user, plan, node_id=node_id, device_name=device_name
        )
        db.refresh(sub)
        _audit(
            db,
            user.telegram_id or "unknown",
            "subscription_created",
            "subscription",
            sub.id,
        )
        return sub, task
    except Exception as exc:  # noqa: BLE001
        logger.exception("Provisioning failed for user %s", user.telegram_id)
        db.rollback()
        raise HTTPException(status_code=500, detail="Provisioning failed") from exc


# Re-export schemas so sub-routers can do ``from ._common import schemas``
# if they want a single import line. Most modules import from app.schemas
# directly — this is just a convenience.
__all__ = [
    "ADMIN_ACTOR_HEADER",
    "_audit",
    "_create_subscription_for_user",
    "_get_or_create_user",
    "_get_user_from_payload",
    "_resolve_admin_actor",
    "get_db",
    "logger",
    "models",
    "schemas",
]
