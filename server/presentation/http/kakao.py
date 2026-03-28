from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from server.application.use_cases import (
    DeliveryDispatchUseCase,
    MessageEventUseCase,
    OutboxPollingUseCase,
    RuntimeHealthUseCase,
)
from server.config import get_api_base_path
from server.core.contracts import MessageEventRequest, OutboxAckRequest, OutboxPullRequest, SocketFlushRequest, SocketSendRequest
from server.presentation.http.responses import build_payload_response

router = APIRouter(prefix=get_api_base_path())
MESSAGE_EVENT_USE_CASE = MessageEventUseCase()
OUTBOX_POLLING_USE_CASE = OutboxPollingUseCase()
DELIVERY_DISPATCH_USE_CASE = DeliveryDispatchUseCase()
RUNTIME_HEALTH_USE_CASE = RuntimeHealthUseCase()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    payload = RUNTIME_HEALTH_USE_CASE.build_health(getattr(request.app.state, "scheduler", None))
    return build_payload_response(payload)


@router.post("/events/message")
async def message_event(payload: MessageEventRequest) -> JSONResponse:
    response = await run_in_threadpool(MESSAGE_EVENT_USE_CASE.handle, payload)
    return build_payload_response(response)


@router.get("/socket/health")
async def socket_health() -> JSONResponse:
    payload = RUNTIME_HEALTH_USE_CASE.build_socket_health()
    return build_payload_response(payload)


@router.post("/socket/send")
async def socket_send(payload: SocketSendRequest) -> JSONResponse:
    result = await run_in_threadpool(DELIVERY_DISPATCH_USE_CASE.send, payload)
    return build_payload_response(result.payload, status_code=result.status_code)


@router.post("/socket/flush")
async def socket_flush(payload: SocketFlushRequest | None = None) -> JSONResponse:
    response = await run_in_threadpool(DELIVERY_DISPATCH_USE_CASE.flush, payload)
    return build_payload_response(response)


@router.post("/outbox/pull")
async def outbox_pull(payload: OutboxPullRequest) -> JSONResponse:
    response = await run_in_threadpool(
        OUTBOX_POLLING_USE_CASE.pull,
        payload,
        action="fallback.outbox.pull",
    )
    return build_payload_response(response)


@router.post("/outbox/ack")
async def outbox_ack(payload: OutboxAckRequest) -> JSONResponse:
    response = await run_in_threadpool(
        OUTBOX_POLLING_USE_CASE.ack,
        payload,
        action="fallback.outbox.ack",
    )
    return build_payload_response(response)


@router.post("/polling/pull")
async def polling_pull(payload: OutboxPullRequest) -> JSONResponse:
    response = await run_in_threadpool(OUTBOX_POLLING_USE_CASE.pull, payload, action="polling.outbox.pull")
    return build_payload_response(response)


@router.post("/polling/ack")
async def polling_ack(payload: OutboxAckRequest) -> JSONResponse:
    response = await run_in_threadpool(OUTBOX_POLLING_USE_CASE.ack, payload, action="polling.outbox.ack")
    return build_payload_response(response)
