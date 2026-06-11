"""Read-only fleet tools for the ops PLANNER (AI_AGENT_ROADMAP Phase 2, dry-run).

Тонкие read-обёртки над моделями/драйверами для планировщика: список
провайдеров/нод/exit'ов/пулов, offerings с ценами и баланс провайдера (для оценки
стоимости), нагрузка ноды (для оценки миграции). Никаких мутаций, commit'ов, SSH.
Тулы с обращением к API провайдера (balance/offerings) изолируют ошибку в
``{"error": ...}`` — провайдерский сбой не должен валить планирование.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import models


def _enum(value) -> str | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def list_providers(db: Session) -> dict[str, Any]:
    """Облачные провайдеры: id, name, kind, активность, дефолты (регион/тариф/ОС)."""
    rows = db.query(models.CloudProvider).order_by(models.CloudProvider.id).all()
    return {
        "providers": [
            {
                "id": p.id,
                "name": p.name,
                "kind": _enum(p.kind),
                "is_active": p.is_active,
                "default_region": p.default_region,
                "default_plan": p.default_plan,
                "default_image": p.default_image,
            }
            for p in rows
        ]
    }


def provider_balance(db: Session, provider_id: int) -> dict[str, Any]:
    """Текущий баланс провайдера (для оценки, хватит ли на заказ). Бьёт по API
    провайдера — read-only."""
    from ..cloud import DriverError, get_driver

    p = db.get(models.CloudProvider, provider_id)
    if not p:
        return {"error": f"provider {provider_id} not found"}
    try:
        driver = get_driver(p)
        if not hasattr(driver, "get_balance"):
            return {"provider_id": provider_id, "balance": None, "note": "driver не умеет get_balance"}
        return {"provider_id": provider_id, "balance": driver.get_balance()}
    except DriverError as exc:
        return {"provider_id": provider_id, "error": str(exc)}


def provider_offerings(db: Session, provider_id: int) -> dict[str, Any]:
    """Локации (datacenters) и тарифы (plans с ценой) провайдера — для выбора
    реальных id и оценки стоимости. Бьёт по API провайдера — read-only.
    Образы зависят от пары (тариф, локация); общий каталог — в plans[].images
    либо отдельным запросом, тут не тянем чтобы не плодить вызовы."""
    from ..cloud import DriverError, get_driver

    p = db.get(models.CloudProvider, provider_id)
    if not p:
        return {"error": f"provider {provider_id} not found"}
    try:
        driver = get_driver(p)
        dcs = driver.list_datacenters() if hasattr(driver, "list_datacenters") else []
        plans = driver.list_plans() if hasattr(driver, "list_plans") else []
        # Не раздуваем контекст: только ключевые поля.
        return {
            "provider_id": provider_id,
            "datacenters": [
                {"id": d.get("id"), "name": d.get("name"), "flag": d.get("flag")}
                for d in (dcs or [])
            ],
            "plans": [
                {
                    "id": pl.get("id"),
                    "name": pl.get("name"),
                    "price": pl.get("price"),
                    "cpu": pl.get("cpu"),
                    "ram_mib": pl.get("ram_mib"),
                }
                for pl in (plans or [])
            ],
        }
    except DriverError as exc:
        return {"provider_id": provider_id, "error": str(exc)}


def list_nodes(db: Session, status: str | None = None) -> dict[str, Any]:
    """VPN-ноды (опц. фильтр по статусу): id, name, region, status, is_active,
    provider_id, pool_id, host."""
    q = db.query(models.VPNNode)
    if status:
        try:
            q = q.filter(models.VPNNode.status == models.VPNNodeStatus(status))
        except ValueError:
            return {"error": f"invalid status {status!r}"}
    rows = q.order_by(models.VPNNode.id).all()
    return {
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "region": n.region,
                "status": _enum(n.status),
                "is_active": n.is_active,
                "provider_id": n.provider_id,
                "pool_id": n.pool_id,
                "host": n.host,
            }
            for n in rows
        ]
    }


def list_exits(db: Session) -> dict[str, Any]:
    """WG-exit-ноды: id, name, region, status, is_active, provider_id, host."""
    rows = db.query(models.WGExitNode).order_by(models.WGExitNode.id).all()
    return {
        "exits": [
            {
                "id": e.id,
                "name": e.name,
                "region": e.region,
                "status": _enum(e.status),
                "is_active": e.is_active,
                "provider_id": e.provider_id,
                "host": e.host,
            }
            for e in rows
        ]
    }


def node_load(db: Session, node_id: int) -> dict[str, Any]:
    """Нагрузка ноды для оценки миграции: сколько подписок на ней провижинено
    (assigned-credentials) + active_users из последнего traffic-сэмпла."""
    n = db.get(models.VPNNode, node_id)
    if not n:
        return {"error": f"node {node_id} not found"}
    assigned_subs = (
        db.query(func.count(func.distinct(models.Credential.subscription_id)))
        .filter(
            models.Credential.node_id == node_id,
            models.Credential.subscription_id.isnot(None),
            models.Credential.pool_state == models.CredentialPoolState.assigned,
        )
        .scalar()
        or 0
    )
    sample = (
        db.query(models.NodeTrafficSample)
        .filter(models.NodeTrafficSample.node_id == node_id)
        .order_by(models.NodeTrafficSample.observed_at.desc())
        .first()
    )
    return {
        "node_id": node_id,
        "name": n.name,
        "status": _enum(n.status),
        "assigned_subscriptions": int(assigned_subs),
        "active_users_latest": sample.active_users if sample else None,
    }


def list_pools(db: Session) -> dict[str, Any]:
    """Server-пулы (куда складывать ноды): id, name."""
    rows = db.query(models.ServerPool).order_by(models.ServerPool.id).all()
    return {"pools": [{"id": p.id, "name": p.name} for p in rows]}


TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "list_providers": {
        "fn": list_providers,
        "schema": {
            "name": "list_providers",
            "description": "Облачные провайдеры (id, name, kind, is_active, дефолты). Начни с этого, если в команде упомянут заказ/провайдер.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    "provider_balance": {
        "fn": provider_balance,
        "schema": {
            "name": "provider_balance",
            "description": "Текущий баланс провайдера — хватит ли на заказ. Бьёт по API провайдера.",
            "input_schema": {
                "type": "object",
                "properties": {"provider_id": {"type": "integer"}},
                "required": ["provider_id"],
            },
        },
    },
    "provider_offerings": {
        "fn": provider_offerings,
        "schema": {
            "name": "provider_offerings",
            "description": "Локации (datacenters) и тарифы (plans с ценой ₽) провайдера. Отсюда бери реальные id региона/тарифа и цену для оценки стоимости.",
            "input_schema": {
                "type": "object",
                "properties": {"provider_id": {"type": "integer"}},
                "required": ["provider_id"],
            },
        },
    },
    "list_nodes": {
        "fn": list_nodes,
        "schema": {
            "name": "list_nodes",
            "description": "VPN-ноды флота (опц. фильтр status: registering|active|error|disabled|draining).",
            "input_schema": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
            },
        },
    },
    "list_exits": {
        "fn": list_exits,
        "schema": {
            "name": "list_exits",
            "description": "WG-exit-ноды (зарубежные, за РУ-relay).",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    "node_load": {
        "fn": node_load,
        "schema": {
            "name": "node_load",
            "description": "Нагрузка ноды для оценки миграции: сколько подписок провижинено + active_users. Зови для каждой ноды-источника миграции.",
            "input_schema": {
                "type": "object",
                "properties": {"node_id": {"type": "integer"}},
                "required": ["node_id"],
            },
        },
    },
    "list_pools": {
        "fn": list_pools,
        "schema": {
            "name": "list_pools",
            "description": "Server-пулы (куда поместить заказанные ноды): id, name.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
}


def tool_schemas() -> list[dict[str, Any]]:
    return [entry["schema"] for entry in TOOL_REGISTRY.values()]
