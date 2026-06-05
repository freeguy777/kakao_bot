from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select

from app import constants
from app.models import OutboundMessage
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


async def test_polling_pull_returns_pending_messages_and_marks_inflight(app) -> None:
    queued = await app.state.delivery_service.send_text("friends_room", "hello", package_name="custom.pkg")

    response = await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["action"] == "polling.outbox.pull"
    assert payload["meta"]["count"] == 1
    assert payload["items"] == payload["meta"]["items"]
    assert payload["items"][0]["message_id"] == queued[0].message_id
    assert payload["items"][0]["target_room"] == "friends_room"
    assert payload["items"][0]["package_name"] == "custom.pkg"
    assert payload["items"][0]["text"] == "hello"
    assert payload["items"][0]["chunk_index"] == 1
    assert payload["items"][0]["total_chunks"] == 1
    assert payload["items"][0]["created_at"]

    stored = app.state.delivery_repository.get_message(queued[0].message_id)
    assert stored is not None
    assert stored.status == constants.OUTBOUND_STATUS_INFLIGHT
    assert stored.inflight_at is not None


async def test_polling_ack_success_marks_message_sent(app) -> None:
    queued = await app.state.delivery_service.send_text("friends_room", "hello")
    await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )

    response = await _post(
        app,
        "/kakao/polling/ack",
        json={"message_id": queued[0].message_id, "success": True, "error_code": None, "error_message": None},
        headers={"X-Bot-Secret": "test-secret"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["action"] == "polling.outbox.ack"
    assert response.json()["meta"]["updated_count"] == 1
    assert response.json()["meta"]["success"] is True
    stored = app.state.delivery_repository.get_message(queued[0].message_id)
    assert stored is not None
    assert stored.status == constants.OUTBOUND_STATUS_SENT
    assert stored.acknowledged_at is not None
    assert stored.inflight_at is None
    assert stored.attempt_count == 1


async def test_polling_ack_failure_records_error_and_marks_message_failed(app) -> None:
    queued = await app.state.delivery_service.send_text("friends_room", "hello")
    await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )

    response = await _post(
        app,
        "/kakao/polling/ack",
        json={
            "message_id": queued[0].message_id,
            "success": False,
            "error_code": "gateway_cannot_reply",
            "error_message": "bot.canReply returned false",
        },
        headers={"X-Bot-Secret": "test-secret"},
    )

    assert response.status_code == 200
    stored = app.state.delivery_repository.get_message(queued[0].message_id)
    assert stored is not None
    assert stored.status == constants.OUTBOUND_STATUS_FAILED
    assert stored.last_error_code == "gateway_cannot_reply"
    assert stored.last_error_message == "bot.canReply returned false"
    assert stored.attempt_count == 1
    assert stored.inflight_at is None
    assert stored.acknowledged_at is not None

    pull_again = await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )
    assert pull_again.status_code == 200
    assert pull_again.json()["items"] == []


async def test_polling_pull_reclaims_stale_inflight_messages(app) -> None:
    queued = await app.state.delivery_service.send_text("friends_room", "hello")
    await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )
    with app.state.delivery_repository._session_factory() as session:
        record = session.scalar(select(OutboundMessage).where(OutboundMessage.message_id == queued[0].message_id))
        assert record is not None
        record.inflight_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        session.commit()

    response = await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["message_id"] for item in items] == [queued[0].message_id]


async def test_polling_pull_expires_old_pending_messages_without_returning_them(app) -> None:
    queued = await app.state.delivery_service.send_text("friends_room", "old hello")
    with app.state.delivery_repository._session_factory() as session:
        record = session.scalar(select(OutboundMessage).where(OutboundMessage.message_id == queued[0].message_id))
        assert record is not None
        record.created_at = datetime.now(timezone.utc) - timedelta(minutes=11)
        record.updated_at = record.created_at
        session.commit()

    response = await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
    stored = app.state.delivery_repository.get_message(queued[0].message_id)
    assert stored is not None
    assert stored.status == constants.OUTBOUND_STATUS_FAILED
    assert stored.last_error_code == constants.OUTBOUND_EXPIRED_ERROR_CODE
    assert stored.last_error_message == constants.OUTBOUND_EXPIRED_ERROR_MESSAGE
    assert stored.inflight_at is None


async def test_polling_pull_expires_old_stale_inflight_messages_without_returning_them(app) -> None:
    queued = await app.state.delivery_service.send_text("friends_room", "old inflight")
    await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )
    with app.state.delivery_repository._session_factory() as session:
        record = session.scalar(select(OutboundMessage).where(OutboundMessage.message_id == queued[0].message_id))
        assert record is not None
        record.created_at = datetime.now(timezone.utc) - timedelta(minutes=11)
        record.inflight_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        session.commit()

    response = await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
    stored = app.state.delivery_repository.get_message(queued[0].message_id)
    assert stored is not None
    assert stored.status == constants.OUTBOUND_STATUS_FAILED
    assert stored.last_error_code == constants.OUTBOUND_EXPIRED_ERROR_CODE
    assert stored.inflight_at is None


async def test_polling_api_rejects_missing_or_invalid_secret(app) -> None:
    missing_pull = await _post(app, "/kakao/polling/pull", json={"limit": 5})
    wrong_pull = await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "wrong-secret"},
    )
    missing_ack = await _post(app, "/kakao/polling/ack", json={"message_id": "msg-1", "success": True})
    wrong_ack = await _post(
        app,
        "/kakao/polling/ack",
        json={"message_id": "msg-1", "success": True},
        headers={"X-Bot-Secret": "wrong-secret"},
    )

    assert missing_pull.status_code == 401
    assert wrong_pull.status_code == 401
    assert missing_ack.status_code == 401
    assert wrong_ack.status_code == 401


async def test_polling_handles_messages_for_multiple_rooms_in_order(app) -> None:
    first = await app.state.delivery_service.send_text("hanall-room", "hanall briefing", package_name="custom.pkg")
    second = await app.state.delivery_service.send_text("weather-room", "weather briefing", package_name="custom.pkg")

    response = await _post(
        app,
        "/kakao/polling/pull",
        json={"limit": 5},
        headers={"X-Bot-Secret": "test-secret"},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["message_id"] for item in items] == [first[0].message_id, second[0].message_id]
    assert [item["target_room"] for item in items] == ["hanall-room", "weather-room"]


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
