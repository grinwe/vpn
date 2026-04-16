"""The ``/api`` router package.

Assembles a single master router with prefix ``/api`` from leaf
modules. Sub-routers are declared without a prefix so they can be
unit-tested in isolation and so the paths inside each module read the
way they're exposed (``/plans``, not ``/api/plans``).

External callers import from this package — the legacy flat
``api.py`` used to live here and two modules still expect its surface:

* ``app.main`` imports ``router`` (re-exported as ``api_router``) and
  ``require_admin`` (re-exported from ``..auth``).
* ``app.api_webapp`` imports ``_subscriptions_for_user`` — the helper
  lives in :mod:`.users` and is re-exported at the package level.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..auth import require_admin
from . import (
    audit,
    autoscale,
    cloud,
    exits,
    health,
    health_pings,
    invoices,
    nodes,
    payments,
    plans,
    probes,
    subscriptions,
    tasks,
    tokens,
    traffic,
    users,
)
from .users import _subscriptions_for_user

router = APIRouter(prefix="/api")

# Order is for readability only — FastAPI resolves routes by prefix/path,
# not by include_router order. Grouped by domain to match the file layout.
router.include_router(health.router)
router.include_router(plans.router)
router.include_router(audit.router)
router.include_router(tokens.router)

router.include_router(nodes.router)
router.include_router(tasks.router)
router.include_router(subscriptions.router)
router.include_router(users.router)
router.include_router(traffic.router)
router.include_router(probes.router)
router.include_router(health_pings.router)

router.include_router(invoices.router)
router.include_router(payments.router)

router.include_router(cloud.router)
router.include_router(autoscale.router)
router.include_router(exits.router)


__all__ = [
    "_subscriptions_for_user",
    "require_admin",
    "router",
]
