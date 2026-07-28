"""Схема набора эндпоинтов 4×1: одна роль — один протокол — одна нода.

Раньше девайс получал 12 записей (3 ноды × 4 протокола): человек видел стену
одинаковых строк, а покрывали они всего 3 ноды. Четыре записи, разнесённые по
четырём разным нодам, покрывают 4 ноды И 4 протокола — больше покрытия втрое
меньшим списком.

Ключевое решение: **тёплый бандл по-прежнему назначается целиком**. На ноде под
одним именем физически залиты все её протоколы, просто три из них не
опубликованы (`Credential.leg_published = False`). Отсюда главное свойство
схемы: «сменить reality на xhttp на той же ноде» стоит переставить флаг в БД —
ноль ansible-прогонов, ноль новых кредов, мгновенно. На этом стоит первый шаг
ротационной лестницы (см. ``rotation.py``).

Подбор ролей — это паросочетание, а не жадный проход: у ноды может не быть
нужного протокола (hy2 поднят не везде), и наивное «первой ноде — reality»
способно оставить роль без ноды, хотя полный набор собирался. Ролей и нод здесь
единицы, поэтому берём алгоритм Куна — он даёт МАКСИМАЛЬНОЕ покрытие ролей.

Документ: ``docs/operations/subset_epic_2026_07_29.md``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from prometheus_client import Counter, Gauge
from sqlalchemy.orm import Session

from .. import models
from .admin_notify import notify_admins

logger = logging.getLogger(__name__)

# Роль → протокол. Порядок = приоритет: чем выше, тем важнее не потерять роль
# при дефиците нод, и тем выше строка в списке у человека.
LEG_ROLES: tuple[tuple[str, str], ...] = (
    ("primary", "vless-reality"),
    ("fast", "hysteria2"),
    ("backup", "vless-xhttp"),
    ("reserve", "vless-ws-cdn"),
)
ROLE_BY_PROTO = {proto: role for role, proto in LEG_ROLES}
PROTO_BY_ROLE = dict(LEG_ROLES)

# Роль дубля, выданного по эскалации (Э5). В паросочетании не участвует:
# это ВТОРОЙ лег по уже работающему протоколу, он живёт сверх набора.
DUP_ROLE = "dup"

LEGS_MISSING = Gauge(
    "vpn_sub_legs_missing",
    "Роли набора 4×1, которые не удалось закрыть на последней раскладке",
    ["role"],
)
LEG_SCHEME_APPLIED = Counter(
    "vpn_sub_leg_scheme_applied_total",
    "Раскладок набора выполнено",
    ["outcome"],  # full | partial
)


def leg_scheme_enabled() -> bool:
    """`4x1` — новая схема; всё остальное (дефолт `legacy`) — прежнее поведение.

    Флагом, а не миграцией: откат схемы обязан стоить перезапуск контейнера.
    """
    return (os.getenv("SUB_LEG_SCHEME") or "legacy").strip().lower() == "4x1"


def target_leg_nodes() -> int:
    """Сколько РАЗНЫХ нод хотим в наборе. По одной роли с ноды ⇒ = числу ролей."""
    try:
        value = int(os.getenv("SUB_LEG_NODES", str(len(LEG_ROLES))) or len(LEG_ROLES))
    except ValueError:
        value = len(LEG_ROLES)
    return max(1, min(value, len(LEG_ROLES)))


@dataclass
class LegAssignment:
    """Итог раскладки: какие роли закрыты, какие остались без ноды."""

    assigned: dict[str, models.Credential] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    published: list[models.Credential] = field(default_factory=list)
    hidden: int = 0

    @property
    def complete(self) -> bool:
        return not self.missing


def _match_roles(
    pool: dict[int, dict[str, models.Credential]],
    roles: list[str],
    preferred: dict[str, int],
    forbid: set[tuple[str, int]] | None = None,
) -> dict[str, int]:
    """Алгоритм Куна: максимальное паросочетание роль ↔ нода.

    ``preferred`` — нода, на которой роль стояла в прошлый раз. Пробуем её
    первой, чтобы раскладка не прыгала между вызовами: у человека в клиенте
    «Основной 2» не должен произвольно менять сервер при каждом реprovision.
    """
    node_of_role: dict[str, int] = {}
    role_of_node: dict[int, str] = {}
    banned = forbid or set()

    def candidates(role: str) -> list[int]:
        proto = PROTO_BY_ROLE[role]
        nodes = [
            node_id
            for node_id, by_proto in pool.items()
            if proto in by_proto and (role, node_id) not in banned
        ]
        nodes.sort(key=lambda nid: (nid != preferred.get(role), nid))
        return nodes

    def try_assign(role: str, seen: set[int]) -> bool:
        for node_id in candidates(role):
            if node_id in seen:
                continue
            seen.add(node_id)
            holder = role_of_node.get(node_id)
            if holder is None or try_assign(holder, seen):
                role_of_node[node_id] = role
                node_of_role[role] = node_id
                return True
        return False

    for role in roles:
        try_assign(role, set())
    return node_of_role


def plan_legs(
    creds: list[models.Credential],
    *,
    roles: list[str] | None = None,
    forbid: set[tuple[str, int]] | None = None,
) -> LegAssignment:
    """Разложить креды по ролям, не трогая БД (чистая функция — её же зовут тесты).

    Учитываются только активные креды с известной нодой: неактивный кред — это
    учётка, которой на ноде уже нет, публиковать её значит показать человеку
    заведомо мёртвый эндпоинт.

    ``forbid`` — пары (роль, нода), которые запрещено брать. На этом стоит
    первый шаг ротации: запретив текущие пары, получаем набор, где КАЖДАЯ нода
    отдаёт другой протокол, — и это ровно то, что нужно, когда режут транспорт,
    а не ноду.
    """
    wanted = roles if roles is not None else [role for role, _ in LEG_ROLES]

    pool: dict[int, dict[str, models.Credential]] = {}
    dups: list[models.Credential] = []
    preferred: dict[str, int] = {}
    for cred in creds:
        if not cred.is_active or cred.node_id is None:
            continue
        if getattr(cred, "leg_role", None) == DUP_ROLE:
            dups.append(cred)
            continue
        # Дубликаты (node, proto) не должны затирать друг друга случайным
        # порядком: держим самый свежий кред — он и есть живая учётка.
        slot = pool.setdefault(cred.node_id, {})
        current = slot.get(cred.proto)
        if current is None or (cred.id or 0) > (current.id or 0):
            slot[cred.proto] = cred
        if getattr(cred, "leg_published", False):
            role = getattr(cred, "leg_role", None) or ROLE_BY_PROTO.get(cred.proto)
            if role in wanted:
                preferred.setdefault(role, cred.node_id)

    node_of_role = _match_roles(pool, wanted, preferred, forbid)

    result = LegAssignment()
    for role in wanted:
        node_id = node_of_role.get(role)
        if node_id is None:
            result.missing.append(role)
            continue
        result.assigned[role] = pool[node_id][PROTO_BY_ROLE[role]]

    chosen = {id(cred) for cred in result.assigned.values()}
    chosen.update(id(cred) for cred in dups)
    result.published = list(result.assigned.values()) + dups
    result.hidden = sum(
        1
        for cred in creds
        if cred.is_active and cred.node_id is not None and id(cred) not in chosen
    )
    return result


def apply_leg_scheme(
    db: Session,
    device: models.Device,
    *,
    commit: bool = False,
    forbid: set[tuple[str, int]] | None = None,
) -> LegAssignment | None:
    """Проставить `leg_published`/`leg_role` кредам устройства.

    ``None`` — схема выключена флагом; ни одной строки не тронуто.

    Никогда не роняет вызывающий провижининг: набор — это про удобство списка,
    а девайс к этому моменту уже рабочий.
    """
    if not leg_scheme_enabled():
        return None

    creds = list(device.credentials or [])
    plan = plan_legs(creds, forbid=forbid)
    keep = {id(cred) for cred in plan.published}

    for role, cred in plan.assigned.items():
        cred.leg_published = True
        cred.leg_role = role
        db.add(cred)
    for cred in creds:
        if id(cred) in keep:
            continue
        # Безусловно, а не «если было True»: у свежего крела из warm-пула
        # атрибут в питоне ещё None (server_default применится на INSERT), и
        # проверка «было опубликовано» пропустила бы его — после flush он
        # всплыл бы в подписке. Инвариант держим жёстким:
        # leg_role IS NOT NULL ⟺ leg_published.
        cred.leg_published = False
        cred.leg_role = None
        db.add(cred)

    for role, _ in LEG_ROLES:
        LEGS_MISSING.labels(role=role).set(1 if role in plan.missing else 0)
    LEG_SCHEME_APPLIED.labels(outcome="full" if plan.complete else "partial").inc()

    if plan.missing:
        # Не ошибка, а дефицит: warm-пул не дал ноды с нужным протоколом.
        # Наружу это выглядит как «в списке три строки вместо четырёх», и
        # заметить это можно только отсюда.
        logger.warning(
            "leg-scheme: device %s — роли без ноды: %s (закрыто %d/%d)",
            device.id,
            ",".join(plan.missing),
            len(plan.assigned),
            len(LEG_ROLES),
        )
    else:
        logger.info(
            "leg-scheme: device %s — набор собран (%d лег(ов), скрыто %d)",
            device.id,
            len(plan.published),
            plan.hidden,
        )

    if commit:
        db.commit()
    return plan


def notify_leg_gap(
    db: Session, device: models.Device, missing: list[str], *, autocommit: bool = True
) -> None:
    """Сообщить админу, что набор собрался не полностью.

    Дефицит ролей молчалив по своей природе: у человека в клиенте просто на
    строку меньше, жаловаться ему не на что — и мы узнаём об этом только когда
    ляжет его единственный рабочий протокол. Дедуп по набору ролей: пока
    дефицит один и тот же, это одна новость, а не по письму на каждую покупку.
    """
    if not missing:
        return
    roles = ",".join(sorted(missing))
    try:
        notify_admins(
            db,
            kind="leg_gap",
            text=(
                "⚠️ Набор эндпоинтов собран не полностью\n"
                f"Устройство: {device.id}\n"
                f"Не закрыты роли: {roles}\n"
                "Причина почти всегда одна: в warm-пуле нет свободной ноды с "
                "нужным протоколом. Проверьте пул и включённые VPNConfig."
            ),
            dedup_key={"roles": roles},
            extra={"device_id": device.id, "missing": sorted(missing)},
            window_sec=3600,
            autocommit=autocommit,
        )
    except Exception:  # noqa: BLE001 — алерт не важнее выданного девайса
        logger.exception("leg-scheme: не удалось уведомить админа о дефиците ролей")


def current_pairs(device: models.Device) -> set[tuple[str, int]]:
    """Текущие пары (роль, нода) набора — то, от чего должна уйти ротация."""
    return {
        (cred.leg_role, cred.node_id)
        for cred in (device.credentials or [])
        if cred.leg_published
        and cred.leg_role
        and cred.leg_role != DUP_ROLE
        and cred.node_id is not None
    }
