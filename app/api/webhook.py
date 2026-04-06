from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.schemas import InboundWebhookPayload, NormalizedInboundEvent

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
