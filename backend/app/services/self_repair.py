"""Ядро самопочинки устройства — без привязки к каналу входа.

Логика «человек нажал: VPN не работает» жила в
``api/client_control.py::report_broken_device_by_telegram`` и зависела от
Telegram ровно одной строкой (resolve юзера по telegram_id) — всё остальное
было функцией от устройства. Эпик «починка без Telegram» добавляет второй
и третий вход (WebApp и страница на саб-домене), поэтому логика переезжает
сюда, а хендлеры остаются тонкими адаптерами.

Что это даёт кроме переиспользования:

* **одинаковый исход у всех каналов.** WebApp-путь раньше звал
  ``failover_device`` напрямую, мимо лестницы ротации, и его жалобы не
  считались счётчиком эскалации (писал ``health_ping_response`` вместо
  ``complaint_received``) — то есть человек, жалующийся из кабинета,
  застревал на самом дорогом шаге и никогда не доходил до дубля;
* **FOR UPDATE в одном месте.** Защита от двойного тапа была только у
  бот-пути.

Термины: «шаг лестницы» — ``services/rotation.py`` (перетасовать протоколы →
сменить ноды → выдать дубль), см. ``docs/operations/subset_epic_2026_07_29.md``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow
from . import rotation

logger = logging.getLogger("app.services.self_repair")

# Операторы связи, которые бот предлагает выбрать после починки. Значение
# вне списка приравнивается к «не указан»: поле идёт в крауд-матрицу
# node×operator, и мусор в нём портит статистику по всем нодам.
OPERATORS = {
    "mts",
    "beeline",
    "megafon",
    "tele2",
    "home_wifi",
    "other",
    "unknown",
}

# Источники авто-банов ноды для юзера (ручные админ-баны сюда не попадают).
AUTO_BAN_SOURCES = ("client_control", "admin_panel")

RepairAction = Literal[
    "migrated",
    "reshuffled",
    "duplicated",
    "throttled",
    "no_target",
    "no_subscription",
    "daily_limit",
]


@dataclass(frozen=True)
class RepairOutcome:
    """Исход попытки починки. Литералы ``action`` совпадают с
    ``ReportBrokenResponse.action`` — бот-схему менять не пришлось."""

    action: RepairAction
    report_id: int | None = None
    device_id: int | None = None
    device_name: str | None = None
    new_node_name: str | None = None
    new_node_region: str | None = None
    task_id: int | None = None
    # Жалоба схлопнулась как дребезг (двойной тап в окне дедупа). Не влияет
    # на действие — только на телеметрию канала.
    deduped: bool = False

    @property
    def repaired(self) -> bool:
        """Человеку что-то починили (любым шагом лестницы).

        WebApp-фронт (``Help.tsx``) читает булев ``migrated`` — без этого
        свойства успешная перетасовка протоколов показалась бы ему как «не
        смогли починить».
        """
        return self.action in ("migrated", "reshuffled", "duplicated")


def _audit_fn():
    """Ленивый импорт ``_audit``: на уровне модуля он утащил бы за собой
    пакет ``app.api`` (его ``__init__`` импортирует роутеры, включая
    client_control, который импортирует этот модуль) — циклический импорт."""
    from ..api._common import _audit

    return _audit


def auto_ban_query(db: Session, user_id: int):
    """Query авто-банов юзера (ручные админ-баны сюда не попадают)."""
    from sqlalchemy import or_

    return (
        db.query(models.NodeUserBan)
        .filter(models.NodeUserBan.user_id == user_id)
        .filter(
            or_(
                models.NodeUserBan.created_by.in_(AUTO_BAN_SOURCES),
                models.NodeUserBan.created_by.like("user:%"),
            )
        )
    )


def prune_stale_auto_bans(db: Session, user_id: int) -> int:
    """Снять протухшие авто-баны юзера (TTL env NODE_USER_BAN_TTL_HOURS).

    Best-effort и идемпотентно; 0/отрицательный TTL = отключено. Возвращает
    число снятых банов.
    """
    ttl_h = int(os.getenv("NODE_USER_BAN_TTL_HOURS", "48"))
    if ttl_h <= 0:
        return 0
    cutoff = utcnow() - timedelta(hours=ttl_h)
    stale = (
        auto_ban_query(db, user_id)
        .filter(models.NodeUserBan.created_at < cutoff)
        .all()
    )
    for ban in stale:
        db.delete(ban)
    if stale:
        db.commit()
    return len(stale)


def record_complaint(
    db: Session,
    user,
    *,
    dedup_sec: int,
    proto: str | None = None,
    throttled: bool = True,
    commit: bool = True,
) -> bool:
    """Зафиксировать жалобу «VPN не работает». True — записали, False — дребезг.

    Пишем в audit-лог, а не в operator_node_reports: та таблица означает
    «сделали перенос и следим за исходом», а здесь переноса могло не быть —
    человека остановил троттл. Смешивать их нельзя, иначе watcher начнёт
    ждать восстановления от жалобы, по которой ничего не делали.

    ``dedup_sec`` приходит параметром, а не читается из окружения: окно
    задаёт вызывающий модуль своей константой (её монкипатчат тесты).
    """
    cutoff = utcnow() - timedelta(seconds=dedup_sec)
    recent = (
        db.query(models.AuditLog.id)
        .filter(
            models.AuditLog.action == "complaint_received",
            models.AuditLog.target_type == "user",
            models.AuditLog.target_id == user.id,
            models.AuditLog.created_at >= cutoff,
        )
        .first()
    )
    if recent is not None:
        return False
    db.add(
        models.AuditLog(
            actor=str(user.telegram_id or user.id),
            actor_type=models.AuditActor.user,
            action="complaint_received",
            target_type="user",
            target_id=user.id,
            extra={"throttled": throttled, "proto": proto},
        )
    )
    if commit:
        db.commit()
    else:
        # flush, а не commit: вызывающий держит устройство под FOR UPDATE, и
        # коммит здесь снял бы блокировку ровно посреди защиты от двойного
        # тапа. Свои же SELECT'ы (счётчик лестницы) flush'нутую строку видят.
        db.flush()
    return True


def rotation_report(
    db: Session,
    user,
    sub,
    device,
    *,
    target_node_id: int | None,
    operator: str | None,
) -> models.OperatorNodeReport:
    """Репорт для шага лестницы, который НЕ обвиняет ноду.

    ``failed_node_id`` пустой намеренно: перетасовка протоколов и дубль
    отвечают на «режут транспорт», а не «нода мертва». Записать сюда ноду
    значило бы влить фальшивый fail в крауд-матрицу node×operator и своими
    руками выжигать здоровые ноды. Исход при этом меряется как обычно:
    watcher засчитывает reconnect по любому активному креду устройства.
    """
    report = models.OperatorNodeReport(
        user_id=user.id,
        subscription_id=sub.id if sub else None,
        device_id=device.id,
        operator=operator if operator in OPERATORS else None,
        failed_node_id=None,
        target_node_id=target_node_id,
        target_access_username=device.access_username,
        outcome="pending",
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def _finish_reshuffle(db, user, sub, device, plan, operator, source) -> RepairOutcome:
    """Шаг 1 состоялся: у человека тот же набор нод и другие протоколы."""
    primary = plan.assigned.get("primary") or next(iter(plan.assigned.values()), None)
    report = rotation_report(
        db, user, sub, device,
        target_node_id=primary.node_id if primary else None,
        operator=operator,
    )
    _audit_fn()(
        db,
        f"user:{user.telegram_id or user.id}",
        "client_reported_failure",
        "subscription",
        sub.id if sub else None,
        metadata={
            "report_id": report.id,
            "device_id": device.id,
            "scope": "protocol_reshuffle",
            "source": source,
            "legs": {role: cred.node_id for role, cred in plan.assigned.items()},
        },
        actor_type=models.AuditActor.user,
    )
    return RepairOutcome(
        action="reshuffled",
        report_id=report.id,
        device_id=device.id,
        device_name=device.name or "Устройство",
    )


def _finish_duplicate(db, user, sub, device, dup, operator, source) -> RepairOutcome:
    """Шаг 3: второй лег по работающему протоколу — страховка от падения ноды."""
    report = rotation_report(
        db, user, sub, device, target_node_id=dup.node_id, operator=operator
    )
    _audit_fn()(
        db,
        f"user:{user.telegram_id or user.id}",
        "client_reported_failure",
        "subscription",
        sub.id if sub else None,
        metadata={
            "report_id": report.id,
            "device_id": device.id,
            "scope": "duplicate_leg",
            "source": source,
            "proto": dup.proto,
            "node_id": dup.node_id,
        },
        actor_type=models.AuditActor.user,
    )
    return RepairOutcome(
        action="duplicated",
        report_id=report.id,
        device_id=device.id,
        device_name=device.name or "Устройство",
    )


def _recent_repairs(db: Session, device_id: int, *, since) -> int:
    """Сколько успешных починок этого устройства было с момента ``since``.

    Считаем по ``OperatorNodeReport`` (btree-индексы по device_id и
    reported_at), а не JSONB-поиском по audit_logs: путь user-facing, а
    audit_logs многомиллионная.
    """
    return (
        db.query(models.OperatorNodeReport)
        .filter(models.OperatorNodeReport.device_id == device_id)
        .filter(models.OperatorNodeReport.reported_at >= since)
        .count()
    )


def handle_broken_device(
    db: Session,
    device_id: int,
    *,
    user,
    dedup_sec: int,
    operator: str | None = None,
    source: str = "bot_vpn_broken",
    throttle_sec: int | None = None,
    daily_max: int | None = None,
) -> RepairOutcome:
    """Один шаг починки устройства: жалоба → лестница → перенос.

    ``user`` резолвит вызывающий (у каждого канала свой способ:
    telegram_id, WebApp-initData, sub_token) — это единственное, что здесь
    отличается от канала к каналу.

    ``throttle_sec`` — окно, в котором повторный запрос вернёт ``throttled``
    вместо новой починки. ``None`` = поведение бота: троттла нет, повторный
    тап отсекается тем, что устройство уже ``revoked``. Публичные каналы
    (страница по токену) обязаны его задавать.

    ``daily_max`` — потолок починок с ЭТОГО устройства за сутки. Нужен
    публичным входам: утёкший sub_token иначе вычерпывает пул нод.
    """
    # FOR UPDATE: два тапа подряд (человек нетерпелив) шли в две параллельные
    # транзакции, обе видели «переноса ещё не было» и обе его запускали —
    # устройство успевало переехать дважды, второй раз впустую.
    device = (
        db.query(models.Device)
        .filter(models.Device.id == device_id)
        .with_for_update()
        .first()
    )
    if device is None or device.user_id != user.id:
        # Anti-forge: чужое/несуществующее устройство.
        return RepairOutcome(action="no_subscription")
    if device.status in (
        models.DeviceStatus.disabled,
        models.DeviceStatus.revoked,
    ):
        # Уже перенесли/отключили (напр. повторный тап по старой клавиатуре).
        return RepairOutcome(action="no_subscription")

    sub = device.subscription
    if sub is None or sub.plan is None:
        return RepairOutcome(action="no_subscription")

    now = utcnow()
    if daily_max is not None and daily_max > 0:
        if _recent_repairs(db, device.id, since=now - timedelta(hours=24)) >= daily_max:
            # Жалобу всё равно фиксируем: счётчик лестницы и телеметрия
            # канала не должны слепнуть от того, что человек упёрся в лимит.
            record_complaint(db, user, dedup_sec=dedup_sec, throttled=True, commit=False)
            db.commit()
            return RepairOutcome(action="daily_limit", device_id=device.id)

    if throttle_sec is not None and throttle_sec > 0:
        if _recent_repairs(db, device.id, since=now - timedelta(seconds=throttle_sec)):
            deduped = not record_complaint(
                db, user, dedup_sec=dedup_sec, throttled=True, commit=False
            )
            db.commit()
            return RepairOutcome(
                action="throttled", device_id=device.id, deduped=deduped
            )

    # Жалоба фиксируется ВСЕГДА и до выбора шага: на её счётчике стоит
    # лестница. commit=False — устройство держим под FOR UPDATE до конца.
    deduped = not record_complaint(
        db, user, dedup_sec=dedup_sec, throttled=False, commit=False
    )

    # Ротационная лестница. Первый шаг — перетасовать протоколы на ТЕХ ЖЕ
    # нодах: чаще всего за жалобой стоит блокировка транспорта, а не смерть
    # ноды, и менять ноду в этом случае значит стрелять мимо. Стоит это
    # переставленного флага в БД — ни ansible, ни новых кредов.
    step = rotation.decide_step(db, user.id)
    if step == rotation.STEP_RESHUFFLE:
        plan = rotation.reshuffle_legs(db, device)
        if plan is not None:
            outcome = _finish_reshuffle(db, user, sub, device, plan, operator, source)
            return RepairOutcome(**{**outcome.__dict__, "deduped": deduped})
        # Перетасовывать нечего (запас исчерпан) — идём переносить ноду.
    elif step == rotation.STEP_DUPLICATE:
        dup = rotation.grant_duplicate_leg(db, device)
        if dup is not None:
            outcome = _finish_duplicate(db, user, sub, device, dup, operator, source)
            return RepairOutcome(**{**outcome.__dict__, "deduped": deduped})
        # Дубль взять неоткуда — тоже падаем в перенос.

    # failover_device сам НЕ банит, но исключает уже забаненные юзером ноды —
    # снимаем протухшие авто-баны, чтобы пул для выбора не сужался навсегда.
    prune_stale_auto_bans(db, user.id)

    from .provisioning import ProvisioningOrchestrator

    orchestrator = ProvisioningOrchestrator(db)
    try:
        target, new_device, task, old_primary = orchestrator.failover_device(device)
    except RuntimeError:
        # Нет свежей ноды (всё исключено/нездорово) → канал предложит поддержку.
        return RepairOutcome(action="no_target", device_id=device.id, deduped=deduped)
    except Exception:  # noqa: BLE001
        if db.is_active:
            db.rollback()
        logger.exception("self-repair: failover failed for device %s", device.id)
        return RepairOutcome(action="no_target", device_id=device.id, deduped=deduped)

    report = models.OperatorNodeReport(
        user_id=user.id,
        subscription_id=sub.id,
        device_id=new_device.id,
        operator=operator if operator in OPERATORS else None,
        failed_node_id=old_primary,
        target_node_id=target.id,
        target_access_username=new_device.access_username,
        outcome="pending",
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    _audit_fn()(
        db,
        f"user:{user.telegram_id or user.id}",
        "client_reported_failure",
        "subscription",
        sub.id,
        metadata={
            "report_id": report.id,
            "failed_node_id": old_primary,
            # current_node_id — ключ, по которому крауд-счётчик
            # (_escalate_node_failure_reports) собирает окно репортов.
            "current_node_id": old_primary,
            "target_node_id": target.id,
            "device_id": new_device.id,
            "scope": "device",
            "source": source,
        },
        actor_type=models.AuditActor.user,
    )
    # Краудсорс здоровья ноды — как в whole-sub пути. Best-effort.
    if old_primary:
        try:
            from ..api.client_control import _escalate_node_failure_reports

            _escalate_node_failure_reports(db, old_primary)
        except Exception:  # noqa: BLE001
            logger.exception(
                "self-repair: crowd-health escalation failed for node %s", old_primary
            )
    return RepairOutcome(
        action="migrated",
        report_id=report.id,
        device_id=new_device.id,
        new_node_name=target.name,
        new_node_region=target.region,
        task_id=task.id if task else None,
        device_name=new_device.name or "Устройство",
        deduped=deduped,
    )
