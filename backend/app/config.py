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
    webapp_init_data_ttl_seconds: int

    def __init__(self) -> None:
        self.admin_api_token = os.getenv("ADMIN_API_TOKEN")
        self.admin_actor_header = os.getenv("ADMIN_ACTOR_HEADER", "X-Admin-Actor")
        self.bot_token = os.getenv("BOT_TOKEN")
        self.webapp_jwt_secret = os.getenv("WEBAPP_JWT_SECRET")
        self.webapp_jwt_ttl_seconds = int(os.getenv("WEBAPP_JWT_TTL_SECONDS", "1800"))
        # TTL for the Telegram WebApp initData handshake. Short by
        # design: the frontend exchanges initData for a session JWT
        # within seconds of the Mini App opening, so a tight TTL bounds
        # the replay window. 300s matches Telegram's recommended
        # validation window for initData. Do NOT confuse with
        # webapp_jwt_ttl_seconds, which is the session-token lifetime.
        self.webapp_init_data_ttl_seconds = int(
            os.getenv("WEBAPP_INIT_DATA_TTL_SECONDS", "300")
        )
        # #62 — Telegram native webhook mode.  When both
        # TELEGRAM_WEBHOOK_SECRET_TOKEN and TELEGRAM_WEBHOOK_URL are set,
        # the backend registers itself as the Bot API webhook receiver on
        # startup and processes Stars payments directly — eliminating the
        # shared-secret forwarding through the bot.
        self.telegram_webhook_secret_token: str | None = os.getenv(
            "TELEGRAM_WEBHOOK_SECRET_TOKEN"
        )
        self.telegram_webhook_url: str | None = os.getenv("TELEGRAM_WEBHOOK_URL")
        self.bot_internal_webhook_url: str | None = os.getenv(
            "BOT_INTERNAL_WEBHOOK_URL"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
