from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from server.application.delivery import deliver_room_messages, flush_pending_outbox_messages
from server.core.contracts import SocketFlushRequest, SocketSendRequest, build_standard_response
from server.utils import make_trace_id


@dataclass(frozen=True, slots=True)
class UseCaseHttpResult:
    status_code: int
    payload: dict[str, Any]


@dataclass(slots=True)
class DeliveryDispatchUseCase:
    deliverer: Callable[..., dict[str, Any]] = deliver_room_messages
    flusher: Callable[[int | None], dict[str, Any]] = flush_pending_outbox_messages
    trace_id_factory: Callable[[], str] = make_trace_id

    def send(self, payload: SocketSendRequest | dict[str, Any]) -> UseCaseHttpResult:
        request = payload if isinstance(payload, SocketSendRequest) else SocketSendRequest.model_validate(payload)
        trace_id = request.trace_id or self.trace_id_factory()
        if not request.room_key:
            return UseCaseHttpResult(
                status_code=400,
                payload=build_standard_response(
                    ok=False,
                    trace_id=trace_id,
                    action="polling.outbox.enqueue",
                    messages=[],
                    error="room_key is required",
                    meta={},
                ),
            )

        result = self.deliverer(
            room_key=request.room_key,
            messages=request.messages,
            message=request.message,
            trace_id=trace_id,
            source_type=request.source_type,
            meta=request.meta,
            dedupe_key=request.dedupe_key,
        )
        accepted = bool(result.get("queued")) or bool(result.get("delivered")) or bool(result.get("outbox_ids")) or (
            result.get("transport") == "polling" and result.get("via") == "dedupe_skip"
        )
        if result.get("transport") == "polling" and result.get("via") == "dedupe_skip":
            messages = ["duplicate delivery skipped"]
        elif result.get("transport") == "polling" and result.get("queued"):
            messages = ["delivery queued for polling"]
        elif result.get("transport") == "polling" and result.get("delivered"):
            messages = ["delivery completed"]
        elif result.get("outbox_ids"):
            messages = ["delivery queued"]
        else:
            messages = []

        return UseCaseHttpResult(
            status_code=200 if accepted else 503,
            payload=build_standard_response(
                ok=accepted,
                trace_id=trace_id,
                action="polling.outbox.enqueue",
                messages=messages,
                error=result.get("error") if not accepted else None,
                meta=result,
            ),
        )

    def flush(self, payload: SocketFlushRequest | dict[str, Any] | None = None) -> dict[str, Any]:
        request = payload if isinstance(payload, SocketFlushRequest) else SocketFlushRequest.model_validate(payload or {})
        trace_id = request.trace_id or self.trace_id_factory()
        result = self.flusher(request.limit)
        return build_standard_response(
            ok=True,
            trace_id=trace_id,
            action="polling.outbox.flush",
            messages=["direct flush is deprecated; polling clients should use /polling/pull and /polling/ack"],
            error=None,
            meta=result,
        )
