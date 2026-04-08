from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.schemas import NormalizedInboundEvent


def test_webhook_idempotency(client, app) -> None:
    app.state.message_router.handle_event = AsyncMock()
    payload = {
        "room": "테스트방방방",
        "content": "!안녕",
        "logId": "log-001",
        "packageName": "com.kakao.talk",
        "author": {"name": "tester"},
    }

    response = client.post("/kakao/webhook", json=payload, headers={"X-Bot-Secret": "test-secret"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "duplicate": False, "processed": True}

    duplicate = client.post("/kakao/webhook", json=payload, headers={"X-Bot-Secret": "test-secret"})
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "ok", "duplicate": True, "processed": True}
    assert app.state.message_router.handle_event.await_count == 1


def test_webhook_retries_failed_routing_for_same_log_id(app) -> None:
    app.state.message_router.handle_event = AsyncMock(side_effect=[RuntimeError("boom"), None])
    payload = {
        "room": "테스트방방방",
        "content": "!안녕",
        "logId": "log-002",
        "packageName": "com.kakao.talk",
        "author": {"name": "tester"},
    }

    with TestClient(app, raise_server_exceptions=False) as retryable_client:
        first = retryable_client.post("/kakao/webhook", json=payload, headers={"X-Bot-Secret": "test-secret"})
        second = retryable_client.post("/kakao/webhook", json=payload, headers={"X-Bot-Secret": "test-secret"})

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


def test_polling_pull_returns_empty_compatible_payload(client) -> None:
    response = client.post("/kakao/polling/pull", json={"room_key": "friends_room", "limit": 5})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["action"] == "polling.outbox.pull"
    assert response.json()["meta"] == {"count": 0, "items": []}


def test_polling_ack_returns_compatible_payload(client) -> None:
    response = client.post("/kakao/polling/ack", json={"message_ids": [1, 2], "success": True})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["action"] == "polling.outbox.ack"
    assert response.json()["meta"] == {"updated_count": 2, "success": True}
