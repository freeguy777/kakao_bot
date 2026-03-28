from __future__ import annotations

from typing import Any

from server.application.use_cases.message_events import MessageEventUseCase

_MESSAGE_EVENT_USE_CASE = MessageEventUseCase()


def handle_message_event(payload: dict[str, Any]) -> dict[str, Any]:
    return _MESSAGE_EVENT_USE_CASE.handle(payload)


__all__ = ["MessageEventUseCase", "handle_message_event"]
