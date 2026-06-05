from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.schemas import (
    DeliveryAckPayload,
    DeliveryResult,
    InboundWebhookPayload,
    NormalizedInboundEvent,
    PollingAckRequest,
    PollingPullRequest,
)

router = APIRouter(prefix="/kakao", tags=["kakao"])
logger = logging.getLogger(__name__)


@router.post("/webhook")
async def kakao_webhook(
    payload: InboundWebhookPayload,
    request: Request,
    x_bot_secret: str = Header(...),
) -> dict[str, object]:
    settings = request.app.state.settings
    if x_bot_secret != settings.inbound_bot_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bot secret")

    event = NormalizedInboundEvent(
        room=payload.room,
        content=payload.content,
        log_id=payload.logId,
        package_name=payload.packageName,
        author_name=payload.author.name if payload.author else None,
        author_hash=payload.author.hash if payload.author else None,
        client_received_at=payload.client_received_at,
        source_timestamp=payload.timestamp,
        server_received_at=datetime.now(timezone.utc),
    )
    claim = request.app.state.event_repository.claim_for_processing(event)
    if claim.already_processed:
        return {"status": "ok", "duplicate": True, "processed": True}
    if not claim.should_process:
        return {"status": "accepted", "duplicate": True, "processed": False}
    try:
        await request.app.state.message_router.handle_event(event)
    except Exception as exc:  # noqa: BLE001
        request.app.state.event_repository.mark_failed(event.log_id, str(exc))
        logger.exception("webhook_event_routing_failed", extra={"log_id": event.log_id, "room": event.room})
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="event routing failed") from exc
    request.app.state.event_repository.mark_processed(event.log_id)
    return {"status": "ok", "duplicate": claim.duplicate, "processed": True}


@router.post("/polling/pull")
async def polling_pull(
    payload: PollingPullRequest,
    request: Request,
    x_bot_secret: str | None = Header(None),
) -> dict[str, object]:
    _require_bot_secret(request, x_bot_secret)
    items = request.app.state.delivery_repository.pull_pending_messages(limit=payload.limit, stale_after_seconds=300)
    serialized_items = [item.model_dump(mode="json") for item in items]
    snapshot = request.app.state.delivery_repository.get_queue_snapshot()
    return {
        "ok": True,
        "trace_id": str(uuid4()),
        "action": "polling.outbox.pull",
        "items": serialized_items,
        "messages": [],
        "error": None,
        "meta": {
            "count": len(serialized_items),
            "items": serialized_items,
            "pending_count": snapshot.pending_count,
            "inflight_count": snapshot.inflight_count,
        },
    }


@router.post("/polling/ack")
async def polling_ack(
    payload: PollingAckRequest,
    request: Request,
    x_bot_secret: str | None = Header(None),
) -> dict[str, object]:
    _require_bot_secret(request, x_bot_secret)
    updated = request.app.state.delivery_repository.acknowledge_polled_message(
        message_id=payload.message_id,
        success=payload.success,
        error_code=payload.error_code,
        error_message=payload.error_message,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    snapshot = request.app.state.delivery_repository.get_queue_snapshot()
    return {
        "ok": True,
        "trace_id": str(uuid4()),
        "action": "polling.outbox.ack",
        "messages": ["처리 완료"],
        "error": None,
        "meta": {
            "updated_count": 1,
            "success": payload.success,
            "pending_count": snapshot.pending_count,
            "inflight_count": snapshot.inflight_count,
        },
    }


def _require_bot_secret(request: Request, x_bot_secret: str | None) -> None:
    settings = request.app.state.settings
    if x_bot_secret != settings.inbound_bot_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bot secret")


@router.post("/delivery/ack")
async def delivery_ack(
    payload: DeliveryAckPayload,
    request: Request,
    x_bot_secret: str = Header(...),
) -> dict[str, object]:
    settings = request.app.state.settings
    if x_bot_secret != settings.inbound_bot_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bot secret")

    await request.app.state.delivery_service.resolve_delivery_ack(
        DeliveryResult(
            message_id=payload.message_id,
            status=payload.status,
            error_code=payload.error_code,
            error_message=payload.error_message,
        )
    )
    return {
        "ok": True,
        "message_id": payload.message_id,
        "status": payload.status,
    }
