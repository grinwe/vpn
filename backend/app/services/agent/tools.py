"""Read-only tool layer for the diagnostic triage agent (AI_AGENT_ROADMAP Phase 1).

Каждая функция — тонкая read-обёртка над существующими моделями/сервисами,
возвращает JSON-able dict (никаких мутаций, никаких commit, никакого SSH).
Это набор тулов, которые получает Claude tool-use: фактаж для корреляции
«почему ноде плохо». Расширять осторожно — на этой фазе ТОЛЬКО read.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ... import models


def _dt(value) -> str | None:
    return value.isoformat() if value is not None else None


def _enum(value) -> str | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def get_node_overview(db: Session, node_id: int) -> dict[str, Any]:
    """Базовые факты о ноде: статус, host, регион, health, флаги, reconcile."""
    n = db.get(models.VPNNode, node_id)
    if not n:
        return {"error": f"node {node_id} not found"}
    return {
        "id": n.id,
        "name": n.name,
        "region": n.region,
        "host": n.host,
        "status": _enum(n.status),
        "is_active": n.is_active,
        "health_score": n.health_score,
        "last_health_check_at": _dt(n.last_health_check_at),
        "suspect_since": _dt(getattr(n, "suspect_since", None)),
        "cooldown_until": _dt(getattr(n, "cooldown_until", None)),
        "provider_id": n.provider_id,
        "provider_external_id": n.provider_external_id,
        "reconcile_pending": (n.desired_generation or 0) > (n.reconciled_generation or 0),
        "reconcile_due_at": _dt(getattr(n, "reconcile_due_at", None)),
        "notes": n.notes,
        "updated_at": _dt(n.updated_at),
    }


def get_node_configs(db: Session, node_id: int) -> dict[str, Any]:
    """Какие протоколы нода обслуживает (protocol/port/sni/enabled)."""
    rows = (
        db.query(models.VPNConfig)
        .filter(models.VPNConfig.node_id == node_id)
        .all()
    )
    return {
        "configs": [
            {
                "protocol": _enum(c.protocol),
                "port": c.port,
                "sni": c.sni,
                "is_enabled": c.is_enabled,
            }
            for c in rows
        ]
    }


def get_node_health_probes(db: Session, node_id: int, limit: int = 10) -> dict[str, Any]:
    """Последние health-пробы по ноде (result/latency/source-region/observed)."""
    limit = max(1, min(int(limit or 10), 50))
    rows = (
        db.query(models.HealthProbe)
        .filter(models.HealthProbe.node_id == node_id)
        .order_by(models.HealthProbe.observed_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "probes": [
            {
                "source_region": p.source_region,
                "source_kind": p.source_kind,
                "result": _enum(p.result),
                "latency_ms": p.latency_ms,
                "observed_at": _dt(p.observed_at),
            }
            for p in rows
        ]
    }


def get_node_traffic(db: Session, node_id: int, limit: int = 5) -> dict[str, Any]:
    """Последние traffic-сэмплы (uplink/downlink/active_users) — прокси
    «нода жива и через неё идёт трафик»."""
    limit = max(1, min(int(limit or 5), 20))
    rows = (
        db.query(models.NodeTrafficSample)
        .filter(models.NodeTrafficSample.node_id == node_id)
        .order_by(models.NodeTrafficSample.observed_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "samples": [
            {
                "observed_at": _dt(s.observed_at),
                "interval_seconds": s.interval_seconds,
                "uplink_bytes": s.uplink_bytes,
                "downlink_bytes": s.downlink_bytes,
                "active_users": s.active_users,
            }
            for s in rows
        ]
    }


def get_node_provisioning_tasks(
    db: Session, node_id: int, limit: int = 10
) -> dict[str, Any]:
    """Последние provisioning-таски ноды (action/status/error) — что недавно
    катали и что упало. Главный источник «почему bootstrap не прошёл»."""
    limit = max(1, min(int(limit or 10), 30))
    rows = (
        db.query(models.ProvisioningTask)
        .filter(
            models.ProvisioningTask.target_type == "node",
            models.ProvisioningTask.target_id == node_id,
        )
        .order_by(models.ProvisioningTask.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "tasks": [
            {
                "id": t.id,
                "action": t.action,
                "status": _enum(t.status),
                "error_message": (t.error_message or "")[:500] or None,
                "created_at": _dt(t.created_at),
                "started_at": _dt(t.started_at),
                "finished_at": _dt(t.finished_at),
            }
            for t in rows
        ]
    }


# Реестр: имя-тула → (callable, JSON-schema для Claude tool-use). Все read-only.
TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_node_overview": {
        "fn": get_node_overview,
        "schema": {
            "name": "get_node_overview",
            "description": "Базовые факты о ноде: статус, host, регион, health_score, флаги (is_active, cooldown, suspect), reconcile_pending. Начни с этого.",
            "input_schema": {
                "type": "object",
                "properties": {"node_id": {"type": "integer"}},
                "required": ["node_id"],
            },
        },
    },
    "get_node_configs": {
        "fn": get_node_configs,
        "schema": {
            "name": "get_node_configs",
            "description": "Какие VPN-протоколы нода обслуживает (protocol/port/sni/is_enabled).",
            "input_schema": {
                "type": "object",
                "properties": {"node_id": {"type": "integer"}},
                "required": ["node_id"],
            },
        },
    },
    "get_node_health_probes": {
        "fn": get_node_health_probes,
        "schema": {
            "name": "get_node_health_probes",
            "description": "Последние health-пробы (result=ok/timeout/refused/tls_fail, latency, source-region). Покажет, блокируется ли нода на конкретных ISP/регионах.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "integer"},
                    "limit": {"type": "integer", "description": "до 50, дефолт 10"},
                },
                "required": ["node_id"],
            },
        },
    },
    "get_node_traffic": {
        "fn": get_node_traffic,
        "schema": {
            "name": "get_node_traffic",
            "description": "Последние traffic-сэмплы (uplink/downlink/active_users). Нули при наличии юзеров = трафик не идёт.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "integer"},
                    "limit": {"type": "integer", "description": "до 20, дефолт 5"},
                },
                "required": ["node_id"],
            },
        },
    },
    "get_node_provisioning_tasks": {
        "fn": get_node_provisioning_tasks,
        "schema": {
            "name": "get_node_provisioning_tasks",
            "description": "Последние provisioning-таски ноды (action, status, error_message). Главный источник причины несостоявшегося bootstrap/site.yml.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "integer"},
                    "limit": {"type": "integer", "description": "до 30, дефолт 10"},
                },
                "required": ["node_id"],
            },
        },
    },
}


def tool_schemas() -> list[dict[str, Any]]:
    """Список tool-схем для Claude tool-use."""
    return [entry["schema"] for entry in TOOL_REGISTRY.values()]
