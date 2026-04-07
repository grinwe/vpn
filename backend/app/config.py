"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    admin_api_token: str | None
    admin_actor_header: str
    bot_token: str | None
    webapp_jwt_secret: str | None
    webapp_jwt_ttl_seconds: int

    def __init__(self) -> None:
        self.admin_api_token = os.getenv("ADMIN_API_TOKEN")
        self.admin_actor_header = os.getenv("ADMIN_ACTOR_HEADER", "X-Admin-Actor")
        self.bot_token = os.getenv("BOT_TOKEN")
        self.webapp_jwt_secret = os.getenv("WEBAPP_JWT_SECRET")
        self.webapp_jwt_ttl_seconds = int(os.getenv("WEBAPP_JWT_TTL_SECONDS", "1800"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
