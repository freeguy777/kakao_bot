from __future__ import annotations

from typing import Any

from server.application.delivery import DEFAULT_SOURCE_TYPE, deliver_room_messages, flush_pending_outbox_messages
from server.infra.socket_push import SocketPushError
from server.infra.socket_push import ping_phone_socket as _ping_phone_socket
from server.utils import make_trace_id

SocketDeliveryError = SocketPushError


def ping_phone_socket() -> dict[str, Any]:
    return _ping_phone_socket(make_trace_id())

