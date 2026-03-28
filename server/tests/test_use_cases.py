from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from server.application.use_cases.delivery_dispatch import DeliveryDispatchUseCase
from server.application.use_cases.message_events import MessageEventUseCase
from server.application.use_cases.outbox_polling import OutboxPollingUseCase


def build_room(*, youtube_summary: bool = True, message_length_limit: int = 3000) -> SimpleNamespace:
    return SimpleNamespace(
        room_key="room-alpha",
        features=SimpleNamespace(youtube_summary=youtube_summary),
        delivery=SimpleNamespace(message_length_limit=message_length_limit),
    )


class MessageEventUseCaseTest(unittest.TestCase):
    def test_duplicate_message_short_circuits_processing(self) -> None:
        room_target_saver = Mock()
        processed_message_saver = Mock()
        youtube_collector = Mock()

        use_case = MessageEventUseCase(
            room_policy_resolver=lambda room_name, channel_id: build_room(),
            room_target_saver=room_target_saver,
            processed_message_checker=lambda room_key, log_id: True,
            processed_message_saver=processed_message_saver,
            feature_detector=lambda text: {"has_youtube_url": False, "youtube_urls": []},
            youtube_message_collector=youtube_collector,
            trace_id_factory=lambda: "trace-duplicate",
        )

        result = use_case.handle(
            {
                "room_name": "테스트방",
                "channel_id": "channel-1",
                "sender": "tester",
                "message": "hello",
                "log_id": "log-1",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "duplicate_message")
        room_target_saver.assert_called_once_with("room-alpha", "테스트방", "channel-1")
        processed_message_saver.assert_not_called()
        youtube_collector.assert_not_called()

    def test_collects_youtube_summary_with_room_policy_limit(self) -> None:
        processed_message_saver = Mock()
        youtube_result = SimpleNamespace(
            messages=["요약 결과"],
            processed_video_ids=["video-1"],
            skipped_video_ids=[],
            failed_urls=[],
            failure_details=[],
        )
        youtube_collector = Mock(return_value=youtube_result)
        deliverer = Mock(
            return_value={
                "ok": True,
                "via": "polling_outbox",
                "trace_id": "trace-youtube",
                "room_key": "room-alpha",
                "messages": ["요약 결과"],
                "ack": None,
                "outbox_ids": [101],
                "error": None,
            }
        )

        use_case = MessageEventUseCase(
            room_policy_resolver=lambda room_name, channel_id: build_room(message_length_limit=1234),
            room_target_saver=Mock(),
            processed_message_checker=lambda room_key, log_id: False,
            processed_message_saver=processed_message_saver,
            feature_detector=lambda text: {
                "has_youtube_url": True,
                "youtube_urls": ["https://youtu.be/example"],
            },
            youtube_message_collector=youtube_collector,
            room_message_deliverer=deliverer,
            admin_notifier=Mock(),
            trace_id_factory=lambda: "trace-youtube",
        )

        result = use_case.handle(
            {
                "room_name": "테스트방",
                "channel_id": "channel-1",
                "sender": "tester",
                "message": "https://youtu.be/example",
                "log_id": "log-2",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "youtube_summary")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["meta"]["delivery_mode"], "polling_outbox")
        self.assertEqual(result["meta"]["queued_message_count"], 1)
        processed_message_saver.assert_called_once()
        youtube_collector.assert_called_once_with(
            "room-alpha",
            ["https://youtu.be/example"],
            message_length_limit=1234,
        )
        deliverer.assert_called_once()

    def test_returns_user_facing_message_when_youtube_summary_fails(self) -> None:
        youtube_result = SimpleNamespace(
            messages=[],
            processed_video_ids=[],
            skipped_video_ids=[],
            failed_urls=["https://youtu.be/example"],
            failure_details=[
                {
                    "url": "https://youtu.be/example",
                    "video_id": "example",
                    "reason": "transcript request failed: ConnectionError: dns down",
                    "attempts": 2,
                }
            ],
        )
        admin_notifier = Mock()
        deliverer = Mock(
            return_value={
                "ok": True,
                "via": "polling_outbox",
                "trace_id": "trace-youtube-failed",
                "room_key": "room-alpha",
                "messages": [
                    "유튜브 요약을 지금 가져오지 못했습니다.\n잠시 후 다시 시도해 주세요.\nurl: https://youtu.be/example"
                ],
                "ack": None,
                "outbox_ids": [202],
                "error": None,
            }
        )

        use_case = MessageEventUseCase(
            room_policy_resolver=lambda room_name, channel_id: build_room(message_length_limit=1234),
            room_target_saver=Mock(),
            processed_message_checker=lambda room_key, log_id: False,
            processed_message_saver=Mock(),
            feature_detector=lambda text: {
                "has_youtube_url": True,
                "youtube_urls": ["https://youtu.be/example"],
            },
            youtube_message_collector=Mock(return_value=youtube_result),
            room_message_deliverer=deliverer,
            admin_notifier=admin_notifier,
            trace_id_factory=lambda: "trace-youtube-failed",
        )

        result = use_case.handle(
            {
                "room_name": "테스트방",
                "channel_id": "channel-1",
                "sender": "tester",
                "message": "https://youtu.be/example",
                "log_id": "log-3",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "youtube_summary_no_reply")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["meta"]["delivery_mode"], "polling_outbox")
        admin_notifier.assert_called_once()
        self.assertIn("attempts=2", admin_notifier.call_args.kwargs["detail"])
        self.assertIn("dns down", admin_notifier.call_args.kwargs["detail"])
        deliverer.assert_called_once()

    def test_suppresses_group_chat_failure_message_and_notifies_admin_only(self) -> None:
        youtube_result = SimpleNamespace(
            messages=[],
            processed_video_ids=[],
            skipped_video_ids=[],
            failed_urls=["https://youtu.be/example"],
            failure_details=[
                {
                    "url": "https://youtu.be/example",
                    "video_id": "example",
                    "reason": "gemini video request failed: ConnectionError: dns down",
                    "attempts": 2,
                }
            ],
        )
        admin_notifier = Mock()
        deliverer = Mock()

        use_case = MessageEventUseCase(
            room_policy_resolver=lambda room_name, channel_id: build_room(message_length_limit=1234),
            room_target_saver=Mock(),
            processed_message_checker=lambda room_key, log_id: False,
            processed_message_saver=Mock(),
            feature_detector=lambda text: {
                "has_youtube_url": True,
                "youtube_urls": ["https://youtu.be/example"],
            },
            youtube_message_collector=Mock(return_value=youtube_result),
            room_message_deliverer=deliverer,
            admin_notifier=admin_notifier,
            trace_id_factory=lambda: "trace-youtube-group-failed",
        )

        result = use_case.handle(
            {
                "room_name": "테스트방",
                "channel_id": "channel-1",
                "sender": "tester",
                "message": "https://youtu.be/example",
                "log_id": "log-3-group",
                "is_group_chat": True,
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "youtube_summary_no_reply")
        self.assertEqual(result["messages"], [])
        admin_notifier.assert_called_once()
        deliverer.assert_not_called()

    def test_falls_back_to_inline_messages_when_queueing_fails(self) -> None:
        youtube_result = SimpleNamespace(
            messages=["요약 결과"],
            processed_video_ids=["video-1"],
            skipped_video_ids=[],
            failed_urls=[],
            failure_details=[],
        )

        use_case = MessageEventUseCase(
            room_policy_resolver=lambda room_name, channel_id: build_room(message_length_limit=1234),
            room_target_saver=Mock(),
            processed_message_checker=lambda room_key, log_id: False,
            processed_message_saver=Mock(),
            feature_detector=lambda text: {
                "has_youtube_url": True,
                "youtube_urls": ["https://youtu.be/example"],
            },
            youtube_message_collector=Mock(return_value=youtube_result),
            room_message_deliverer=Mock(side_effect=RuntimeError("queue failed")),
            admin_notifier=Mock(),
            trace_id_factory=lambda: "trace-inline-fallback",
        )

        result = use_case.handle(
            {
                "room_name": "테스트방",
                "channel_id": "channel-1",
                "sender": "tester",
                "message": "https://youtu.be/example",
                "log_id": "log-4",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "youtube_summary")
        self.assertEqual(result["messages"], ["요약 결과"])
        self.assertEqual(result["meta"]["delivery"]["via"], "inline_fallback")


class OutboxPollingUseCaseTest(unittest.TestCase):
    def test_pull_builds_standard_response(self) -> None:
        puller = Mock(
            return_value=[
                {
                    "id": 1,
                    "room_key": "room-alpha",
                    "message": "queued",
                }
            ]
        )
        use_case = OutboxPollingUseCase(
            puller=puller,
            trace_id_factory=lambda: "trace-pull",
        )

        result = use_case.pull({"room_key": "room-alpha", "limit": 3}, action="fallback.outbox.pull")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "fallback.outbox.pull")
        self.assertEqual(result["meta"]["count"], 1)
        puller.assert_called_once_with("room-alpha", None, 3)

    def test_ack_enables_retry_increment_on_failure_by_default(self) -> None:
        acker = Mock(return_value=2)
        use_case = OutboxPollingUseCase(
            acker=acker,
            trace_id_factory=lambda: "trace-ack",
        )

        result = use_case.ack({"message_ids": [10, 20], "success": False}, action="polling.outbox.ack")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "polling.outbox.ack")
        self.assertEqual(result["meta"]["updated_count"], 2)
        acker.assert_called_once_with([10, 20], False, True)


class DeliveryDispatchUseCaseTest(unittest.TestCase):
    def test_send_returns_bad_request_when_room_key_is_missing(self) -> None:
        use_case = DeliveryDispatchUseCase(trace_id_factory=lambda: "trace-send")

        result = use_case.send({"message": "hello"})

        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.payload["ok"])
        self.assertEqual(result.payload["error"], "room_key is required")

    def test_send_marks_polling_queue_as_accepted(self) -> None:
        deliverer = Mock(
            return_value={
                "ok": True,
                "via": "polling_outbox",
                "trace_id": "trace-send",
                "room_key": "room-alpha",
                "messages": ["hello"],
                "ack": None,
                "outbox_ids": [1],
                "error": None,
            }
        )
        use_case = DeliveryDispatchUseCase(
            deliverer=deliverer,
            trace_id_factory=lambda: "trace-send",
        )

        result = use_case.send({"room_key": "room-alpha", "message": "hello"})

        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.payload["ok"])
        self.assertEqual(result.payload["messages"], ["delivery queued for polling"])
        deliverer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
