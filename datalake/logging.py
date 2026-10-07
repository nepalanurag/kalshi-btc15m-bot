"""Structured logging for the data lake.

One configuration point; every module does::

    from datalake.logging import get_logger
    log = get_logger(__name__)

JSON goes to stderr by default (``DATALAKE_LOG_FORMAT=json``) so log
aggregators can parse it; ``console`` renders human-readable lines for
interactive runs.
"""

from __future__ import annotations

import logging
import sys

import structlog

_configured = False


def configure_logging(log_format: str = "json", level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO), stream=sys.stderr, force=True
    )
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
