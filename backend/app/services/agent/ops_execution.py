"""Серверная валидация ops-плана перед исполнением (AI_AGENT_ROADMAP Phase 3).

Между «оператор подтвердил план» и «бэкенд дёрнул order/destroy» стоит ЭТОТ слой.
План приходит от LLM и потому НЕДОВЕРЕННЫЙ: ``submit_plan.params`` —
``additionalProperties:true``, ``kind``/``tier``/``est_cost`` пишет модель. Доверять
этому на исполнении нельзя (галлюцинация ``count:50`` или ``destroy`` живой ноды =
реальные деньги/снос). ``validate_plan`` приводит план к исполнимому виду или
ОТКЛОНЯЕТ его, проверяя:

- gate 2: ``kind`` — закрытый allowlist (никаких ``other``/неизвестных); обязательные
  params присутствуют и нужного типа; каждый ``provider_id``/``node_id``/``exit_id``
  резолвится в существующую (активную) строку БД. tier — СЕРВЕРНЫЙ (из ``kind``),
  ``needs_confirmation`` — серверный (любой costly/destructive ⇒ true). Поля модели
  для гейтинга игнорируются.
- gate 3 (структурная часть): ``count`` ограничен ``OPS_MAX_ORDER_COUNT``; число
  заказов в плане ≤ ``OPS_MAX_NODES_PER_PLAN``. Денежный spend-cap по ЖИВЫМ ценам +
  баланс — pre-flight исполнителя (там сеть/драйвер), не здесь.
- gate 6: destructive-инвариант — ``destroy``/``reinstall`` ноды с
  ``assigned_subscriptions>0`` отклоняется, ПОКА в плане раньше нет ``migrate_users``
  с этой ноды-источника.

Сетевую валидацию (регион/тариф/ОС по живым offerings, цена×count, баланс) делает
pre-flight исполнителя — отдельно, чтобы этот слой оставался детерминированным и
полностью покрывался тестами без сети. Здесь НИЧЕГО не исполняется.
"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import models

# Серверная карта kind → tier (НЕ доверяем tier от LLM).
_KIND_TIER: dict[str, str] = {
    "order_node": "costly",
    "order_exit": "costly",
    "attach_tunnel": "reversible",
    "migrate_users": "destructive",
    "reinstall": "destructive",
    "destroy": "destructive",
    "set_active": "reversible",
}
_ALLOWED_KINDS = frozenset(_KIND_TIER)
_DESTRUCTIVE_KINDS = frozenset({"reinstall", "destroy"})


def _max_order_count() -> int:
    return max(1, int(os.getenv("OPS_MAX_ORDER_COUNT", "5")))


def _max_nodes_per_plan() -> int:
    return max(1, int(os.getenv("OPS_MAX_NODES_PER_PLAN", "5")))


def _assigned_subscriptions(db: Session, node_id: int) -> int:
    """Сколько живых подписок провижинено на ноде (для destructive-инварианта)."""
    return int(
        db.query(func.count(func.distinct(models.Credential.subscription_id)))
        .filter(
            models.Credential.node_id == node_id,
            models.Credential.subscription_id.isnot(None),
            models.Credential.pool_state == models.CredentialPoolState.assigned,
        )
        .scalar()
        or 0
    )


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):  # bool — подтип int, но не то, что нужно
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_plan(db: Session, ops_plan: models.OpsPlan) -> dict[str, Any]:
    """Привести сохранённый план к исполнимому виду или отклонить.

    Возвращает ``{ok, rejections, needs_confirmation, steps, totals}``. ``ok`` —
    можно ли исполнять; ``rejections`` — почему нельзя; ``steps`` — провалидированные
    шаги с серверным tier и резолвнутыми id; ``totals`` — агрегаты (число заказов,
    нод). НИЧЕГО не исполняет и не ходит в сеть."""
    rejections: list[str] = []
    out_steps: list[dict[str, Any]] = []

    raw_plan = ops_plan.plan or {}
    if raw_plan.get("feasible") is False:
        rejections.append("план помечен планировщиком как невыполнимый (feasible=false)")

    steps = raw_plan.get("steps") or []
    if not isinstance(steps, list) or not steps:
        rejections.append("в плане нет шагов")
        steps = []

    # Источники миграции, увиденные ДО текущего шага (для destructive-инварианта:
    # снести/реинсталлить ноду можно только если её юзеров раньше увели).
    migrated_sources: set[int] = set()
    order_count = 0

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            rejections.append(f"шаг #{idx + 1}: не объект")
            continue
        kind = str(step.get("kind") or "").strip()
        params = step.get("params") or {}
        if not isinstance(params, dict):
            rejections.append(f"шаг #{idx + 1} ({kind or '?'}): params не объект")
            params = {}

        if kind not in _ALLOWED_KINDS:
            rejections.append(
                f"шаг #{idx + 1}: kind={kind!r} вне allowlist "
                f"({', '.join(sorted(_ALLOWED_KINDS))})"
            )
            continue

        tier = _KIND_TIER[kind]  # серверный, не от LLM
        resolved: dict[str, Any] = {}
        step_errors: list[str] = []

        if kind in ("order_node", "order_exit"):
            provider_id = _as_int(params.get("provider_id"))
            if provider_id is None:
                step_errors.append("нет/битый provider_id")
            else:
                provider = db.get(models.CloudProvider, provider_id)
                if not provider:
                    step_errors.append(f"provider_id={provider_id} не найден")
                elif not provider.is_active:
                    step_errors.append(f"provider_id={provider_id} неактивен")
                else:
                    resolved["provider_id"] = provider_id
            count = _as_int(params.get("count", 1)) or 1
            if count < 1 or count > _max_order_count():
                step_errors.append(
                    f"count={count} вне [1..{_max_order_count()}] (OPS_MAX_ORDER_COUNT)"
                )
            else:
                resolved["count"] = count
                order_count += count

        elif kind in _DESTRUCTIVE_KINDS:  # destroy | reinstall
            node_id = _as_int(params.get("node_id"))
            if node_id is None:
                step_errors.append("нет/битый node_id")
            else:
                node = db.get(models.VPNNode, node_id)
                if not node:
                    step_errors.append(f"node_id={node_id} не найдена во флоте")
                else:
                    resolved["node_id"] = node_id
                    assigned = _assigned_subscriptions(db, node_id)
                    if assigned > 0 and node_id not in migrated_sources:
                        step_errors.append(
                            f"{kind} ноды #{node_id} с {assigned} живыми подписками "
                            "без предшествующей migrate_users — запрещено (gate 6)"
                        )
                    resolved["assigned_subscriptions"] = assigned

        elif kind == "migrate_users":
            src = _as_int(params.get("from_node_id") or params.get("node_id"))
            dst = _as_int(params.get("to_node_id") or params.get("target_node_id"))
            if src is None:
                step_errors.append("нет/битый from_node_id")
            elif not db.get(models.VPNNode, src):
                step_errors.append(f"from_node_id={src} не найдена")
            else:
                resolved["from_node_id"] = src
                migrated_sources.add(src)
            if dst is None:
                step_errors.append("нет/битый to_node_id")
            else:
                tgt = db.get(models.VPNNode, dst)
                if not tgt:
                    step_errors.append(f"to_node_id={dst} не найдена")
                elif not tgt.is_active:
                    step_errors.append(f"to_node_id={dst} неактивна как цель миграции")
                else:
                    resolved["to_node_id"] = dst

        # attach_tunnel / set_active — reversible; глубокую валидацию целей делает
        # pre-flight исполнителя (нужны relay/exit-связки). Здесь пропускаем.

        if step_errors:
            for e in step_errors:
                rejections.append(f"шаг #{idx + 1} ({kind}): {e}")

        out_steps.append(
            {
                "index": idx,
                "kind": kind,
                "tier": tier,
                "params": params,
                "resolved": resolved,
                "errors": step_errors,
            }
        )

    if order_count > _max_nodes_per_plan():
        rejections.append(
            f"заказов в плане {order_count} > потолка {_max_nodes_per_plan()} "
            "(OPS_MAX_NODES_PER_PLAN)"
        )

    # Серверный needs_confirmation: любой costly/destructive шаг ⇒ нужно подтверждение
    # (флаг модели игнорируем).
    needs_confirmation = any(
        s["tier"] in ("costly", "destructive") for s in out_steps
    )

    return {
        "ok": not rejections,
        "rejections": rejections,
        "needs_confirmation": needs_confirmation,
        "steps": out_steps,
        "totals": {
            "order_count": order_count,
            "steps": len(out_steps),
            "destructive": sum(1 for s in out_steps if s["tier"] == "destructive"),
        },
    }
