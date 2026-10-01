"""Воронка онбординга: бот → кабинет → триал → устройство → оплата.

Один источник правды для админки (`GET /api/admin/onboarding-funnel`) и CLI
(`scripts/onboarding_funnel.py`) — иначе две реализации разойдутся ровно тогда,
когда по ним начнут принимать решения.

⚠️ Шаги считаются из РАЗНЫХ источников, и это определяет всю арифметику:

* «забрали триал», «получили ссылку», «оплатили» — из состояния БД
  (`User.trial_activated_at`, `Device`, `Invoice`), т.е. известны за всю
  историю;
* «открыли кабинет» — из событий аудита `webapp_open`, которые пишутся только
  с ``TELEMETRY_SINCE``.

Поэтому шаг про кабинет считается по СВОЕЙ под-когорте — юзерам, пришедшим
после включения телеметрии, — и имеет собственный знаменатель. Смешивать их в
одну шкалу нельзя: первая версия делила единицу на всех 73 юзеров и рисовала
«98.6% не открыли кабинет», хотя про 72 из них данных просто не существует.
Отсутствие данных, поданное как потеря, — худший вид вранья в аналитике: по
нему принимают решения.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow

# Дата, с которой пишутся webapp_open / trial_activate_*.
TELEMETRY_SINCE = datetime(2026, 7, 25)

# Причины trial_activate_rejected, которые считаются провалом активации: юзер
# хотел бесплатные дни и не получил их по нашей вине. Значения общие для бота
# и кабинета.
TRIAL_FAILURE_REASONS = frozenset(
    {"no_trial_plan", "throttled", "provision_failed", "insufficient"}
)


def _step(key: str, label: str, count: int, denominator: int, *, measurable: bool = True) -> dict:
    return {
        "key": key,
        "label": label,
        "count": count if measurable else None,
        # Знаменатель у шагов разный (см. докстринг), поэтому отдаём его явно —
        # иначе UI посчитает процент не от того основания.
        "denominator": denominator,
        "pct": round(100 * count / denominator, 1) if (measurable and denominator) else None,
        "measurable": measurable,
    }


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
            "trial_failures": 0, "telemetry_cohort": 0,
        }

    ids = [u.id for u in users]
    # Под-когорта, про которую телеметрия вообще может что-то сказать.
    measurable_users = [
        u for u in users if u.created_at and u.created_at >= TELEMETRY_SINCE
    ]
    measurable_ids = {u.id for u in measurable_users}
    cohort = len(measurable_users)

    def _targets(action: str) -> set[int]:
        return {
            t for (t,) in db.query(models.AuditLog.target_id)
            .filter(models.AuditLog.action == action,
                    models.AuditLog.target_id.in_(ids))
            .distinct().all()
        }

    # Считаем открытия ТОЛЬКО по измеримой под-когорте: событие от старого
    # юзера (зашёл в кабинет уже после включения телеметрии) ничего не говорит
    # о его онбординге и только завышало бы картину.
    opened = _targets("webapp_open") & measurable_ids
    # Провал активации — только настоящие сбои. Отказы live / already_used /
    # already_activated — это платящие и уже взявшие триал, которые жмут
    # старые кнопки: они раздули бы «провалы» на Dashboard.
    trial_failed = {
        t for (t, extra) in db.query(models.AuditLog.target_id, models.AuditLog.extra)
        .filter(models.AuditLog.action == "trial_activate_rejected",
                models.AuditLog.target_id.in_(ids))
        .all()
        if (extra or {}).get("reason") in TRIAL_FAILURE_REASONS
    }
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
    # «Живы сейчас»: трафик за последние 24 часа. Device.last_seen_at штампует
    # тик traffic_stats — в отличие от остальных шагов это не этап онбординга,
    # а срез текущего состояния: сколько из когорты реально пользуются VPN
    # (на когорте «всё время» — грубый ретеншн).
    active_24h = {
        uid for (uid,) in db.query(models.Device.user_id)
        .filter(models.Device.user_id.in_(ids),
                models.Device.last_seen_at >= utcnow() - timedelta(hours=24))
        .distinct().all()
    }

    steps = [
        _step("started", "Пришли в бота", total, total),
        _step("opened", "Открыли кабинет", len(opened), cohort, measurable=cohort > 0),
        _step("trial", "Забрали триал", len(claimed), total),
        # NB: «получили ссылку» может быть БОЛЬШЕ, чем «забрали триал» — сюда
        # входят купившие подписку без триала. Это не сбой воронки: шаги
        # параллельные, а не вложенные.
        _step("device", "Получили ссылку", len(with_device), total),
        _step("paid", "Оплатили", len(paid), total),
        _step("active_24h", "Активны за 24ч", len(active_24h), total),
    ]
    losses = [
        _step("never_opened", "Не открыли кабинет",
              cohort - len(opened), cohort, measurable=cohort > 0),
        _step("opened_no_trial", "Открыли, но без триала",
              len(opened - claimed), cohort, measurable=cohort > 0),
        _step("trial_no_device", "Забрали триал, но без ссылки",
              len(claimed - with_device), total),
        # Знаменатель — забравшие триал: вопрос этой строки — «сколько из
        # попробовавших уже отвалилось», а не доля от всей когорты.
        _step("trial_inactive", "Забрали триал, но не активны 24ч",
              len(claimed - active_24h), len(claimed),
              measurable=len(claimed) > 0),
    ]

    return {
        "days": days,
        "total": total,
        # Сколько юзеров когорты вообще попадают под телеметрию. 0 — значит про
        # кабинет мы не знаем НИЧЕГО, и UI обязан сказать это словами.
        "telemetry_cohort": cohort,
        "steps": steps,
        "losses": losses,
        "trial_failures": len(trial_failed),
    }
