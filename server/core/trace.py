from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator
from uuid import uuid4

_TRACE_ID: ContextVar[str] = ContextVar("trace_id", default="-")


def make_trace_id() -> str:
    return uuid4().hex[:12]


def get_trace_id() -> str:
    value = _TRACE_ID.get()
    return value or "-"


@contextmanager
def trace_context(trace_id: str | None = None) -> Iterator[str]:
    resolved_trace_id = (trace_id or "").strip() or make_trace_id()
    token = _TRACE_ID.set(resolved_trace_id)
    try:
        yield resolved_trace_id
    finally:
        _TRACE_ID.reset(token)

