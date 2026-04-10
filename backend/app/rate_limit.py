"""Shared SlowAPI rate-limiter instance.

Imported by main.py (to wire middleware) and by route modules (to apply
per-route ``@limiter.limit(...)`` decorators).
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

_storage_uri = os.getenv("SLOWAPI_STORAGE_URI", "memory://")

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["300/minute", "60/second"],
    storage_uri=_storage_uri,
)
