"""Алерт админу, когда автоматическая починка «VPN не работает» НЕ помогла.

Раньше пуш уходил в момент жалобы на плановый пинг («Юзер жалуется: VPN не
работает»), ДО починки и без её исхода: админ видел тревогу, хотя лестница
self_repair (перетасовка протоколов → перенос ноды → дубль-нога) обычно уже
всё исправила (user 1000078, 30.09.2026: перетасовка, переподключение через
14 минут). Первый пуш убран — жалоба и шаги остаются в БД
(``health_ping_response``, ``complaint_received``, ``client_reported_failure``,
``OperatorNodeReport``), а админ получает ОДИН пуш, только если:

* watcher не увидел переподключения за окно (``inconclusive``);
* юзер сам нажал «всё ещё не работает» (``fail``) — бот, кабинет, страница;
* починка вообще ничего не сделала: целевой ноды нет (``no_target``).

Покрывает все каналы (пинг, кнопка бота, кабинет, страница): хуки стоят на
исходе ``OperatorNodeReport`` и на ``no_target`` в ядре, а не на входе канала.
Best effort: сбой алерта не должен ломать починку и watcher.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow
from .admin_notify import notify_admins

logger = logging.getLogger(__name__)

KIND = "repair_failed"
# Один пуш на репорт: inconclusive от watcher-а и последующий «всё ещё не
# работает» по тому же репорту — одна и та же история.
_REPORT_WINDOW_SEC = 7 * 24 * 3600
# no_target: не чаще раза в час на юзера (повторные жалобы подряд).
_NO_TARGET_WINDOW_SEC = 3600

_STEP_TEXT = {
    "protocol_reshuffle": "перетасовали протоколы на тех же нодах",
    "duplicate_leg": "добавили запасную ногу",
    "device": "перенесли устройство на другую ноду",
}
_REASON_TEXT = {
    "inconclusive": "переподключения не видно",
    "fail": "юзер нажал «всё ещё не работает»",
}


def _naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _step_of(db: Session, report_id: int) -> tuple[str, str | None]:
    """Что сделала починка по этому репорту — из client_reported_failure."""
    row = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.action == "client_reported_failure",
            models.AuditLog.extra.contains({"report_id": report_id}),
        )
        .order_by(models.AuditLog.id.desc())
        .first()
    )
    extra = (row.extra or {}) if row is not None else {}
    scope = extra.get("scope")
    if scope in _STEP_TEXT:
        step = _STEP_TEXT[scope]
    elif extra.get("kind") == "user_reported" or scope == "subscription":
        step = "перенесли подписку на другую ноду"
    else:
        step = "починка"
    return step, extra.get("source")


def _who(db: Session, user_id: int | None) -> str:
    user = db.get(models.User, user_id) if user_id else None
    tg = user.telegram_id if user is not None else None
    return f"tg={tg} (id={user_id})" if tg else f"id={user_id}"


def alert_repair_not_fixed(db: Session, report: models.OperatorNodeReport, *, reason: str) -> None:
    """Пуш «починка не помогла» по репорту. Не коммитит (коммитит вызывающий)."""
    try:
        step, source = _step_of(db, report.id)
        node = db.get(models.VPNNode, report.target_node_id) if report.target_node_id else None
        reported = _naive(report.reported_at)
        minutes = int((utcnow() - reported).total_seconds() // 60) if reported else None
        text = (
            f"🚨 Починка не помогла: {_who(db, report.user_id)}\n"
            f"Подписка #{report.subscription_id or '?'}, устройство #{report.device_id or '?'}\n"
            f"Сделали: {step}"
            + (f", нода {node.name}" if node is not None else "")
            + f"; оператор: {report.operator or 'не указан'}\n"
            f"Исход: {_REASON_TEXT.get(reason, reason)}"
            + (f" (через {minutes} мин после жалобы)" if minutes is not None else "")
            + (f"; канал: {source}" if source else "")
        )
        notify_admins(
            db,
            kind=KIND,
            text=text,
            dedup_key={"report_id": report.id},
            extra={"reason": reason, "user_id": report.user_id,
                   "subscription_id": report.subscription_id},
            window_sec=_REPORT_WINDOW_SEC,
        )
    except Exception:  # noqa: BLE001
        logger.exception("repair_alerts: not-fixed alert failed for report %s", report.id)


def alert_repair_no_target(
    db: Session,
    user: models.User,
    sub: models.Subscription | None,
    *,
    source: str | None,
    device_id: int | None = None,
) -> None:
    """Пуш «починить не смогли — целевой ноды нет». Коммитит сам: зовётся после
    отката неудачного переноса, внешней транзакции уже нет."""
    try:
        text = (
            f"🚨 Починить не смогли: {_who(db, user.id)}\n"
            f"Подписка #{sub.id if sub is not None else '?'}"
            + (f", устройство #{device_id}" if device_id else "")
            + "\nЦелевой ноды для переноса нет (пул пуст или все исключены)"
            + (f"; канал: {source}" if source else "")
        )
        notify_admins(
            db,
            kind=KIND,
            text=text,
            dedup_key={"user_id": user.id, "reason": "no_target"},
            extra={"subscription_id": sub.id if sub is not None else None,
                   "device_id": device_id, "source": source},
            window_sec=_NO_TARGET_WINDOW_SEC,
            autocommit=True,
        )
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("repair_alerts: no-target alert failed for user %s", user.id)
