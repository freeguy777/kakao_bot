from __future__ import annotations

import hashlib
import logging
from typing import Any

from server.config import get_admin_room_key, get_room_policy
from server.infra.sqlite_store import (
    count_outbox_messages,
    enqueue_outbox_message,
    register_admin_alert_attempt,
    register_delivery_dedupe,
)
from server.utils import make_trace_id

logger = logging.getLogger(__name__)
DEFAULT_SOURCE_TYPE = "server_push"
DEFAULT_DELIVERY_VIA = "polling_outbox"
DEFAULT_DELIVERY_TRANSPORT = "polling"


def _build_delivery_result(
    *,
    ok: bool,
    via: str,
    trace_id: str,
    room_key: str,
    messages: list[str],
    outbox_ids: list[int] | None = None,
    error: str | None = None,
    queued: bool = False,
    delivered: bool = False,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "transport": DEFAULT_DELIVERY_TRANSPORT,
        "via": via,
        "queued": queued,
        "delivered": delivered,
        "trace_id": trace_id,
        "room_key": room_key,
        "messages": messages,
        "ack": None,
        "outbox_ids": list(outbox_ids or []),
        "error": error,
    }


def _normalize_messages(raw_messages: Any, raw_message: Any | None = None) -> list[str]:
    normalized: list[str] = []
    if isinstance(raw_messages, list):
        for item in raw_messages:
            text = str(item).strip()
            if text:
                normalized.append(text)
    elif raw_message is not None:
        text = str(raw_message).strip()
        if text:
            normalized.append(text)
    return normalized


def _split_message_by_limit(text: str, limit: int) -> list[str]:
    normalized = str(text).strip()
    if not normalized:
        return []
    if len(normalized) <= limit:
        return [normalized]
    chunks: list[str] = []
    paragraphs = [paragraph.strip() for paragraph in normalized.splitlines() if paragraph.strip()]
    if not paragraphs:
        paragraphs = [normalized]

    current = ""
    for paragraph in paragraphs:
        remaining = paragraph
        while remaining:
            if not current:
                if len(remaining) <= limit:
                    current = remaining
                    remaining = ""
                    continue
                cut = _find_split_index(remaining, limit)
                chunks.append(remaining[:cut].rstrip())
                remaining = remaining[cut:].lstrip()
                continue

            separator = "\n"
            candidate = f"{current}{separator}{remaining}"
            if len(candidate) <= limit:
                current = candidate
                remaining = ""
                continue

            available = limit - len(current) - len(separator)
            if available <= 0:
                chunks.append(current.rstrip())
                current = ""
                continue

            cut = _find_split_index(remaining, available)
            fitted = remaining[:cut].rstrip()
            if fitted:
                current = f"{current}{separator}{fitted}"
                chunks.append(current.rstrip())
                current = ""
                remaining = remaining[cut:].lstrip()
                continue

            chunks.append(current.rstrip())
            current = ""

    if current:
        chunks.append(current.rstrip())
    return [chunk for chunk in chunks if chunk]


def _find_split_index(text: str, limit: int) -> int:
    search_window = text[:limit]
    min_index = max(1, int(limit * 0.6))
    for marker in ["\n", ". ", "! ", "? ", "다. ", "요. ", ", ", " "]:
        cut = search_window.rfind(marker)
        if cut >= min_index:
            return cut + (0 if marker == "\n" else len(marker.strip()))
    return limit


def _expand_messages_for_room(room_key: str, raw_messages: list[str]) -> list[str]:
    room = get_room_policy(room_key)
    limit = room.delivery.message_length_limit if room else 3000
    expanded: list[str] = []
    for message in raw_messages:
        expanded.extend(_split_message_by_limit(message, limit))
    return expanded


def build_delivery_dedupe_key(
    *,
    room_key: str,
    source_type: str,
    messages: list[str],
    hint: str | None = None,
) -> str:
    digest = hashlib.sha256(
        "\n".join([room_key, source_type, hint or "", *messages]).encode("utf-8")
    ).hexdigest()
    return digest


def deliver_room_messages(
    *,
    room_key: str,
    messages: list[str] | None = None,
    message: str | None = None,
    trace_id: str | None = None,
    source_type: str = DEFAULT_SOURCE_TYPE,
    meta: dict[str, Any] | None = None,
    allow_fallback: bool = True,
    dedupe_key: str | None = None,
    dedupe_ttl_seconds_override: int | None = None,
) -> dict[str, Any]:
    normalized_messages = _expand_messages_for_room(room_key, _normalize_messages(messages, message))
    if not normalized_messages:
        raise ValueError("delivery messages are empty")

    room = get_room_policy(room_key)
    resolved_trace_id = (trace_id or make_trace_id()).strip()
    dedupe_ttl_seconds = room.delivery.dedupe_ttl_seconds if room else 600
    if dedupe_ttl_seconds_override is not None:
        dedupe_ttl_seconds = max(dedupe_ttl_seconds, int(dedupe_ttl_seconds_override))
    resolved_dedupe_key = dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None
    if resolved_dedupe_key and not register_delivery_dedupe(
        dedupe_key=resolved_dedupe_key,
        room_key=room_key,
        source_type=source_type,
        trace_id=resolved_trace_id,
        ttl_seconds=dedupe_ttl_seconds,
    ):
        return _build_delivery_result(
            ok=True,
            via="dedupe_skip",
            trace_id=resolved_trace_id,
            room_key=room_key,
            messages=normalized_messages,
            outbox_ids=[],
            error=None,
            queued=False,
            delivered=False,
        )

    try:
        outbox_ids: list[int] = []
        for item in normalized_messages:
            outbox_ids.append(
                enqueue_outbox_message(
                    room_key=room_key,
                    message_text=item,
                    source_type=source_type,
                    trace_id=resolved_trace_id,
                    meta={
                        "delivery_mode": DEFAULT_DELIVERY_VIA,
                        "transport": DEFAULT_DELIVERY_TRANSPORT,
                        "original_meta": meta or {},
                        "dedupe_key": resolved_dedupe_key,
                        "allow_fallback": bool(allow_fallback),
                    },
                )
            )
        return _build_delivery_result(
            ok=True,
            via=DEFAULT_DELIVERY_VIA,
            trace_id=resolved_trace_id,
            room_key=room_key,
            messages=normalized_messages,
            outbox_ids=outbox_ids,
            error=None,
            queued=True,
            delivered=False,
        )
    except Exception as exc:
        logger.exception(
            "polling outbox enqueue failed room_key=%s trace_id=%s source_type=%s",
            room_key,
            resolved_trace_id,
            source_type,
            exc_info=exc,
        )
        return _build_delivery_result(
            ok=False,
            via="error",
            trace_id=resolved_trace_id,
            room_key=room_key,
            messages=normalized_messages,
            outbox_ids=[],
            error=str(exc),
            queued=False,
            delivered=False,
        )


def flush_pending_outbox_messages(limit: int | None = None) -> dict[str, Any]:
    return {
        "pulled": 0,
        "sent": 0,
        "failed": 0,
        "errors": [],
        "limit": limit,
        "delivery_mode": DEFAULT_DELIVERY_VIA,
        "pending": count_outbox_messages("pending"),
        "inflight": count_outbox_messages("inflight"),
        "sent_total": count_outbox_messages("sent"),
    }


def notify_admin_error(
    *,
    room_key: str,
    feature_key: str,
    trace_id: str,
    detail: str,
    meta: dict[str, Any] | None = None,
) -> None:
    room = get_room_policy(room_key)
    if room is not None and not room.delivery.notify_admin_on_error:
        return
    throttle_seconds = room.delivery.admin_alert_throttle_seconds if room is not None else 300
    throttle_decision = register_admin_alert_attempt(
        room_key=room_key,
        feature_key=feature_key,
        throttle_seconds=throttle_seconds,
    )
    if not throttle_decision.should_send:
        logger.info(
            "admin notification suppressed room_key=%s feature=%s trace_id=%s suppressed_count=%s",
            room_key,
            feature_key,
            trace_id,
            throttle_decision.suppressed_count,
        )
        return
    admin_room_key = get_admin_room_key(room_key)
    if not admin_room_key:
        logger.warning(
            "admin notification skipped feature=%s room_key=%s trace_id=%s reason=no_admin_room",
            feature_key,
            room_key,
            trace_id,
        )
        return

    message = (
        "[관리자 알림]\n"
        f"feature: {feature_key}\n"
        f"room_key: {room_key}\n"
        f"trace_id: {trace_id}\n"
        f"detail: {detail}"
    )
    if throttle_decision.suppressed_count > 0:
        message += f"\nsuppressed_since_last_alert: {throttle_decision.suppressed_count}"
    try:
        deliver_room_messages(
            room_key=admin_room_key,
            message=message,
            trace_id=trace_id,
            source_type="admin_alert",
            meta=meta or {},
            allow_fallback=True,
            dedupe_key=build_delivery_dedupe_key(
                room_key=admin_room_key,
                source_type="admin_alert",
                messages=[feature_key, room_key, trace_id, detail],
                hint="admin_alert",
            ),
        )
    except Exception as exc:
        logger.warning(
            "admin notification failed admin_room_key=%s feature=%s trace_id=%s error=%s",
            admin_room_key,
            feature_key,
            trace_id,
            exc,
        )
