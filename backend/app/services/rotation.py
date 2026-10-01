"""Ротационная лестница: что делать по жалобе «VPN не работает».

Раньше ответ был один на все случаи — перенести устройство на другую ноду. Но
отказы у нас двух видов, и чаще встречается второй:

* **нода** легла/забанена/деградировала — помогает смена ноды;
* **протокол** режется (регион душит TCP-Reality, ТСПУ давит транспорт) —
  смена ноды стреляет мимо, человеку нужен другой транспорт.

Отсюда лестница, где первый шаг самый дешёвый:

| Жалоба | Что делаем | Цена |
|---|---|---|
| Первая | перетасовать протоколы на ТЕХ ЖЕ нодах | флаги в БД, мгновенно |
| Вторая | сменить сами ноды | обычный провижининг |
| Третья | выдать дубль по протоколу, который работает | +1 лег |

Первый шаг бесплатен, потому что тёплый бандл назначается целиком: все четыре
протокола на ноде уже залиты под тем же именем, три просто не опубликованы.
Переставить `leg_published` — и у человека другой транспорт через минуту, без
единого ansible-прогона. Побочно это снимает риск «кандидатов на ротацию не
хватит»: шаг 1 к пулу свободных нод не обращается вовсе.

hy2 в шаге 1 уезжает на ДРУГУЮ ноду, а не меняет протокол: его устойчивость
именно в транспорте (UDP/QUIC проходит там, где душат TCP), и если не работает
он — TCP-варианты на этой ноде уже есть и, судя по жалобе, тоже не работают.
Перестановка ролей по кольцу нод даёт это автоматически.

Ограничения, записанные честно: перестановка не спасёт, если легла сама нода
(её протоколы мертвы одинаково) — это ловит второй шаг; и запас перестановок
конечен: протоколов на ноде четыре, значит не больше трёх ходов.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from prometheus_client import Counter
from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow
from . import leg_scheme

logger = logging.getLogger(__name__)

STEP_RESHUFFLE = "reshuffle"
STEP_RELOCATE = "relocate"
STEP_DUPLICATE = "duplicate"

ROTATION_STEPS = Counter(
    "vpn_rotation_steps_total",
    "Шаги ротационной лестницы",
    ["step", "outcome"],
)


def ladder_window_sec() -> int:
    """Окно, в котором жалобы считаются одной серией.

    Сутки: жалоба неделю спустя — это новая история (регион мог разблокироваться,
    ноды сменились), и начинать её надо снова с дешёвого шага.
    """
    try:
        return int(os.getenv("ROTATION_LADDER_WINDOW_SEC", "86400") or "86400")
    except ValueError:
        return 86400


def complaints_in_window(db: Session, user_id: int) -> int:
    """Сколько жалоб человек подал за окно (дребезг уже схлопнут дедупом).

    Жалобы, по которым ничего не делали (``extra.throttled = true`` — человек
    упёрся в окно повторов или суточный потолок), ступень НЕ двигают: иначе
    два нетерпеливых тапа внутри окна перепрыгивали смену ноды и сразу
    давали дубль. В телеметрии они остаются (ревью 2026-09-13).
    """
    from sqlalchemy import or_

    cutoff = utcnow() - timedelta(seconds=ladder_window_sec())
    throttled = models.AuditLog.extra["throttled"].astext
    return (
        db.query(models.AuditLog.id)
        .filter(
            models.AuditLog.action == "complaint_received",
            models.AuditLog.target_type == "user",
            models.AuditLog.target_id == user_id,
            models.AuditLog.created_at >= cutoff,
            or_(throttled.is_(None), throttled != "true"),
        )
        .count()
    )


def decide_step(db: Session, user_id: int) -> str:
    """Какой шаг лестницы применить к этой жалобе.

    Без схемы 4×1 лестницы нет: перетасовывать нечего (публикуются все
    протоколы сразу), поэтому сразу прежнее поведение — смена ноды.
    """
    if not leg_scheme.leg_scheme_enabled():
        return STEP_RELOCATE
    count = complaints_in_window(db, user_id)
    if count <= 1:
        return STEP_RESHUFFLE
    if count == 2:
        return STEP_RELOCATE
    return STEP_DUPLICATE


def reshuffle_legs(db: Session, device: models.Device) -> leg_scheme.LegAssignment | None:
    """Шаг 1: тот же набор нод, другие протоколы. Только флаги в БД.

    ``None`` — перетасовать не удалось (набор не изменился): значит запас
    перестановок исчерпан или на нодах нет альтернативных протоколов, и
    вызывающий обязан перейти к следующему шагу лестницы. Возвращать «успех»
    при неизменном наборе нельзя: человек нажал кнопку, получил «готово» и
    ровно тот же список — хуже, чем честное «переносим».
    """
    before = leg_scheme.current_pairs(device)
    if not before:
        return None

    plan = leg_scheme.apply_leg_scheme(db, device, commit=True, forbid=before)
    if plan is None:
        return None

    after = {
        (role, cred.node_id)
        for role, cred in plan.assigned.items()
        if cred.node_id is not None
    }
    if not after or after == before:
        ROTATION_STEPS.labels(step=STEP_RESHUFFLE, outcome="exhausted").inc()
        logger.info(
            "rotation: device %s — перетасовка ничего не изменила, нужен перенос",
            device.id,
        )
        return None

    ROTATION_STEPS.labels(step=STEP_RESHUFFLE, outcome="ok").inc()
    logger.info(
        "rotation: device %s — протоколы перетасованы на тех же нодах (%d лег(ов))",
        device.id,
        len(plan.assigned),
    )
    return plan


def working_protocol_hint(db: Session, device: models.Device) -> str | None:
    """Какой протокол у человека, судя по всему, работает.

    Эвристика для дубля: берём hy2, если он в наборе, — по регионам с жёстким
    DPI (Ярославль, Тула) вывозит именно он. Иначе — первую роль набора.
    Точного сигнала у нас нет: per-leg телеметрии от клиента не приходит.
    """
    published = [
        cred
        for cred in (device.credentials or [])
        if cred.leg_published and cred.is_active and cred.leg_role != leg_scheme.DUP_ROLE
    ]
    if not published:
        return None
    for cred in published:
        if cred.proto == "hysteria2":
            return cred.proto
    return published[0].proto


def duplicate_leg_cap() -> int:
    """Сколько дублей максимум держим на устройстве.

    Дубли платные: каждый — лишний кред на ноде и лишняя строка в списке.
    Смысл был в том, чтобы не платить ими за всех, поэтому потолок жёсткий.
    """
    try:
        return max(0, int(os.getenv("SUB_LEG_DUP_MAX", "1") or "1"))
    except ValueError:
        return 1


def grant_duplicate_leg(db: Session, device: models.Device) -> models.Credential | None:
    """Шаг 3: второй лег по протоколу, который у человека работает.

    Человек с единственным живым протоколом (регион режет всё остальное) при
    падении своей ноды остаётся без связи вовсе. Дубль по тому же протоколу с
    ДРУГОЙ ноды — единственное, что его страхует. Всем подряд мы такое не
    выдаём: большинству оно не нужно ни разу.

    ``None`` — подходящего крела нет: на других нодах устройства этого
    протокола не оказалось (либо потолок дублей исчерпан).
    """
    if not leg_scheme.leg_scheme_enabled():
        return None

    creds = list(device.credentials or [])
    existing_dups = [
        c for c in creds if c.leg_role == leg_scheme.DUP_ROLE and c.leg_published
    ]
    if len(existing_dups) >= duplicate_leg_cap():
        ROTATION_STEPS.labels(step=STEP_DUPLICATE, outcome="capped").inc()
        return None

    proto = working_protocol_hint(db, device)
    if proto is None:
        return None

    # Исключаем только ноды, где ЭТОТ протокол уже опубликован. Ноду, занятую
    # другой ролью, брать можно и нужно: при схеме 4×1 свободных нод у
    # устройства попросту нет, а страхует дубль именно от падения ноды с
    # рабочим протоколом — значит второй hy2 должен стоять не там, где первый.
    proto_nodes = {
        c.node_id
        for c in creds
        if c.proto == proto and c.leg_published and c.node_id is not None
    }
    candidates = [
        c
        for c in creds
        if c.proto == proto
        and c.is_active
        and c.node_id is not None
        and c.node_id not in proto_nodes
        and not c.leg_published
    ]
    if not candidates:
        # Дубль на той же ноде бессмыслен: ляжет нода — умрут оба лега.
        ROTATION_STEPS.labels(step=STEP_DUPLICATE, outcome="no_candidate").inc()
        logger.info(
            "rotation: device %s — дубль по %s негде взять (нет свободной ноды)",
            device.id,
            proto,
        )
        return None

    dup = sorted(candidates, key=lambda c: c.id or 0)[0]
    dup.leg_published = True
    dup.leg_role = leg_scheme.DUP_ROLE
    db.add(dup)
    db.commit()
    ROTATION_STEPS.labels(step=STEP_DUPLICATE, outcome="ok").inc()
    logger.info(
        "rotation: device %s — выдан дубль %s с ноды %s",
        device.id,
        proto,
        dup.node_id,
    )
    return dup
