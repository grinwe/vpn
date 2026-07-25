import logging
import os
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import Counter, generate_latest
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .config import get_settings
from .logging_config import configure_logging, request_id_var
from .migrations import run_migrations
from .rate_limit import limiter
from .services.provisioning_throttle import ColdPathThrottled
from .api import router as api_router
from .api_extensions import ext_router
from .api_webapp import webapp_router
from .telegram_webhook import router as tg_webhook_router, register_webhook

configure_logging()
run_migrations()

logger = logging.getLogger(__name__)


def _check_required_settings() -> None:
    settings = get_settings()
    if not settings.admin_api_token:
        raise RuntimeError("ADMIN_API_TOKEN environment variable is required")


def reset_stuck_tasks() -> dict[str, int | bool]:
    """Recover provisioning tasks from a hard restart.

    Tasks that were ``running`` when the previous process died are either
    requeued (if the RQ backend is reachable) or marked ``failed``
    (best-effort fallback). Tasks that were ``pending`` but never picked up
    are re-enqueued so they eventually execute.

    Returns a small summary dict so the admin reset-stuck endpoint can
    surface what happened; the startup caller discards the value.
    """
    from .db import SessionLocal
    from . import models
    from .queue import enqueue_task, get_queue

    queue_available = get_queue() is not None
    summary: dict[str, int | bool] = {
        "queue_available": queue_available,
        "requeued": 0,
        "failed": 0,
        "pending_requeued": 0,
    }

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
                summary["requeued"] += 1
            else:
                task.status = models.ProvisioningTaskStatus.failed
                task.error_message = "Interrupted by server restart"
                summary["failed"] += 1

        if queue_available:
            pending = db.query(models.ProvisioningTask).filter(
                models.ProvisioningTask.status == models.ProvisioningTaskStatus.pending
            ).all()
            for task in pending:
                enqueue_task(task.id, None)
                summary["pending_requeued"] += 1

        db.commit()
    finally:
        db.close()
    return summary


_check_required_settings()
reset_stuck_tasks()

_is_prod = os.getenv("APP_ENV", "dev").lower() == "production"
app = FastAPI(
    title="VPN backend",
    docs_url=None if _is_prod else "/api/docs",
    redoc_url=None if _is_prod else "/api/redoc",
    openapi_url=None if _is_prod else "/api/openapi.json",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(ColdPathThrottled)
async def _cold_path_throttled_handler(request: Request, exc: ColdPathThrottled):
    # 503 + Retry-After so well-behaved clients back off instead of
    # retrying immediately. Surfaces as a banner in the webapp and a
    # "подожди N секунд" toast in the bot.
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Слишком много активаций подряд, попробуй через несколько секунд.",
            "retry_after_seconds": exc.retry_after_seconds,
        },
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    # Ловим необработанные исключения (реальные 500-краши): логируем трейс и
    # возвращаем клиенту request_id, по которому его можно найти в логах.
    # ServerErrorMiddleware — самый внешний слой, поэтому этот ответ НЕ проходит
    # обратно через add_request_id, и заголовок надо проставить здесь вручную.
    # Метрику 500 при этом инкрементит add_metrics на пути исключения.
    rid = request_id_var.get() or ""
    logger.exception("unhandled_exception", extra={"path": request.url.path})
    return JSONResponse(
        status_code=500,
        content={"detail": "Внутренняя ошибка сервера.", "request_id": rid},
        headers={"X-Request-ID": rid} if rid else None,
    )

# CORS — restrict to explicit origins. WEBAPP_ORIGIN env controls which
# frontend domains may call the API. Falls back to same-origin only (empty
# list = no cross-origin requests allowed).
_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

REQUEST_COUNTER = Counter("vpn_requests_total", "Total HTTP requests", ["path", "status"])
ERROR_COUNTER = Counter("vpn_requests_errors_total", "HTTP errors", ["path", "status"])


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request_id_var.set(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


def _metrics_path_label(request: Request) -> str:
    # Use the matched route template (e.g. "/api/users/{user_id}") instead of
    # the raw request path — otherwise every unique id becomes its own label
    # value and Prometheus cardinality explodes. request.scope["route"] is set
    # by the router even when the endpoint later raises, so this stays valid on
    # the exception path too.
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


@app.middleware("http")
async def add_metrics(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        # Необработанное исключение = настоящий 500-краш. Считаем его в обоих
        # счётчиках, иначе всплеск багов после деплоя не виден на дашбордах, и
        # пере-бросываем — финальный ответ строит exception-handler ниже.
        path_label = _metrics_path_label(request)
        REQUEST_COUNTER.labels(path=path_label, status="500").inc()
        ERROR_COUNTER.labels(path=path_label, status="500").inc()
        raise
    status_code = response.status_code
    path_label = _metrics_path_label(request)
    REQUEST_COUNTER.labels(path=path_label, status=str(status_code)).inc()
    if status_code >= 400:
        ERROR_COUNTER.labels(path=path_label, status=str(status_code)).inc()
    return response


app.include_router(api_router)
app.include_router(ext_router)
app.include_router(webapp_router)
app.include_router(tg_webhook_router)


@app.on_event("startup")
def _startup_assert_secrets_configured():
    """Падаем на старте, если нет APP_SECRET_KEY (аудит 2026-07-25).

    Раньше отсутствие ключа означало тихую запись секретов в БД открытым
    текстом — контейнер поднимался, эндпоинты отвечали, и заметить мисконфиг
    было нечем, кроме одной warning-строки в логе.
    """
    from .security import assert_secrets_configured

    assert_secrets_configured()


@app.on_event("startup")
def _startup_register_telegram_webhook():
    register_webhook()


@app.get("/", include_in_schema=False)
def root():
    raise HTTPException(status_code=404)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    # No auth — backend binds to 127.0.0.1:8000, Prometheus scrapes via
    # host.docker.internal from the same host. Labels are route templates
    # (no PII), counters are aggregates. If binding ever opens to 0.0.0.0,
    # re-add require_admin or restrict /metrics at nginx.
    return PlainTextResponse(generate_latest(), media_type="text/plain")
