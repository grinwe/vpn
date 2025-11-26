from fastapi import FastAPI
from .db import Base, engine
from . import models
from .api import router as api_router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="VPN backend")
app.include_router(api_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "vpn-backend"}
