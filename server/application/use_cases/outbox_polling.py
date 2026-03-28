from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from server.core.contracts import OutboxAckRequest, OutboxPullRequest, build_standard_response
from server.infra.sqlite_store import ack_outbox_messages, pull_pending_outbox_messages
from server.utils import make_trace_id


@dataclass(slots=True)
class OutboxPollingUseCase:
    puller: Callable[[str | None, str | None, int], list[dict[str, Any]]] = pull_pending_outbox_messages
    acker: Callable[[list[Any], bool, bool], int] = ack_outbox_messages
    trace_id_factory: Callable[[], str] = make_trace_id

    def pull(
        self,
        payload: OutboxPullRequest | dict[str, Any],
        *,
        action: str = "polling.outbox.pull",
    ) -> dict[str, Any]:
        request = payload if isinstance(payload, OutboxPullRequest) else OutboxPullRequest.model_validate(payload)
        trace_id = self.trace_id_factory()
        items = self.puller(request.room_key, request.channel_id, request.limit)
        return build_standard_response(
            ok=True,
            trace_id=trace_id,
            action=action,
            messages=[],
            error=None,
            meta={"count": len(items), "items": items},
        )

    def ack(
        self,
        payload: OutboxAckRequest | dict[str, Any],
        *,
        action: str = "polling.outbox.ack",
    ) -> dict[str, Any]:
        request = payload if isinstance(payload, OutboxAckRequest) else OutboxAckRequest.model_validate(payload)
        trace_id = self.trace_id_factory()
        increment_retry = request.increment_retry if request.increment_retry is not None else (not request.success)
        updated = self.acker(request.message_ids, request.success, increment_retry)
        return build_standard_response(
            ok=True,
            trace_id=trace_id,
            action=action,
            messages=["처리 완료"],
            error=None,
            meta={
                "updated_count": updated,
                "success": request.success,
            },
        )
