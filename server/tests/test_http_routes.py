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
            meta={"room_key": "room-alpha"},
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
        mocked_handle.assert_called_once()

    def test_socket_send_route_propagates_use_case_status_code(self) -> None:
        payload = build_standard_response(
            ok=False,
            trace_id="trace-route",
            action="socket.send",
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


if __name__ == "__main__":
    unittest.main()
