from __future__ import annotations

from typing import Any

SocketDeliveryError = RuntimeError


def ping_phone_socket() -> dict[str, Any]:
    return {
        "ok": False,
        "deprecated": True,
        "status": "inactive",
        "active_transport": "polling_outbox",
        "error": "socket delivery is inactive; polling_outbox is the only supported path",
    }
