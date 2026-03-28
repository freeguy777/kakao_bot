from __future__ import annotations

import logging

from server.core.trace import get_trace_id

_CONFIGURED = False


class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True


def _ensure_filter(logger: logging.Logger) -> None:
    has_filter = any(isinstance(current, TraceIdFilter) for current in logger.filters)
    if not has_filter:
        logger.addFilter(TraceIdFilter())

    for handler in logger.handlers:
        handler_has_filter = any(isinstance(current, TraceIdFilter) for current in handler.filters)
        if not handler_has_filter:
            handler.addFilter(TraceIdFilter())


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] trace_id=%(trace_id)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not _CONFIGURED:
        logging.basicConfig(level=level, format=formatter._fmt, datefmt=formatter.datefmt)  # type: ignore[attr-defined]
        _CONFIGURED = True

    target_loggers = (
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("fastapi"),
    )
    for target_logger in target_loggers:
        _ensure_filter(target_logger)
        for handler in target_logger.handlers:
            handler.setFormatter(formatter)

