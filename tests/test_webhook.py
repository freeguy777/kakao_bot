from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx

from app import constants
from app.schemas import NormalizedInboundEvent


async def _post(app, path: str, *, json: dict, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, json=json, headers=headers)


async def test_webhook_idempotency(app) -> None:
    app.state.message_router.handle_event = AsyncMock()
    payload = {
        "room": "테스트방방방",
        "content": "!안녕",
        "logId": "log-001",
        "packageName": "com.kakao.talk",
        "author": {"name": "tester"},
    }

    response = await _post(app, "/kakao/webhook", json=payload, headers={"X-Bot-Secret": "test-secret"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "duplicate": False, "processed": True}

    duplicate = await _post(app, "/kakao/webhook", json=payload, headers={"X-Bot-Secret": "test-secret"})
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "ok", "duplicate": True, "processed": True}
    assert app.state.message_router.handle_event.await_count == 1


async def test_webhook_retries_failed_routing_for_same_log_id(app) -> None:
    app.state.message_router.handle_event = AsyncMock(side_effect=[RuntimeError("boom"), None])
    payload = {
        "room": "테스트방방방",
        "content": "!안녕",
        "logId": "log-002",
        "packageName": "com.kakao.talk",
        "author": {"name": "tester"},
    }

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as retryable_client:
        first = await retryable_client.post("/kakao/webhook", json=payload, headers={"X-Bot-Secret": "test-secret"})
        second = await retryable_client.post("/kakao/webhook", json=payload, headers={"X-Bot-Secret": "test-secret"})

    assert first.status_code == 500
    assert first.json() == {"detail": "event routing failed"}
    assert second.status_code == 200
    assert second.json() == {"status": "ok", "duplicate": True, "processed": True}
    assert app.state.message_router.handle_event.await_count == 2


def test_event_repository_does_not_reprocess_same_log_id_while_processing(app) -> None:
    event = NormalizedInboundEvent(
        room="테스트방방방",
        content="https://youtu.be/example",
        log_id="log-003",
        package_name="com.kakao.talk",
        author_name="tester",
        server_received_at=datetime.now(timezone.utc),
    )

    first = app.state.event_repository.claim_for_processing(event)
    claim = app.state.event_repository.claim_for_processing(event)

    assert first.duplicate is False
    assert first.should_process is True
    assert first.already_processed is False
    assert claim.duplicate is True
    assert claim.should_process is False
    assert claim.already_processed is False


async def test_polling_pull_returns_empty_compatible_payload(app) -> None:
    response = await _post(app, "/kakao/polling/pull", json={"room_key": "friends_room", "limit": 5})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["action"] == "polling.outbox.pull"
    assert response.json()["meta"] == {"count": 0, "items": []}


async def test_polling_ack_returns_compatible_payload(app) -> None:
    response = await _post(app, "/kakao/polling/ack", json={"message_ids": [1, 2], "success": True})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["action"] == "polling.outbox.ack"
    assert response.json()["meta"] == {"updated_count": 2, "success": True}


async def test_delivery_ack_resolves_waiting_message(app) -> None:
    response = await _post(
        app,
        "/kakao/delivery/ack",
        json={
            "message_id": "ack-001",
            "status": constants.ACK_RETRYABLE_ERROR,
            "error_code": "gateway_cannot_reply",
            "error_message": "bot.canReply returned false",
        },
        headers={"X-Bot-Secret": "test-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "message_id": "ack-001", "status": constants.ACK_RETRYABLE_ERROR}
    future = await app.state.delivery_service.register_delivery_ack_waiter("ack-001")
    assert future.done() is True
    result = future.result()
    assert result.message_id == "ack-001"
    assert result.error_code == "gateway_cannot_reply"


async def test_delivery_ack_rejects_invalid_secret(app) -> None:
    response = await _post(
        app,
        "/kakao/delivery/ack",
        json={"message_id": "ack-002", "status": constants.ACK_OK},
        headers={"X-Bot-Secret": "wrong-secret"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid bot secret"}
