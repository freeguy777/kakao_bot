# Run:
#   uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
# Health check:
#   curl http://127.0.0.1:8000/kakao/health

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.config import get_api_base_url, reload_settings
from server.core.logging import configure_logging
from server.core.trace import trace_context
from server.db import init_db, reset_inflight_outbox_messages
from server.presentation.http import router
from server.presentation.http.responses import build_json_response
from server.scheduler import create_scheduler, register_jobs, shutdown_scheduler
from server.utils import make_trace_id

configure_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = reload_settings()
    init_db(settings.sqlite_path)
    reset_inflight_outbox_messages()
    scheduler = create_scheduler(settings.timezone)
    register_jobs(scheduler, settings)
    app.state.scheduler = scheduler
    logger.info("application started base_url=%s active_transport=%s", get_api_base_url(), "polling_outbox")
    try:
        yield
    finally:
        shutdown_scheduler(getattr(app.state, "scheduler", None))
        logger.info("application stopped")


app = FastAPI(
    title="Kakao Bot FastAPI",
    version="0.3.0",
    lifespan=lifespan,
    default_response_class=JSONResponse,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    trace_id = make_trace_id()
    with trace_context(trace_id):
        logger.exception("unhandled exception", exc_info=exc)
    return build_json_response(
        status_code=500,
        ok=False,
        trace_id=trace_id,
        action="error",
        messages=[],
        error="서버 내부 오류가 발생했습니다.",
        meta={"detail": str(exc)},
    )


app.include_router(router)
