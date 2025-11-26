from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import Counter, generate_latest

from .migrations import run_migrations
from .api import router as api_router

run_migrations()

app = FastAPI(title="VPN backend")

REQUEST_COUNTER = Counter("vpn_requests_total", "Total HTTP requests", ["path"])


@app.middleware("http")
async def add_metrics(request: Request, call_next):
    response = await call_next(request)
    REQUEST_COUNTER.labels(path=request.url.path).inc()
    return response


app.include_router(api_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "vpn-backend"}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type="text/plain")
