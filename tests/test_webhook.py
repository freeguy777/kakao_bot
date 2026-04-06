from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


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
