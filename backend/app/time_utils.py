"""Timezone helpers.

`datetime.utcnow()` is deprecated in Python 3.12 because it returns a
naive datetime that silently pretends to be UTC. All of our SQLAlchemy
columns are still naive `DateTime` — migrating them to tz-aware is a
separate schema change — so we centralize the "give me a naive UTC
timestamp" helper here. When we later migrate columns to tz-aware, we
flip `utcnow()` to return the aware version in one place.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC datetime, matching the existing DB column semantics."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
