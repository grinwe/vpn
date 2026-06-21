"""Per-target diagnose incident state + toggles (ASK-3 anti-spam, ASK-4).

Operates duck-typed on any target carrying the ``diagnose_*`` columns added
by migration 0039 — today ``VPNNode`` and ``WGExitNode``. Centralizes the
single "should we diagnose this target right now?" decision so the worker
ticks AND the manual endpoints share one anti-spam gate instead of the three
fragmented 30-min AuditLog debounce zones we had before.

Incident model (operator's choice: one-and-done + push, opt-in escalation):
  * node goes down → ``should_diagnose`` returns True ONCE, opens the
    incident (``diagnose_incident_open_at``), pushes, then stays quiet;
  * the admin decides from the push: ``ack`` ("вижу, работаю" → stop),
    ``mute`` (silence alerts N hours), or ``follow`` (opt into 30m→2h→6h
    re-diagnosis via ``diagnose_follow_mode='exponential'``);
  * node recovers → ``close_incident`` resets state so the NEXT outage is a
    fresh incident.

Two INDEPENDENT toggles (kept orthogonal on purpose):
  * ``diagnostics_disabled_at`` — hard-stop ALL diagnose tasks;
  * ``alerts_muted_until``      — silence admin pushes until a TTL.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from ..time_utils import utcnow

# "Mute forever" sentinel for the "совсем" preset — far enough out that no
# real TTL collides, cheap to compare, and still un-mutable via /enable.
_FOREVER = datetime(2100, 1, 1)


def _safety_recap_hours() -> int:
    return int(os.getenv("DIAGNOSE_SAFETY_RECAP_HOURS", "12"))


def is_diagnostics_disabled(target) -> bool:
    """Hard toggle: no diagnose tasks at all for this target."""
    if getattr(target, "diagnostics_disabled_at", None) is not None:
        return True
    # Backward-compat: nodes muted under the legacy combined flag before
    # migration 0039 ran its backfill still mean "don't diagnose".
    return getattr(target, "auto_diagnose_disabled_at", None) is not None


def is_alerts_muted(target, now: datetime | None = None) -> bool:
    """Soft toggle: suppress admin Telegram alerts until the TTL passes."""
    now = now or utcnow()
    muted_until = getattr(target, "alerts_muted_until", None)
    return muted_until is not None and muted_until > now


def should_diagnose(target, now: datetime | None = None) -> tuple[bool, str]:
    """The single anti-spam gate. Returns (should_run, reason).

    Replaces the old per-tick 30-min AuditLog debounce: a down target is
    diagnosed ONCE per outage, not every tick. Exponential opt-in re-arms on
    the backoff ladder; a rare safety recap re-checks a long-down target.
    """
    now = now or utcnow()

    if is_diagnostics_disabled(target):
        return False, "diagnostics_disabled"

    incident_open = getattr(target, "diagnose_incident_open_at", None)
    if incident_open is None:
        return True, "incident_open"  # first diagnosis of this outage

    follow = getattr(target, "diagnose_follow_mode", None)
    acked = getattr(target, "diagnose_acked_at", None)

    # "вижу, работаю" → stop auto re-diagnosis for this incident, UNLESS the
    # operator explicitly opted into exponential follow (the two are mutually
    # exclusive choices, but follow wins if both somehow set).
    if acked is not None and acked >= incident_open and follow != "exponential":
        return False, "acked"

    if follow == "exponential":
        backoff_until = getattr(target, "diagnose_backoff_until", None)
        if backoff_until is None or backoff_until <= now:
            return True, "exponential_due"
        return False, "exponential_backoff"

    # one-and-done: only a rare safety recap re-diagnoses a long-down target.
    last = getattr(target, "last_diagnosed_at", None)
    if last is not None and last <= now - timedelta(hours=_safety_recap_hours()):
        return True, "safety_recap"
    return False, "once_done"


def _next_backoff(target, now: datetime) -> datetime:
    """Exponential ladder 30m → 2h → 6h, picked by time since the outage."""
    opened = getattr(target, "diagnose_incident_open_at", None) or now
    elapsed_min = (now - opened).total_seconds() / 60
    if elapsed_min < 30:
        step = 30
    elif elapsed_min < 150:
        step = 120
    else:
        step = 360
    return now + timedelta(minutes=step)


def mark_diagnosed(target, now: datetime | None = None) -> None:
    """Record a diagnosis: open the incident if new, stamp + arm backoff."""
    now = now or utcnow()
    if getattr(target, "diagnose_incident_open_at", None) is None:
        target.diagnose_incident_open_at = now
    target.last_diagnosed_at = now
    if getattr(target, "diagnose_follow_mode", None) == "exponential":
        target.diagnose_backoff_until = _next_backoff(target, now)


def close_incident(target) -> bool:
    """Target recovered — reset incident state. Returns True if one was open."""
    if getattr(target, "diagnose_incident_open_at", None) is None:
        return False
    target.diagnose_incident_open_at = None
    target.diagnose_backoff_until = None
    target.diagnose_follow_mode = None
    target.diagnose_acked_at = None
    return True


def reconcile_healthy_incident(
    target, now: datetime | None = None, max_age_min: float = 30.0
) -> bool:
    """Бэкстоп-закрытие: снять инцидент, оставшийся открытым на уже здоровой цели.

    Reachability-тик закрывает инцидент СРАЗУ на свежем ``ssh_ok`` пробе. Но это
    штатное закрытие можно пропустить: рекавери-проб обрезан wall-clock бюджетом
    тика (хвост списка целей не пробился), инцидент открыт крауд-путём на
    SSH-здоровой ноде, или тик подвисал. Реконсилит остаточное состояние
    «инцидент открыт + probe ok» каждый тик — дёшево, без SSH, — чтобы красный
    бейдж не висел, пока нода доказуемо жива. Закрытие на ``ssh_ok`` в тике
    остаётся как было; это лишь подстраховка для рассинхрона.

    Закрывает iff: инцидент открыт, последний проб == ``ok``, нет активной серии
    падений (``unreachable_since`` is None) и этот проб свежий (``last_probe_at``
    не старше ``max_age_min`` — чтобы не действовать по протухшей телеметрии
    подвисшего тика). Возвращает True, если закрыл. ``max_age_min`` <= 0 снимает
    проверку свежести.
    """
    now = now or utcnow()
    if getattr(target, "diagnose_incident_open_at", None) is None:
        return False
    if getattr(target, "last_probe_status", None) != "ok":
        return False
    if getattr(target, "unreachable_since", None) is not None:
        return False
    last_probe_at = getattr(target, "last_probe_at", None)
    if last_probe_at is None:
        return False
    if max_age_min > 0 and last_probe_at < now - timedelta(minutes=max_age_min):
        return False
    return close_incident(target)


# ── Toggle / button mutations (called by api/diagnostics.py + bot) ──────────

def set_diagnostics_disabled(target, disabled: bool, now: datetime | None = None) -> None:
    target.diagnostics_disabled_at = (now or utcnow()) if disabled else None


def mute_alerts(target, hours: int, now: datetime | None = None) -> datetime | None:
    """hours>0 → mute until now+hours; hours<0 → forever; hours==0 → unmute."""
    now = now or utcnow()
    if hours == 0:
        target.alerts_muted_until = None
    elif hours < 0:
        target.alerts_muted_until = _FOREVER
    else:
        target.alerts_muted_until = now + timedelta(hours=hours)
    return target.alerts_muted_until


def ack_incident(target, now: datetime | None = None) -> None:
    """«Вижу, работаю» — stop auto re-diagnosis for the open incident.

    Universal stop: also LEAVES exponential-follow mode (and clears its
    backoff). Otherwise a target the operator put into follow mode would
    keep re-diagnosing + pushing on the ladder despite the ack — ``should_
    diagnose`` lets ``follow=='exponential'`` win over the acked branch, so
    ack must clear it to actually take effect (the bot has no other way out
    of follow mode).
    """
    target.diagnose_acked_at = now or utcnow()
    target.diagnose_follow_mode = None
    target.diagnose_backoff_until = None


def set_follow_mode(target, mode: str | None, now: datetime | None = None) -> None:
    """Opt into 'exponential' re-diagnosis (or back to one-and-done 'once')."""
    now = now or utcnow()
    target.diagnose_follow_mode = mode
    if mode == "exponential":
        # Re-arm so the next tick re-diagnoses on the ladder immediately.
        target.diagnose_backoff_until = _next_backoff(target, now)
    else:
        target.diagnose_backoff_until = None
