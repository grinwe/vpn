"""Target node selection для control-channel'а.

Phase A control-channel'а: клиент шлёт сигнал `connect_failed` →
backend выбирает другую ноду и migrate'ит юзера. Эта функция —
sole responsibility of выбора **куда** мигрировать.

См. docs/operations/control_channel_roadmap.md §3 (Phase A.5).
"""
from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models

logger = logging.getLogger(__name__)


def select_target_node(
    db: Session,
    current_node_id: int,
    *,
    plan_pool_id: int | None = None,
    exclude_node_ids: list[int] | None = None,
) -> models.VPNNode | None:
    """Pick the best alternative VPN node for an unhealthy subscription.

    Strategy для V1:
      * status=active, is_active=True (нода готова принимать трафик)
      * auto_diagnose_disabled_at IS NULL (оператор не замьютил —
        замьюченная нода либо известно сломана, либо в режиме maintenance,
        отправлять туда юзеров = ухудшать им жизнь)
      * id != current_node_id (очевидно, не на ту же ноду)
      * id NOT IN exclude_node_ids (caller может передать «уже пробовали»
        список, чтобы избежать loop'ов на одних и тех же fail-нодах)
      * pool_id == plan_pool_id если задан (сабка привязана к пулу — не
        мигрируем за его пределы, иначе нарушим plan-isolation)
      * ORDER BY health_score DESC NULLS LAST, RANDOM() — лучший health
        первый, при равенстве случайно (load-balance между равно-здоровыми).

    Возвращает None если кандидатов нет — caller должен handle'ить как
    «миграция невозможна, юзер сидит на текущей ноде, надо алертить
    оператора». В audit log напишем причину.

    ML-based ranking (учёт historical fail-rate per region, latency,
    тип DPI-block'а юзера) — TODO Phase D, когда накопим метрик.
    """
    q = (
        db.query(models.VPNNode)
        .filter(models.VPNNode.status == models.VPNNodeStatus.active)
        .filter(models.VPNNode.is_active.is_(True))
        .filter(models.VPNNode.id != current_node_id)
    )
    # auto_diagnose_disabled_at добавлен миграцией 0036 — getattr-guard
    # на случай pre-миграционного deploy'а.
    disabled_col = getattr(models.VPNNode, "auto_diagnose_disabled_at", None)
    if disabled_col is not None:
        q = q.filter(disabled_col.is_(None))

    excluded = list(exclude_node_ids or [])
    if excluded:
        q = q.filter(~models.VPNNode.id.in_(excluded))

    if plan_pool_id is not None:
        q = q.filter(models.VPNNode.pool_id == plan_pool_id)

    # NULLS LAST для health_score через CASE — PG-агностично, работает
    # одинаково на SQLite (для тестов) и Postgres.
    return (
        q.order_by(
            func.coalesce(models.VPNNode.health_score, -1).desc(),
            func.random(),
        )
        .first()
    )
