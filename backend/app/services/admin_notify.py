"""Admin push-уведомления через бота.

Поверх существующего транспорта `AuditLog` + `/api/notifications/pending`
+ `notification_poller` в `bot/bot.py`: пишем по одной AuditLog-строке на
каждого telegram_id из env `ADMIN_TELEGRAM_IDS`. Bot-поллер читает их и
шлёт `bot.send_message`. Новой таблицы не требуется.

Дедуп: перед записью серии смотрим, не было ли уже алерта с тем же
`{kind, **dedup_key}` за окно `ADMIN_ALERT_DEDUP_WINDOW_SEC` (JSONB
containment `@>`). Если match — пропускаем. Проверка идёт по ЛЮБОМУ
получателю: достаточно того, что один админ в окне уже получил push —
серия для остальных админов в том же окне не создаётся, иначе 3 админа
× 50 юзеров = 150 пушей на один инцидент.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow

logger = logging.getLogger(__name__)

DEFAULT_DEDUP_WINDOW_SEC = 600


def _admin_ids() -> list[int]:
    """Парсим env `ADMIN_TELEGRAM_IDS` (CSV telegram-id'шников).

    Не кешируем — env в тестах монкипатчится, кеш даст stale-результат.
    Парсинг дешёвый.
    """
    raw = os.getenv("ADMIN_TELEGRAM_IDS", "").strip()
    if not raw:
        return []
    out: list[int] = []
    for item in raw.split(","):
        s = item.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError:
            logger.warning(
                "admin_notify: невалидный telegram_id в ADMIN_TELEGRAM_IDS: %r", s
            )
    return out


def notify_admins(
    db: Session,
    *,
    kind: str,
    text: str,
    dedup_key: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    window_sec: int | None = None,
    autocommit: bool = False,
) -> list[int]:
    """Поставить push-уведомление админам через bot-поллер.

    Args:
        kind: short tag, идёт в action как ``"admin_alert_{kind}"``.
            Попадает и в allowlist в ``/api/notifications/pending``,
            и в dedup-needle.
        text: готовый текст сообщения — бот рендерит as-is, форматирует backend.
        dedup_key: словарь, который в сумме с ``{"kind": kind}`` ищется
            через JSONB ``@>`` в ``AuditLog.extra`` за окно. Пустой ``{}``
            = «не чаще раза в окно, независимо от контекста».
        extra: дополнительная телеметрия (уйдёт в extra помимо dedup_key).
        window_sec: override ``ADMIN_ALERT_DEDUP_WINDOW_SEC`` (default 600).
        autocommit: если True — хелпер сам сделает ``db.commit()``
            (удобно для worker-хуков). Для endpoint-ов держим False:
            endpoint коммитит сам вместе с основным update-ом.

    Returns:
        Список id созданных ``AuditLog``-строк. Пустой, если
        ``ADMIN_TELEGRAM_IDS`` не задан или дедуп сработал.
    """
    admin_ids = _admin_ids()
    if not admin_ids:
        logger.debug(
            "admin_notify: ADMIN_TELEGRAM_IDS пуст, пропускаем %s", kind
        )
        return []

    dedup_key = dict(dedup_key or {})
    action_name = f"admin_alert_{kind}"
    window = (
        window_sec
        if window_sec is not None
        else int(os.getenv("ADMIN_ALERT_DEDUP_WINDOW_SEC", str(DEFAULT_DEDUP_WINDOW_SEC)))
    )

    cutoff = utcnow() - timedelta(seconds=window)
    needle: dict[str, Any] = {"kind": kind, **dedup_key}

    # JSONB containment: `AuditLog.extra @> needle`. SQLAlchemy JSONB-
    # comparator.contains() → "@>". Любое соответствие в окне — серию
    # не пишем.
    existing = (
        db.query(models.AuditLog.id)
        .filter(
            models.AuditLog.action == action_name,
            models.AuditLog.actor_type == models.AuditActor.system,
            models.AuditLog.created_at >= cutoff,
            models.AuditLog.extra.contains(needle),
        )
        .first()
    )
    if existing is not None:
        logger.info(
            "admin_notify: дедуп — %s %s уже есть в окне %ss, пропускаем",
            action_name,
            needle,
            window,
        )
        return []

    rows: list[models.AuditLog] = []
    for tg_id in admin_ids:
        row = models.AuditLog(
            actor="admin_notify",
            actor_type=models.AuditActor.system,
            action=action_name,
            target_type="admin_alert",
            target_id=None,
            extra={
                "telegram_id": str(tg_id),
                "kind": kind,
                "text": text,
                **dedup_key,
                **(extra or {}),
            },
        )
        db.add(row)
        rows.append(row)

    if autocommit:
        db.commit()
    else:
        db.flush()
    return [r.id for r in rows if r.id is not None]
