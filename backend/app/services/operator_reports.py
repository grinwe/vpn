"""Phase 1 operator-aware routing — resolve OperatorNodeReport outcomes.

After a user taps «VPN не работает» the backend migrates them and stores
an ``OperatorNodeReport(outcome="pending")`` with the new device's
``access_username`` on the target node. This module's tick (run every
~5 min) resolves reports older than ``T_RECONNECT`` by checking whether
that username showed up in a ``NodeTrafficSample`` for the target node
after the report — i.e. did the client actually reconnect there.

See docs/operations/operator_routing_roadmap.md.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow

logger = logging.getLogger(__name__)


def report_reconnected(db: Session, report: models.OperatorNodeReport) -> bool:
    """Did the user reconnect on the target node since the report?

    True iff ``target_access_username`` shows up in any
    ``NodeTrafficSample.details["users"]`` for the target node with
    ``observed_at >= reported_at``. Used both by the watcher (to set the
    matrix outcome) and on-demand by the bot's conditional «всё ещё не
    работает» push (only nudge if we did NOT observe a reconnect).
    """
    if not (report.target_node_id and report.target_access_username):
        return False
    samples = (
        db.query(models.NodeTrafficSample)
        .filter(
            models.NodeTrafficSample.node_id == report.target_node_id,
            models.NodeTrafficSample.observed_at >= report.reported_at,
        )
        .all()
    )
    for sample in samples:
        users = (sample.details or {}).get("users") or []
        if report.target_access_username in users:
            return True
    return False


def resolve_pending_reports(db: Session) -> dict:
    """Mark pending reports ``ok``/``inconclusive`` by observed reconnect.

    A report flips to ``ok`` if the client reconnected on the new node
    (see :func:`report_reconnected`), else ``inconclusive`` — a weaker
    signal than the explicit ``fail`` set by «всё равно не работает».
    """
    window_min = int(os.getenv("OPERATOR_RECONNECT_WINDOW_MIN", "10"))
    now = utcnow()
    cutoff = now - timedelta(minutes=window_min)

    pending = (
        db.query(models.OperatorNodeReport)
        .filter(models.OperatorNodeReport.outcome == "pending")
        .filter(models.OperatorNodeReport.reported_at <= cutoff)
        .all()
    )

    ok = 0
    inconclusive = 0
    for report in pending:
        reconnected = report_reconnected(db, report)
        report.outcome = "ok" if reconnected else "inconclusive"
        report.resolved_at = now
        if reconnected:
            ok += 1
        else:
            inconclusive += 1

    if pending:
        db.commit()
        logger.info(
            "operator_reports: resolved %s pending (ok=%s, inconclusive=%s)",
            len(pending), ok, inconclusive,
        )
    return {
        "checked": len(pending),
        "resolved_ok": ok,
        "resolved_inconclusive": inconclusive,
    }
