"""Health probe ingestion + target discovery: ``/api/nodes/{id}/probes`` and ``/api/probes/targets``.

External probe rigs (ru-mts, kz, eu...) pull ``/probes/targets`` on a
schedule, run connectivity checks against each endpoint, and POST the
outcome back to ``/nodes/{id}/probes``. Scoped tokens gate both — read
for the target list, write for observations.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import models, schemas
from ..auth import (
    SCOPE_PROBE_READ,
    SCOPE_PROBE_WRITE,
    AuthPrincipal,
    require_scope,
)
from ..rate_limit import limiter
from ..services.health import record_probe, recompute_node_health
from ..time_utils import utcnow
from ._common import get_db

router = APIRouter()


@router.post("/nodes/{node_id}/probes", response_model=schemas.NodeHealthOut)
@limiter.limit("120/minute")
def submit_probe(
    node_id: int,
    payload: schemas.HealthProbeIn,
    request: Request,
    db=Depends(get_db),
    principal: AuthPrincipal = Depends(require_scope(SCOPE_PROBE_WRITE)),
):
    """Accept a probe observation and recompute node health.

    Intended to be called by external probers (active check rigs in different
    regions) or by a passive ingestor that aggregates client-side telemetry.
    Requires the admin token so that random actors cannot poison health data.
    """
    node = db.get(models.VPNNode, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        result = models.ProbeResult(payload.result)
    except ValueError as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid probe result") from exc

    record_probe(
        db,
        node=node,
        source_region=payload.source_region,
        result=result,
        latency_ms=payload.latency_ms,
        source_kind=payload.source_kind,
        details=payload.details,
    )
    summary = recompute_node_health(db, node)
    return schemas.NodeHealthOut(**summary)


@router.get("/probes/targets", response_model=schemas.ProbeTargetList)
def list_probe_targets(
    db=Depends(get_db),
    principal: AuthPrincipal = Depends(require_scope(SCOPE_PROBE_READ)),
):
    """Return the list of node endpoints that probe agents should check.

    Called by out-of-band probe rigs in each source region (ru-mts, kz, eu…)
    on a schedule. The agent pulls this list, runs its checks, and POSTs
    the outcome of each endpoint back to ``/nodes/{id}/probes``.

    Only nodes that are active and past the ``registering`` stage are
    included — a half-provisioned node returning ``refused`` from a probe
    rig would otherwise cook its health score before real traffic ever
    reached it.
    """
    nodes = (
        db.query(models.VPNNode)
        .filter(
            models.VPNNode.is_active.is_(True),
            models.VPNNode.status != models.VPNNodeStatus.registering,
        )
        .all()
    )

    targets: list[schemas.ProbeTarget] = []
    for node in nodes:
        endpoints: list[schemas.ProbeTargetEndpoint] = []
        for cfg in node.configs:
            if not cfg.is_enabled:
                continue
            kind = "tls" if cfg.sni else "tcp"
            endpoints.append(
                schemas.ProbeTargetEndpoint(
                    protocol=cfg.protocol.value if hasattr(cfg.protocol, "value") else str(cfg.protocol),
                    port=cfg.port,
                    kind=kind,
                    sni=cfg.sni,
                )
            )
        if not endpoints:
            continue
        targets.append(
            schemas.ProbeTarget(
                node_id=node.id,
                name=node.name,
                region=node.region,
                host=node.host,
                endpoints=endpoints,
            )
        )

    return schemas.ProbeTargetList(generated_at=utcnow(), targets=targets)
