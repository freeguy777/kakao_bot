from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.core.contracts import build_standard_response
from server.presentation.http.kakao import router


class HttpRouteContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def test_message_event_route_returns_standard_payload(self) -> None:
        payload = build_standard_response(
            ok=True,
            trace_id="trace-route",
            action="youtube_summary",
            messages=["요약"],
            error=None,
            meta={
                "room_key": "room-alpha",
                "delivery": {
                    "ok": True,
                    "transport": "polling",
                    "via": "polling_outbox",
                    "queued": True,
                    "delivered": False,
                    "outbox_ids": [1],
                    "error": None,
                },
            },
        )

        with patch("server.presentation.http.kakao.MessageEventUseCase.handle", return_value=payload) as mocked_handle:
            response = self.client.post(
                "/kakao/events/message",
                json={
                    "room_name": "테스트방",
                    "channel_id": "channel-1",
                    "sender": "tester",
                    "message": "https://youtu.be/example",
                    "log_id": "log-1",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "youtube_summary")
        self.assertEqual(response.json()["meta"]["delivery"]["transport"], "polling")
        mocked_handle.assert_called_once()

    def test_socket_send_route_propagates_use_case_status_code(self) -> None:
        payload = build_standard_response(
            ok=False,
            trace_id="trace-route",
            action="polling.outbox.enqueue",
            messages=[],
            error="room_key is required",
            meta={},
        )

        with patch("server.presentation.http.kakao.DeliveryDispatchUseCase.send") as mocked_send:
            mocked_send.return_value.status_code = 400
            mocked_send.return_value.payload = payload

            response = self.client.post(
                "/kakao/socket/send",
                json={"message": "hello"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "room_key is required")
        mocked_send.assert_called_once()

    def test_polling_pull_route_uses_canonical_contract(self) -> None:
        payload = build_standard_response(
            ok=True,
            trace_id="trace-pull",
            action="polling.outbox.pull",
            messages=[],
            error=None,
            meta={
                "active_transport": "polling_outbox",
                "count": 1,
                "items": [{"id": 1, "message": "queued"}],
                "polling_enabled": True,
                "polling_interval_ms": 15000,
                "pending_outbox_count": 1,
                "last_pull_at": "2026-03-29T09:00:00+09:00",
                "last_success_at": "2026-03-29T09:00:00+09:00",
                "last_ack_success_count": 0,
                "last_ack_fail_count": 0,
            },
        )

        with patch("server.presentation.http.kakao.OutboxPollingUseCase.pull", return_value=payload) as mocked_pull:
            response = self.client.post("/kakao/polling/pull", json={"limit": 5})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "polling.outbox.pull")
        self.assertEqual(response.json()["meta"]["active_transport"], "polling_outbox")
        mocked_pull.assert_called_once()

    def test_polling_ack_route_uses_canonical_contract(self) -> None:
        payload = build_standard_response(
            ok=True,
            trace_id="trace-ack",
            action="polling.outbox.ack",
            messages=["처리 완료"],
            error=None,
            meta={
                "active_transport": "polling_outbox",
                "updated_count": 2,
                "success": True,
                "polling_enabled": True,
                "polling_interval_ms": 15000,
                "pending_outbox_count": 0,
                "last_pull_at": None,
                "last_success_at": "2026-03-29T09:00:10+09:00",
                "last_ack_success_count": 2,
                "last_ack_fail_count": 0,
            },
        )

        with patch("server.presentation.http.kakao.OutboxPollingUseCase.ack", return_value=payload) as mocked_ack:
            response = self.client.post("/kakao/polling/ack", json={"message_ids": [1, 2], "success": True})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "polling.outbox.ack")
        self.assertEqual(response.json()["meta"]["updated_count"], 2)
        mocked_ack.assert_called_once()

    def test_socket_health_route_reports_deprecated_inactive(self) -> None:
        payload = build_standard_response(
            ok=True,
            trace_id="trace-socket-health",
            action="socket.health",
            messages=["socket delivery is inactive; polling_outbox is the only supported delivery path"],
            error=None,
            meta={"deprecated": True, "status": "inactive", "active_transport": "polling_outbox"},
        )

        with patch("server.presentation.http.kakao.RuntimeHealthUseCase.build_socket_health", return_value=payload):
            response = self.client.get("/kakao/socket/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meta"]["status"], "inactive")

    def test_polling_status_route_exposes_active_transport(self) -> None:
        payload = build_standard_response(
            ok=True,
            trace_id="trace-polling-status",
            action="polling.status",
            messages=["ok"],
            error=None,
            meta={"active_transport": "polling_outbox", "polling_enabled": True, "polling_interval_ms": 15000},
        )

        with patch("server.presentation.http.kakao.RuntimeHealthUseCase.build_polling_status", return_value=payload):
            response = self.client.get("/kakao/polling/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meta"]["active_transport"], "polling_outbox")


if __name__ == "__main__":
    unittest.main()
