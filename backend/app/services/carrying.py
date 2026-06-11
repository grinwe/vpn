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

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models


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
        eligible = (
            db.query(func.count(func.distinct(models.Credential.device_id)))
            .filter(
                models.Credential.node_id == node.id,
                models.Credential.is_active.is_(True),
                models.Credential.device_id.isnot(None),
            )
            .scalar()
            or 0
        )
        sample = (
            db.query(models.NodeTrafficSample)
            .filter(models.NodeTrafficSample.node_id == node.id)
            .order_by(models.NodeTrafficSample.observed_at.desc())
            .first()
        )
        usernames = _sample_usernames(sample)
        if usernames:
            carrying = (
                db.query(func.count(func.distinct(models.Credential.device_id)))
                .filter(
                    models.Credential.node_id == node.id,
                    models.Credential.device_id.isnot(None),
                    models.Credential.access_username.in_(list(usernames)),
                )
                .scalar()
                or 0
            )
        else:
            carrying = 0
        frac = round(carrying / eligible, 3) if eligible else None
        out.append(
            {
                "node_id": node.id,
                "name": node.name,
                "region": node.region,
                "eligible_devices": int(eligible),
                "carrying_devices": int(carrying),
                "carrying_fraction": frac,
                "sample_at": sample.observed_at.isoformat() if sample else None,
            }
        )
    return out
