import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, generate_latest
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from .config import get_settings
from .migrations import run_migrations
from .api import router as api_router, require_admin
from .api_extensions import ext_router
from .api_webapp import webapp_router

run_migrations()


def _check_required_settings() -> None:
    settings = get_settings()
    if not settings.admin_api_token:
        raise RuntimeError("ADMIN_API_TOKEN environment variable is required")


def reset_stuck_tasks() -> None:
    """Recover provisioning tasks from a hard restart.

    Tasks that were ``running`` when the previous process died are either
    requeued (if the RQ backend is reachable) or marked ``failed``
    (best-effort fallback). Tasks that were ``pending`` but never picked up
    are re-enqueued so they eventually execute.
    """
    from .db import SessionLocal
    from . import models
    from .queue import enqueue_task, get_queue

    queue_available = get_queue() is not None

    db = SessionLocal()
    try:
        stuck = db.query(models.ProvisioningTask).filter(
            models.ProvisioningTask.status == models.ProvisioningTaskStatus.running
        ).all()
        for task in stuck:
            if queue_available:
                task.status = models.ProvisioningTaskStatus.pending
                task.error_message = "Requeued after restart"
                enqueue_task(task.id, None)
            else:
                task.status = models.ProvisioningTaskStatus.failed
                task.error_message = "Interrupted by server restart"

        if queue_available:
            pending = db.query(models.ProvisioningTask).filter(
                models.ProvisioningTask.status == models.ProvisioningTaskStatus.pending
            ).all()
            for task in pending:
                enqueue_task(task.id, None)

        db.commit()
    finally:
        db.close()


_check_required_settings()
reset_stuck_tasks()

app = FastAPI(title="VPN backend")

# Rate limiter. Defaults are conservative blanket limits to stop abuse;
# per-route limits (login/auth, webhooks) can be declared on individual
# handlers via ``@limiter.limit(...)``. Storage is in-memory — good for
# single-process; wire to Redis via SLOWAPI_STORAGE_URI when scaling out.
_storage_uri = os.getenv("SLOWAPI_STORAGE_URI", "memory://")
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["300/minute", "60/second"],
    storage_uri=_storage_uri,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

REQUEST_COUNTER = Counter("vpn_requests_total", "Total HTTP requests", ["path", "status"])
ERROR_COUNTER = Counter("vpn_requests_errors_total", "HTTP errors", ["path", "status"])


@app.middleware("http")
async def add_metrics(request: Request, call_next):
    response = await call_next(request)
    status_code = response.status_code
    # Use the matched route template (e.g. "/api/users/{user_id}") instead of
    # the raw request path — otherwise every unique id becomes its own label
    # value and Prometheus cardinality explodes.
    route = request.scope.get("route")
    path_label = getattr(route, "path", None) or "unmatched"
    REQUEST_COUNTER.labels(path=path_label, status=str(status_code)).inc()
    if status_code >= 400:
        ERROR_COUNTER.labels(path=path_label, status=str(status_code)).inc()
    return response


app.include_router(api_router)
app.include_router(ext_router)
app.include_router(webapp_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "vpn-backend"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/metrics")
def metrics(_: str = Depends(require_admin)):
    return PlainTextResponse(generate_latest(), media_type="text/plain")
