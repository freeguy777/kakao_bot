from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any

from server.config import get_room_policy
from server.infra.sqlite_store import get_room_target_snapshot
from server.settings import get_settings
from server.utils import now_kst

logger = logging.getLogger(__name__)


class SocketPushError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoomTarget:
    room_key: str
    room_name: str
    channel_id: str


def resolve_room_target(room_key: str) -> RoomTarget:
    room = get_room_policy(room_key)
    if room is None:
        raise ValueError(f"room config not found: {room_key}")

    snapshot = get_room_target_snapshot(room_key) or {}
    room_name = str(snapshot.get("room_name", "")).strip() or room.display_name
    channel_id = str(snapshot.get("channel_id", "")).strip() or room.channel_id
    if not room_name and not channel_id:
        raise ValueError(f"room target is empty: {room_key}")
    return RoomTarget(
        room_key=room_key,
        room_name=room_name,
        channel_id=channel_id,
    )


def _send_socket_line(line: str) -> str | None:
    settings = get_settings()
    if not settings.socket.enabled:
        raise SocketPushError("phone socket delivery is disabled")

    encoded = (line + "\n").encode("utf-8")
    address = (settings.socket.host, settings.socket.port)

    try:
        with socket.create_connection(address, timeout=settings.socket.connect_timeout_seconds) as conn:
            conn.settimeout(settings.socket.read_timeout_seconds)
            conn.sendall(encoded)
            try:
                ack = conn.recv(4096)
            except socket.timeout:
                return None
            return ack.decode("utf-8", errors="ignore").strip() or None
    except OSError as exc:
        raise SocketPushError(str(exc)) from exc


def _build_debug_room_request(inner_message: str) -> str:
    settings = get_settings()
    request = {
        "name": "debugRoom",
        "data": {
            "botName": settings.socket.bot_name,
            "authorName": settings.socket.control_author_name,
            "roomName": settings.socket.control_room_name,
            "isGroupChat": False,
            "packageName": settings.socket.package_name,
            "message": inner_message,
        },
    }
    return json.dumps(request, ensure_ascii=False)


def build_control_message(
    *,
    room_key: str,
    messages: list[str],
    trace_id: str,
    source_type: str,
    meta: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    target = resolve_room_target(room_key)
    control_payload = {
        "action": "send_messages",
        "trace_id": trace_id,
        "secret": settings.socket.shared_secret,
        "room_key": target.room_key,
        "room_name": target.room_name,
        "channel_id": target.channel_id,
        "message": messages[0] if len(messages) == 1 else None,
        "messages": messages,
        "source_type": source_type,
        "requested_at": now_kst().isoformat(),
        "meta": meta or {},
    }
    logger.info(
        "socket target resolved room_key=%s room_name=%s channel_id=%s",
        target.room_key,
        target.room_name,
        target.channel_id,
    )
    return json.dumps(control_payload, ensure_ascii=False)


def push_messages(
    *,
    room_key: str,
    messages: list[str],
    trace_id: str,
    source_type: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    control_message = build_control_message(
        room_key=room_key,
        messages=messages,
        trace_id=trace_id,
        source_type=source_type,
        meta=meta,
    )
    request_line = _build_debug_room_request(control_message)
    settings = get_settings()
    started_at = time.perf_counter()
    ack = _send_socket_line(request_line)
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "socket payload delivered trace_id=%s room_key=%s host=%s port=%s elapsed_ms=%.1f",
        trace_id,
        room_key,
        settings.socket.host,
        settings.socket.port,
        elapsed_ms,
    )
    return {
        "ok": True,
        "via": "messengerbot_builtin_socket",
        "trace_id": trace_id,
        "room_key": room_key,
        "messages": messages,
        "ack": ack,
        "error": None,
    }


def ping_phone_socket(trace_id: str) -> dict[str, Any]:
    settings = get_settings()
    control_message = json.dumps(
        {
            "action": "ping",
            "trace_id": trace_id,
            "secret": settings.socket.shared_secret,
            "requested_at": now_kst().isoformat(),
        },
        ensure_ascii=False,
    )
    try:
        ack = _send_socket_line(_build_debug_room_request(control_message))
        return {
            "ok": True,
            "trace_id": trace_id,
            "host": settings.socket.host,
            "port": settings.socket.port,
            "ack": ack,
            "error": None,
        }
    except Exception as exc:
        logger.warning("socket ping failed trace_id=%s error=%s", trace_id, exc)
        return {
            "ok": False,
            "trace_id": trace_id,
            "host": settings.socket.host,
            "port": settings.socket.port,
            "ack": None,
            "error": str(exc),
        }
