"""Global sliding-window throttle on cold-path subscription provisioning.

Motivation — 2026-04-15 incident: ~250 Telegram bots poured into the bot
within 2 minutes, clicked through /plans → activate, and every one of
them missed the warm pool. The cold path spawns an Ansible run per
subscription; the MAX_CONCURRENT_ANSIBLE semaphore kept parallelism in
check, but the backlog of serial runs thrashed a single xray node until
it crashed.

This throttle sits *only* on the cold path in
``ProvisioningOrchestrator.provision_subscription``: warm-pool fast-path
assignments bypass it entirely (they don't touch Ansible), and
``reprovision_subscription`` (migrations, self-service regen) bypasses
too — those are system- or admin-triggered, not the DDoS vector.

Tunables (env):

* ``COLD_PROVISION_MAX_PER_WINDOW`` — max cold provisions per window
  (default 5). A legitimate "friend shared the link, 10 people tap at
  once" burst loses users past the limit; compensate by keeping the
  warm pool deep enough that organic traffic stays on the fast path.
* ``COLD_PROVISION_WINDOW_SECONDS`` — window size (default 60).

The window is global (single-process, per-backend-replica) — that's
intentional. Each replica has its own Ansible worker pool, so a
per-replica limit lines up with what actually hurts the nodes.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)


class ColdPathThrottled(Exception):
    """Raised when cold-path provisioning is currently rate-limited.

    API layer should translate this to HTTP 503 with a ``Retry-After``
    header so clients back off instead of hammering.
    """

    def __init__(self, retry_after_seconds: int):
        super().__init__(
            f"Cold-path provisioning throttled; retry in {retry_after_seconds}s"
        )
        self.retry_after_seconds = retry_after_seconds


_lock = threading.Lock()
_bucket: deque[float] = deque()


def _limits() -> tuple[int, int]:
    max_n = int(os.getenv("COLD_PROVISION_MAX_PER_WINDOW", "5"))
    window = int(os.getenv("COLD_PROVISION_WINDOW_SECONDS", "60"))
    return max_n, window


def check_and_consume() -> None:
    """Record a cold-path provisioning attempt or raise if over quota."""
    max_n, window = _limits()
    now = time.monotonic()
    with _lock:
        while _bucket and _bucket[0] <= now - window:
            _bucket.popleft()
        if len(_bucket) >= max_n:
            retry = int(window - (now - _bucket[0])) + 1
            logger.warning(
                "Cold-path provisioning throttled: %s/%s in last %ss, retry in %ss",
                len(_bucket), max_n, window, retry,
            )
            raise ColdPathThrottled(retry_after_seconds=max(retry, 1))
        _bucket.append(now)


def reset_for_tests() -> None:
    with _lock:
        _bucket.clear()
