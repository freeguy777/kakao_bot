from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from server.core.contracts import OutboxAckRequest, OutboxPullRequest, build_standard_response
from server.infra.sqlite_store import ack_outbox_messages, count_outbox_messages, pull_pending_outbox_messages
from server.utils import make_trace_id, now_kst

ACTIVE_TRANSPORT = "polling_outbox"
DEFAULT_POLLING_INTERVAL_MS = 15000
_POLLING_STATUS: dict[str, Any] = {
    "active_transport": ACTIVE_TRANSPORT,
    "polling_enabled": True,
    "polling_interval_ms": DEFAULT_POLLING_INTERVAL_MS,
    "last_pull_at": None,
    "last_success_at": None,
    "last_ack_success_count": 0,
    "last_ack_fail_count": 0,
}


def reset_polling_status() -> None:
    _POLLING_STATUS.update(
        {
            "active_transport": ACTIVE_TRANSPORT,
            "polling_enabled": True,
            "polling_interval_ms": DEFAULT_POLLING_INTERVAL_MS,
            "last_pull_at": None,
            "last_success_at": None,
            "last_ack_success_count": 0,
            "last_ack_fail_count": 0,
        }
    )


def get_polling_status_snapshot(
    *,
    outbox_counter: Callable[[str | None], int] = count_outbox_messages,
) -> dict[str, Any]:
    try:
        pending_outbox_count = outbox_counter("pending")
        inflight_outbox_count = outbox_counter("inflight")
    except Exception:
        pending_outbox_count = 0
        inflight_outbox_count = 0
    snapshot = dict(_POLLING_STATUS)
    snapshot["pending_outbox_count"] = pending_outbox_count
    snapshot["inflight_outbox_count"] = inflight_outbox_count
    return snapshot


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
        checked_at = now_kst().isoformat()
        _POLLING_STATUS["last_pull_at"] = checked_at
        items = self.puller(request.room_key, request.channel_id, request.limit)
        _POLLING_STATUS["last_success_at"] = checked_at
        status = get_polling_status_snapshot()
        return build_standard_response(
            ok=True,
            trace_id=trace_id,
            action=action,
            messages=[],
            error=None,
            meta={
                "active_transport": ACTIVE_TRANSPORT,
                "count": len(items),
                "items": items,
                **status,
            },
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
        checked_at = now_kst().isoformat()
        if request.success:
            _POLLING_STATUS["last_ack_success_count"] = updated
        else:
            _POLLING_STATUS["last_ack_fail_count"] = updated
        _POLLING_STATUS["last_success_at"] = checked_at
        status = get_polling_status_snapshot()
        return build_standard_response(
            ok=True,
            trace_id=trace_id,
            action=action,
            messages=["처리 완료"],
            error=None,
            meta={
                "active_transport": ACTIVE_TRANSPORT,
                "updated_count": updated,
                "success": request.success,
                **status,
            },
        )
