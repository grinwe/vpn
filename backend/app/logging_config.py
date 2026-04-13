"""Structured logging configuration.

Wraps stdlib logging with structlog so that *all* existing
``logging.getLogger(__name__)`` calls produce structured JSON output in
production and human-readable coloured output in dev.

Import ``configure_logging()`` early — before any logger is used.
The request_id contextvar is set per-request by the middleware in main.py
and automatically injected into every log line.
"""
from __future__ import annotations

import logging
import logging.config
import os
from contextvars import ContextVar

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def _add_request_id(
    logger: logging.Logger,  # noqa: ARG001
    method_name: str,  # noqa: ARG001
    event_dict: dict,
) -> dict:
    rid = request_id_var.get()
    if rid is not None:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    json_logs = os.getenv("LOG_FORMAT", "json").lower() == "json"

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_request_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_logs:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()  # type: ignore[assignment]

    # Configure structlog itself (for code that uses structlog.get_logger())
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Wrap stdlib logging so existing getLogger() calls also go through
    # structlog's pipeline and produce the same output format.
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "structlog": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": [
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        renderer,
                    ],
                    "foreign_pre_chain": shared_processors,
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "structlog",
                },
            },
            "root": {
                "handlers": ["default"],
                "level": log_level,
            },
            # Quieten noisy third-party loggers
            "loggers": {
                "uvicorn": {"level": "WARNING"},
                "uvicorn.access": {"level": "WARNING"},
                "sqlalchemy.engine": {"level": "WARNING"},
            },
        }
    )
