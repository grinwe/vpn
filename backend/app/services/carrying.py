"""Phase C — carrying_fraction: детект блокировок по поведению клиентов (read-only).

Идея (см. docs/operations/diverse_subscription_epic.md): при диверсной N×M
подписке клиент сам прыгает на живой эндпоинт. Значит «нода широко мертва»
видно по тому, что клиенты, у кого она В НАБОРЕ, на ней БОЛЬШЕ НЕ КРУТЯТСЯ:

    carrying_fraction(нода) = (девайсов, чей трафик СЕЙЧАС на ноде)
                              / (девайсов, у кого активный credential на ноде)

Падение к нулю = широкий блок (клиенты ушли на 2nd-best), хотя нода «жива» для
тех немногих, кого ещё тащит. Нормировано на популяцию, у кого нода доступна →
переживает разброс доступности. Чисто из уже собираемой телеметрии
(NodeTrafficSample.details users → access_username → Credential → device) — без
проб, без палева. ОСМЫСЛЕН только когда наборы мультинодовые (DIVERSE_SUB_NODES>1);
при однонодовых подписках ≈ обычная утилизация ноды.

Только чтение. Тренд вниз во времени — сигнал «пора списать/заменить IP».
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..time_utils import utcnow


def _sample_usernames(sample: models.NodeTrafficSample | None) -> set[str]:
    """access_username'ы, реально замеченные в последнем traffic-сэмпле ноды.
    Толерантен к legacy-формату (``details[proto]['users']`` == int)."""
    if sample is None:
        return set()
    out: set[str] = set()
    for proto, payload in (sample.details or {}).items():
        if proto == "_errors" or not isinstance(payload, dict):
            continue
        users = payload.get("users")
        if not isinstance(users, list):
            continue
        for u in users:
            if isinstance(u, str) and u:
                out.add(u)
    return out


def _healthy_device_count(db: Session, node_id: int, extra: Any = None) -> int:
    """distinct device_id активных кредов на ноде, но ТОЛЬКО у «живых» девайсов
    (audit #8). Креды протухших/замороженных подписок и отключённых девайсов
    ещё могут висеть is_active=True (подметаются асинхронно), а их владельцы
    трафик не гонят: они раздували бы знаменатель eligible и топили
    carrying_fraction, маскируя мёртвые подписки под широкий блок ноды. Фильтр
    один и тот же для eligible и carrying — иначе доля разъедется выше 1.0.
    «Живой» = подписка status=active И девайс status=active."""
    q = (
        db.query(func.count(func.distinct(models.Credential.device_id)))
        .join(models.Device, models.Credential.device_id == models.Device.id)
        .join(
            models.Subscription,
            models.Device.subscription_id == models.Subscription.id,
        )
        .filter(
            models.Credential.node_id == node_id,
            models.Credential.is_active.is_(True),
            models.Credential.device_id.isnot(None),
            models.Device.status == models.DeviceStatus.active,
            models.Subscription.status == models.SubscriptionStatus.active,
        )
    )
    if extra is not None:
        q = q.filter(extra)
    return int(q.scalar() or 0)


def compute_carrying_fractions(db: Session) -> list[dict[str, Any]]:
    """Per-node carrying_fraction по последнему traffic-сэмплу. Read-only."""
    nodes = (
        db.query(models.VPNNode)
        .filter(models.VPNNode.is_active.is_(True))
        .order_by(models.VPNNode.id)
        .all()
    )
    out: list[dict[str, Any]] = []
    for node in nodes:
        eligible = _healthy_device_count(db, node.id)
        sample = (
            db.query(models.NodeTrafficSample)
            .filter(models.NodeTrafficSample.node_id == node.id)
            .order_by(models.NodeTrafficSample.observed_at.desc())
            .first()
        )
        # Сэмпл старше порога протух: сбор трафика на ноде мог умереть неделю
        # назад, и carrying той давности нельзя выдавать как текущий (это
        # маскирует именно тот случай, что метрика должна ловить). Порог —
        # несколько трафик-тиков (TRAFFIC_STATS_INTERVAL=300s по умолчанию),
        # переопределяется CARRYING_SAMPLE_MAX_AGE_MIN; <=0 отключает проверку.
        interval = int(os.getenv("TRAFFIC_STATS_INTERVAL", "300"))
        max_age_min = float(
            os.getenv("CARRYING_SAMPLE_MAX_AGE_MIN", str(interval * 3 / 60))
        )
        stale = bool(
            sample is not None
            and max_age_min > 0
            and sample.observed_at is not None
            and sample.observed_at < utcnow() - timedelta(minutes=max_age_min)
        )
        usernames = _sample_usernames(sample)
        if usernames:
            # Тот же фильтр «живых» девайсов, что и в eligible (is_active +
            # активная подписка + активный девайс) — иначе девайс с
            # деактивированным кредом/мёртвой подпиской, чей access_username ещё
            # светится в сэмпле, попал бы в числитель, но не в знаменатель, и
            # carrying_fraction вылез бы за 1.0. С общим фильтром carrying —
            # строгое подмножество eligible.
            carrying = _healthy_device_count(
                db,
                node.id,
                extra=models.Credential.access_username.in_(list(usernames)),
            )
        else:
            carrying = 0
        frac = None if stale else (round(carrying / eligible, 3) if eligible else None)
        out.append(
            {
                "node_id": node.id,
                "name": node.name,
                "region": node.region,
                "eligible_devices": int(eligible),
                "carrying_devices": int(carrying),
                "carrying_fraction": frac,
                "sample_at": sample.observed_at.isoformat() if sample else None,
                "stale": stale,
            }
        )
    return out
