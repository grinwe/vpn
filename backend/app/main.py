from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, generate_latest

from .config import get_settings
from .migrations import run_migrations
from .api import router as api_router

run_migrations()


def _check_required_settings() -> None:
    settings = get_settings()
    if not settings.admin_api_token:
        raise RuntimeError("ADMIN_API_TOKEN environment variable is required")


def reset_stuck_tasks() -> None:
    """Reset any tasks stuck in 'running' status back to 'failed' on startup."""
    from .db import SessionLocal
    from . import models

    db = SessionLocal()
    try:
        stuck = db.query(models.ProvisioningTask).filter(
            models.ProvisioningTask.status == models.ProvisioningTaskStatus.running
        ).all()
        for task in stuck:
            task.status = models.ProvisioningTaskStatus.failed
            task.error_message = "Interrupted by server restart"
        db.commit()
    finally:
        db.close()


_check_required_settings()
reset_stuck_tasks()

app = FastAPI(title="VPN backend")

REQUEST_COUNTER = Counter("vpn_requests_total", "Total HTTP requests", ["path", "status"])
ERROR_COUNTER = Counter("vpn_requests_errors_total", "HTTP errors", ["path", "status"])


@app.middleware("http")
async def add_metrics(request: Request, call_next):
    response = await call_next(request)
    status_code = response.status_code
    REQUEST_COUNTER.labels(path=request.url.path, status=str(status_code)).inc()
    if status_code >= 400:
        ERROR_COUNTER.labels(path=request.url.path, status=str(status_code)).inc()
    return response


app.include_router(api_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "vpn-backend"}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type="text/plain")
