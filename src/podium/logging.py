"""structlog setup — one call at startup. JSON in prod, console renderer for local readability."""

from __future__ import annotations

import logging

import structlog


def configure_logging(*, json: bool = True) -> None:
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
