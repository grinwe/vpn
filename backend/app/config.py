"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    admin_api_token: str | None
    admin_actor_header: str

    def __init__(self) -> None:
        self.admin_api_token = os.getenv("ADMIN_API_TOKEN")
        self.admin_actor_header = os.getenv("ADMIN_ACTOR_HEADER", "X-Admin-Actor")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
