from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class ApiResponse(BaseModel):
    ok: bool
    trace_id: str
    action: str
    messages: list[str] = Field(default_factory=list)
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


def build_standard_response(
    *,
    ok: bool,
    trace_id: str,
    action: str,
    messages: list[str],
    error: str | None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ApiResponse(
        ok=ok,
        trace_id=trace_id,
        action=action,
        messages=messages,
        error=error,
        meta=meta or {},
    ).model_dump()


class MessageEventRequest(BaseModel):
    room_name: str | None = None
    channel_id: str | None = None
    sender: str | None = None
    message: str = ""
    log_id: str | None = None
    package_name: str | None = None
    user_hash: str | None = None
    is_group_chat: bool = False
    received_at: str | None = None

    _normalize_room_name = field_validator("room_name", mode="before")(_strip_or_none)
    _normalize_channel_id = field_validator("channel_id", mode="before")(_strip_or_none)
    _normalize_sender = field_validator("sender", mode="before")(_strip_or_none)
    _normalize_log_id = field_validator("log_id", mode="before")(_strip_or_none)
    _normalize_package_name = field_validator("package_name", mode="before")(_strip_or_none)
    _normalize_user_hash = field_validator("user_hash", mode="before")(_strip_or_none)
    _normalize_received_at = field_validator("received_at", mode="before")(_strip_or_none)

    @field_validator("message", mode="before")
    @classmethod
    def _normalize_message(cls, value: Any) -> str:
        return str(value or "").strip()


class SocketSendRequest(BaseModel):
    room_key: str | None = None
    messages: list[str] | None = None
    message: str | None = None
    trace_id: str | None = None
    source_type: str = "manual"
    meta: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = None

    _normalize_room_key = field_validator("room_key", mode="before")(_strip_or_none)
    _normalize_message = field_validator("message", mode="before")(_strip_or_none)
    _normalize_trace_id = field_validator("trace_id", mode="before")(_strip_or_none)
    _normalize_source_type = field_validator("source_type", mode="before")(
        lambda value: str(value or "manual").strip() or "manual"
    )
    _normalize_dedupe_key = field_validator("dedupe_key", mode="before")(_strip_or_none)


class SocketFlushRequest(BaseModel):
    trace_id: str | None = None
    limit: int | None = None

    _normalize_trace_id = field_validator("trace_id", mode="before")(_strip_or_none)


class OutboxPullRequest(BaseModel):
    room_key: str | None = None
    channel_id: str | None = None
    limit: int = 10

    _normalize_room_key = field_validator("room_key", mode="before")(_strip_or_none)
    _normalize_channel_id = field_validator("channel_id", mode="before")(_strip_or_none)


class OutboxAckRequest(BaseModel):
    message_ids: list[int] = Field(default_factory=list)
    success: bool = True
    increment_retry: bool | None = None

