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

from sqlalchemy import text as sql_text
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
    # Устройство ещё собирается (pending): чинить его сейчас — это второй
    # холодный провижн поверх незавершённого первого.
    "not_ready",
]

RepairScope = Literal["device", "subscription"]

# Единая политика повторов для ВСЕХ каналов («VPN не работает» в боте,
# кабинете, на странице по токену, ответ «плохо» на плановый пинг).
# Канонические имена — SELF_REPAIR_*; SUB_FIX_* оставлены как fallback: до
# унификации (2026-09-12) окно и потолок были только у страницы, и прод
# настроен через них.
_THROTTLE_DEFAULT = 120
_DAILY_MAX_DEFAULT = 5


def _env_int(names: tuple[str, ...], default: int) -> int:
    for name in names:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return default


def default_throttle_sec() -> int:
    """Окно, в котором повторная жалоба по подписке отвечает ``throttled``."""
    return _env_int(("SELF_REPAIR_THROTTLE_SEC", "SUB_FIX_THROTTLE_SEC"), _THROTTLE_DEFAULT)


def default_daily_max() -> int:
    """Потолок починок одной подписки за сутки (0 = без потолка)."""
    return _env_int(("SELF_REPAIR_DAILY_MAX", "SUB_FIX_DAILY_MAX"), _DAILY_MAX_DEFAULT)


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
    # Сколько секунд ждать до следующей попытки (throttled / daily_limit).
    retry_after_sec: int | None = None
    # Что чинили: одно устройство или всю подписку («все мои устройства»).
    scope: RepairScope = "device"
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


def _recent_repairs(db: Session, sub_id: int, *, since) -> int:
    """Сколько починок этой ПОДПИСКИ было с момента ``since``.

    Считаем по подписке, а НЕ по устройству: успешный перенос ноды
    пересоздаёт Device (старый становится revoked, появляется новый с новым
    id), поэтому счётчик по device_id обнулялся бы после каждой миграции — и
    суточный потолок, который защищает пул нод от утёкшего токена, не
    работал бы ровно там, где он нужен. Подписка живёт через все миграции.

    Источник — ``OperatorNodeReport`` (btree-индексы по subscription_id и
    reported_at), а не JSONB-поиск по audit_logs: путь user-facing, а
    audit_logs многомиллионная.
    """
    return (
        db.query(models.OperatorNodeReport)
        .filter(models.OperatorNodeReport.subscription_id == sub_id)
        .filter(models.OperatorNodeReport.reported_at >= since)
        .count()
    )


def _repair_times(db: Session, sub_id: int, *, since) -> list:
    """Моменты починок подписки с ``since`` — новые первыми."""
    rows = (
        db.query(models.OperatorNodeReport.reported_at)
        .filter(models.OperatorNodeReport.subscription_id == sub_id)
        .filter(models.OperatorNodeReport.reported_at >= since)
        .order_by(models.OperatorNodeReport.reported_at.desc())
        .all()
    )
    return [r[0] for r in rows if r[0] is not None]


def _resolve_policy(throttle_sec: int | None, daily_max: int | None) -> tuple[int, int]:
    """``None`` = единый дефолт из окружения; явный ``0`` = выключено."""
    return (
        default_throttle_sec() if throttle_sec is None else throttle_sec,
        default_daily_max() if daily_max is None else daily_max,
    )


def repair_wait(
    db: Session,
    sub,
    *,
    now=None,
    throttle_sec: int | None = None,
    daily_max: int | None = None,
) -> tuple[int | None, Literal["throttled", "daily_limit"] | None]:
    """Сколько ждать до следующей починки подписки и почему.

    ``(None, None)`` — можно чинить прямо сейчас. Чистая проверка: ничего не
    пишет. Ею пользуются ПРЕ-чеки пикеров (бот, кабинет), чтобы не спрашивать
    «какое устройство?», когда действие всё равно будет отклонено — и ровно
    той же арифметикой пользуется гейт внутри ``handle_broken_*``, поэтому
    пре-чек и действие никогда не расходятся.
    """
    now = now or utcnow()
    throttle_sec, daily_max = _resolve_policy(throttle_sec, daily_max)
    if daily_max > 0:
        # Потолок задан «на устройство», а считается по ПОДПИСКЕ (перенос
        # пересоздаёт Device, счётчик по device_id обнулялся бы). Поэтому
        # масштабируем на число живых устройств: человеку с тремя
        # устройствами по два шага на каждое иначе не хватало бы пяти.
        daily_cap = daily_max * max(1, len(live_devices(sub)))
        times = _repair_times(db, sub.id, since=now - timedelta(hours=24))
        if len(times) >= daily_cap:
            # Окно освободится, когда самая старая из «лишних» починок
            # выйдет за сутки.
            oldest = times[daily_cap - 1]
            left = (oldest + timedelta(hours=24) - now).total_seconds()
            return max(1, int(left) + 1), "daily_limit"
    if throttle_sec > 0:
        times = _repair_times(db, sub.id, since=now - timedelta(seconds=throttle_sec))
        if times:
            left = throttle_sec - (now - times[0]).total_seconds()
            return max(1, int(left) + 1), "throttled"
    return None, None


def _policy_gate(
    db: Session,
    user,
    sub,
    *,
    now,
    dedup_sec: int,
    throttle_sec: int | None,
    daily_max: int | None,
    device_id: int | None = None,
    scope: RepairScope = "device",
) -> RepairOutcome | None:
    """Троттл и суточный потолок — одни на все каналы.

    Жалобу фиксируем и здесь: счётчик лестницы и телеметрия канала не должны
    слепнуть от того, что человек упёрся в лимит («не помогло, жму ещё раз» —
    самый частый сценарий и главный сигнал для эскалации).
    """
    wait, reason = repair_wait(
        db, sub, now=now, throttle_sec=throttle_sec, daily_max=daily_max
    )
    if reason is None:
        return None
    deduped = not record_complaint(
        db, user, dedup_sec=dedup_sec, throttled=True, commit=False
    )
    db.commit()
    return RepairOutcome(
        action=reason,
        device_id=device_id,
        retry_after_sec=wait,
        scope=scope,
        deduped=deduped,
    )


def user_may_repair(user) -> bool:
    """Глобально забаненный (``User.banned_at``) не чинится ни из одного
    канала: бот дропает его апдейты мидлварой, а кабинет и страница по
    токену иначе давали бы ему жечь слоты и ansible."""
    return user is not None and getattr(user, "banned_at", None) is None


def _sub_is_repairable(sub, *, now) -> bool:
    """Право на починку проверяет ЯДРО, а не экран.

    Экран истёкшей подписки кнопку починки не рисует — но это защита в слое
    отображения, а POST приходит по URL. Причём бесплатно: POST без nonce
    возвращает страницу, а на ней уже валидный nonce. Без этой проверки
    человек с истёкшей подпиской в grace-периоде (worker метит expired
    сразу, а устройства ревокает лишь через RENEWAL_GRACE_HOURS) мог бы
    раз за разом жечь слот ноды и полный прогон ansible, а вдобавок каждая
    такая «починка» вливала бы фальшивый fail-голос против ЗДОРОВОЙ ноды
    в крауд-матрицу.
    """
    if sub is None or sub.plan is None:
        return False
    if sub.status != models.SubscriptionStatus.active:
        return False
    return not (sub.expires_at is not None and sub.expires_at < now)


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
    вместо новой починки; ``daily_max`` — потолок починок ПОДПИСКИ за сутки
    (утёкший sub_token иначе вычерпывает пул нод). ``None`` = единый дефолт
    из окружения (``default_throttle_sec`` / ``default_daily_max``) — он
    один для всех каналов; явный ``0`` выключает проверку (тесты).
    """
    if not user_may_repair(user):
        return RepairOutcome(action="no_subscription")
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
    if device.status == models.DeviceStatus.pending:
        # Ещё собирается: перетасовывать нечего (кредов нет), а перенос
        # запустил бы второй холодный провижн поверх первого.
        return RepairOutcome(action="not_ready", device_id=device.id)

    sub = device.subscription
    now = utcnow()
    if not _sub_is_repairable(sub, now=now):
        return RepairOutcome(action="no_subscription", device_id=device.id)

    gated = _policy_gate(
        db, user, sub,
        now=now, dedup_sec=dedup_sec,
        throttle_sec=throttle_sec, daily_max=daily_max,
        device_id=device.id,
    )
    if gated is not None:
        return gated

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
    except (RuntimeError, Exception) as exc:  # noqa: BLE001
        # ОТКАТ ОБЯЗАТЕЛЕН И ПЕРВЫМ ДЕЛОМ. failover_device мог успеть отозвать
        # старое устройство и снять с него sub_token — коммит такого
        # полусостояния убил бы seamless-alias инвариант (сохранённые URL
        # клиентов начинают 404-ить). Условие `if db.is_active` здесь стояло
        # бы во вред: у сессии в состоянии partial-rollback этот флаг False,
        # то есть откат пропускался бы ровно тогда, когда он необходим.
        db.rollback()
        if not isinstance(exc, RuntimeError):
            logger.exception("self-repair: failover failed for device %s", device.id)
        # Откат снёс и жалобу (она была во flush'е) — пишем заново своей
        # транзакцией. Без этого человек, которому не нашли ноду, не
        # продвигался бы по лестнице вовсе: следующая жалоба снова считалась
        # бы первой, и он навсегда застревал на самом дешёвом шаге.
        record_complaint(db, user, dedup_sec=dedup_sec, throttled=False, commit=True)
        # Автоматика ничего не сделала — это пуш админу (services/repair_alerts).
        from .repair_alerts import alert_repair_no_target

        alert_repair_no_target(db, user, sub, source=source, device_id=device.id)
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


def live_devices(sub) -> list:
    """Устройства подписки, которые есть смысл чинить, — один набор для
    пикеров всех каналов: ``active`` и ``failed`` (упавший apply чинится
    переносом). ``pending`` исключён: он ещё собирается (см. ``not_ready``),
    ``disabled``/``revoked`` — уже не живые. Порядок стабильный, по id."""
    if sub is None:
        return []
    live = [
        d
        for d in (sub.devices or [])
        if d.status in (models.DeviceStatus.active, models.DeviceStatus.failed)
    ]
    live.sort(key=lambda d: d.id)
    return live


def first_active_subscription(db: Session, user):
    """Подписка, которую чинят каналы без токена (бот, кабинет): первая
    активная по id — одно правило на всех, иначе «какая подписка чинится»
    зависело бы от кнопки."""
    return (
        db.query(models.Subscription)
        .filter(
            models.Subscription.user_id == user.id,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
        .order_by(models.Subscription.id)
        .first()
    )


def handle_broken_subscription(
    db: Session,
    sub,
    *,
    user,
    dedup_sec: int,
    operator: str | None = None,
    source: str = "bot_vpn_broken",
    throttle_sec: int | None = None,
    daily_max: int | None = None,
) -> RepairOutcome:
    """«Все мои устройства не работают»: перенос ВСЕЙ подписки.

    Тот же вход, что и у ``handle_broken_device`` (жалоба, единый троттл и
    суточный потолок, banned/expired-гейты), но действие — whole-sub
    ``_do_failover``: новая нода для всех устройств + user-wide бан старой.
    Лестница ротации здесь не применяется намеренно: человек сказал, что
    легла ВСЯ подписка, а не транспорт одного устройства.

    До унификации (2026-09-12) у этого пути были свой троттл (5 мин на
    юзера в боте / 5 мин на подписку в кабинете) и НЕ было записи жалобы —
    после переезда через кабинет следующий тап в боте снова считался
    «первой» жалобой.
    """
    if not user_may_repair(user):
        return RepairOutcome(action="no_subscription", scope="subscription")
    if sub is None or sub.user_id != user.id or sub.node is None:
        return RepairOutcome(action="no_subscription", scope="subscription")
    now = utcnow()
    if not _sub_is_repairable(sub, now=now):
        return RepairOutcome(action="no_subscription", scope="subscription")
    if not live_devices(sub) and any(
        d.status == models.DeviceStatus.pending for d in (sub.devices or [])
    ):
        # Все устройства ещё собираются — тот же гейт, что у per-device пути:
        # whole-sub перенос запустил бы второй холодный провижн поверх первого.
        return RepairOutcome(action="not_ready", scope="subscription")

    # Гард двойного тапа — как advisory-лок 4001 в failover_device: FOR UPDATE
    # умер бы на первом commit'е внутри миграции, а два параллельных
    # whole-sub переноса — это две миграции подписки и два бана нод.
    #
    # Лок берём на ОТДЕЛЬНОМ коннекте, выделенном на время операции, а не на
    # сессии: session-level лок живёт на коннекте, а сессия после каждого
    # commit'а возвращает свой коннект в пул и следующий statement может
    # уйти в другой — unlock тогда промахивался бы, и лок залипал бы на
    # idle-коннекте до pool_recycle (так флакали тесты с локом 4001). Свой
    # коннект гарантирует lock и unlock на одном сокете; умрёт процесс —
    # умрёт и лок.
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    lock_conn = engine.connect()
    try:
        got = lock_conn.execute(
            sql_text("SELECT pg_try_advisory_lock(4002, :sub)"), {"sub": sub.id}
        ).scalar()
        if not got:
            return RepairOutcome(
                action="throttled", retry_after_sec=30, scope="subscription"
            )
        try:
            return _repair_subscription_locked(
                db, sub, user=user, now=now, dedup_sec=dedup_sec,
                operator=operator, source=source,
                throttle_sec=throttle_sec, daily_max=daily_max,
            )
        finally:
            try:
                lock_conn.execute(
                    sql_text("SELECT pg_advisory_unlock(4002, :sub)"),
                    {"sub": sub.id},
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "self-repair: advisory unlock failed for subscription %s",
                    sub.id,
                )
    finally:
        lock_conn.close()


def _repair_subscription_locked(
    db: Session,
    sub,
    *,
    user,
    now,
    dedup_sec: int,
    operator: str | None,
    source: str,
    throttle_sec: int | None,
    daily_max: int | None,
) -> RepairOutcome:
    """Тело whole-sub починки под advisory-локом (см. handle_broken_subscription)."""
    gated = _policy_gate(
        db, user, sub,
        now=now, dedup_sec=dedup_sec,
        throttle_sec=throttle_sec, daily_max=daily_max,
        scope="subscription",
    )
    if gated is not None:
        return gated

    deduped = not record_complaint(
        db, user, dedup_sec=dedup_sec, throttled=False, commit=True
    )

    # Ленивый импорт: client_control импортирует этот модуль.
    from ..api.client_control import _do_failover

    res = _do_failover(
        db,
        sub,
        kind="user_reported",
        actor=f"user:{user.telegram_id or user.id}",
        actor_type=models.AuditActor.user,
        source=source,
        operator=operator,
        # Троттл уже применён выше единой политикой; внутренний 5-мин
        # (для control-channel/админки) здесь дал бы второе окно.
        throttle_sec=0,
    )
    if res.action == "migrated":
        node = db.get(models.VPNNode, res.target_node_id) if res.target_node_id else None
        return RepairOutcome(
            action="migrated",
            report_id=res.report_id,
            new_node_name=res.target_node_name,
            new_node_region=node.region if node else None,
            task_id=res.task_id,
            scope="subscription",
            deduped=deduped,
        )
    if res.action == "throttled":
        return RepairOutcome(
            action="throttled",
            retry_after_sec=res.retry_after_sec,
            scope="subscription",
            deduped=deduped,
        )
    if res.action == "subscription_inactive":
        return RepairOutcome(action="no_subscription", scope="subscription", deduped=deduped)
    # no_target_available / error / deferred — целевой ноды сейчас нет.
    # Откат перед алертом: после ошибки внутри переноса сессия может быть в
    # failed-состоянии, и алерт молча потерялся бы (ревью 30.09). Жалоба уже
    # закоммичена выше (record_complaint commit=True).
    db.rollback()
    from .repair_alerts import alert_repair_no_target

    detail = (
        "Целевой ноды для переноса нет (пул пуст или все исключены)"
        if res.action == "no_target_available"
        else f"Перенос подписки не удался ({res.action}) — смотреть логи backend"
    )
    alert_repair_no_target(db, user, sub, source=source, detail=detail)
    return RepairOutcome(action="no_target", scope="subscription", deduped=deduped)
