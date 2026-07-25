"""Воронка онбординга: бот → кабинет → триал → устройство → оплата.

Один источник правды для админки (`GET /api/admin/onboarding-funnel`) и CLI
(`scripts/onboarding_funnel.py`) — иначе две реализации разойдутся ровно тогда,
когда по ним начнут принимать решения.

Телеметрия появилась 2026-07-25 (роадмап E0), поэтому у когорт СТАРШЕ этой даты
`opened_cabinet` всегда 0: события `webapp_open` тогда просто не писались. Это
отражено флагом ``telemetry_since`` в ответе, чтобы админка не выдавала «никто не
открывал кабинет» за факт.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow

# Дата, с которой пишутся webapp_open / trial_activate_*.
TELEMETRY_SINCE = datetime(2026, 7, 25)


def compute(db: Session, days: int | None = 7) -> dict:
    """Посчитать воронку по когорте пользователей, пришедших за ``days`` дней.

    ``days=None``/0 — за всё время.
    """
    q = db.query(models.User)
    if days:
        q = q.filter(models.User.created_at >= utcnow() - timedelta(days=days))
    users = q.all()
    total = len(users)
    if not total:
        return {
            "days": days, "total": 0, "steps": [], "losses": [],
            "trial_failures": 0, "telemetry_partial": False,
        }

    ids = [u.id for u in users]

    def _targets(action: str) -> set[int]:
        return {
            t for (t,) in db.query(models.AuditLog.target_id)
            .filter(models.AuditLog.action == action,
                    models.AuditLog.target_id.in_(ids))
            .distinct().all()
        }

    opened = _targets("webapp_open")
    trial_failed = _targets("trial_activate_rejected")
    claimed = {u.id for u in users if u.trial_activated_at is not None}
    with_device = {
        uid for (uid,) in db.query(models.Device.user_id)
        .filter(models.Device.user_id.in_(ids)).distinct().all()
    }
    paid = {
        uid for (uid,) in db.query(models.Invoice.user_id)
        .filter(models.Invoice.user_id.in_(ids),
                models.Invoice.status == models.InvoiceStatus.paid)
        .distinct().all()
    }

    # Когорта частично старше телеметрии → «открыли кабинет» занижено.
    oldest = min(u.created_at for u in users if u.created_at)
    telemetry_partial = bool(oldest and oldest < TELEMETRY_SINCE)

    steps = [
        {"key": "started", "label": "Пришли в бота", "count": total},
        {"key": "opened", "label": "Открыли кабинет", "count": len(opened)},
        {"key": "trial", "label": "Забрали триал", "count": len(claimed)},
        {"key": "device", "label": "Получили ссылку", "count": len(with_device)},
        {"key": "paid", "label": "Оплатили", "count": len(paid)},
    ]
    losses = [
        {"key": "never_opened", "label": "Не открыли кабинет",
         "count": total - len(opened)},
        {"key": "opened_no_trial", "label": "Открыли, но без триала",
         "count": len([u for u in users if u.id in opened and u.id not in claimed])},
        {"key": "trial_no_device", "label": "Забрали триал, но без ссылки",
         "count": len([u for u in users if u.id in claimed and u.id not in with_device])},
    ]
    for row in steps + losses:
        row["pct"] = round(100 * row["count"] / total, 1)

    return {
        "days": days,
        "total": total,
        "steps": steps,
        "losses": losses,
        "trial_failures": len(trial_failed),
        "telemetry_partial": telemetry_partial,
    }
