from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
from zoneinfo import ZoneInfo

from server.config import DeliveryPolicy, _normalize_room_policy
from server.scheduler import JOB_BUILDERS, _build_scheduler_job_id, _deliver_job_message, _deliver_recently_due_jobs, _iter_room_job_specs


class RoomScheduleConfigTest(unittest.TestCase):
    def test_normalize_room_policy_supports_inline_room_triggers(self) -> None:
        room = _normalize_room_policy(
            "room-alpha",
            raw_room={
                "display_name": "테스트 방",
                "channel_id": "channel-1",
                "schedules": {
                    "enabled": True,
                    "jobs": [
                        {
                            "name": "family_morning_brief",
                            "triggers": [
                                {"hour": 7, "minute": 30},
                            ],
                        }
                    ],
                },
            },
            defaults=DeliveryPolicy(admin_room_key="admin-room"),
            scheduled_jobs={
                "family_morning_brief": {
                    "builder": "family_morning_brief",
                }
            },
        )

        self.assertTrue(room.schedules.enabled)
        self.assertEqual(len(room.schedules.jobs), 1)
        self.assertEqual(room.schedules.jobs[0].name, "family_morning_brief")
        self.assertEqual(room.schedules.jobs[0].builder, "family_morning_brief")
        self.assertEqual(room.schedules.jobs[0].triggers[0]["hour"], 7)
        self.assertEqual(room.to_dict()["schedules"]["jobs"][0]["triggers"][0]["minute"], 30)

    def test_normalize_room_policy_keeps_legacy_shared_schedule_jobs(self) -> None:
        room = _normalize_room_policy(
            "room-alpha",
            raw_room={
                "display_name": "테스트 방",
                "channel_id": "channel-1",
                "schedules": {
                    "enabled": True,
                    "jobs": [
                        "family_morning_brief",
                    ],
                },
            },
            defaults=DeliveryPolicy(admin_room_key="admin-room"),
            scheduled_jobs={
                "family_morning_brief": {
                    "builder": "family_morning_brief",
                    "triggers": [
                        {"hour": 8, "minute": 10},
                    ],
                }
            },
        )

        self.assertEqual(len(room.schedules.jobs), 1)
        self.assertEqual(room.schedules.jobs[0].builder, "family_morning_brief")
        self.assertEqual(room.schedules.jobs[0].triggers, ({"hour": 8, "minute": 10},))


class SchedulerRoomJobSpecTest(unittest.TestCase):
    def test_iter_room_job_specs_keeps_ids_unique_for_duplicate_job_names(self) -> None:
        room_jobs = (
            SimpleNamespace(
                name="family_morning_brief",
                builder="family_morning_brief",
                triggers=(
                    {"hour": 8, "minute": 10},
                ),
            ),
            SimpleNamespace(
                name="family_morning_brief",
                builder="family_morning_brief",
                triggers=(
                    {"hour": 9, "minute": 5},
                ),
            ),
        )
        registry = SimpleNamespace(
            rooms={
                "room-alpha": SimpleNamespace(
                    room_key="room-alpha",
                    schedules=SimpleNamespace(enabled=True, jobs=room_jobs),
                )
            }
        )

        with patch("server.scheduler.get_rooms_registry", return_value=registry):
            specs = _iter_room_job_specs()

        self.assertEqual(len(specs), 2)
        job_ids = [
            _build_scheduler_job_id(spec.room_key, spec.job_name, spec.job_index, spec.trigger, spec.trigger_index)
            for spec in specs
        ]
        self.assertEqual(len(set(job_ids)), 2)
        self.assertEqual(job_ids[0], "room-alpha:family_morning_brief:1:1")
        self.assertEqual(job_ids[1], "room-alpha:family_morning_brief:2:1")


class SchedulerCatchUpTest(unittest.TestCase):
    def test_deliver_recently_due_jobs_catches_up_within_grace_window(self) -> None:
        builder = Mock()
        spec = SimpleNamespace(
            room_key="room-alpha",
            job_name="family_morning_brief",
            job_index=1,
            builder=builder,
            trigger={"hour": 8, "minute": 10},
            trigger_index=1,
        )
        current_now = datetime(2026, 3, 29, 8, 21, tzinfo=ZoneInfo("Asia/Seoul"))

        with (
            patch("server.scheduler._iter_room_job_specs", return_value=[spec]),
            patch("server.scheduler._deliver_job_message") as mocked_deliver,
            patch("server.scheduler._safe_record_scheduler_event") as mocked_record,
        ):
            delivered = _deliver_recently_due_jobs(
                "Asia/Seoul",
                900,
                current_now=current_now,
            )

        self.assertEqual(delivered, ["room-alpha:family_morning_brief:1:1"])
        mocked_deliver.assert_called_once_with("family_morning_brief", "room-alpha", builder)
        mocked_record.assert_called_once()
        self.assertEqual(mocked_record.call_args.args[0], "catch_up_delivered")
        self.assertEqual(mocked_record.call_args.kwargs["meta"]["grace_seconds"], 900)
        self.assertEqual(mocked_record.call_args.kwargs["meta"]["delay_seconds"], 660.0)

    def test_deliver_recently_due_jobs_skips_stale_jobs_outside_grace_window(self) -> None:
        spec = SimpleNamespace(
            room_key="room-alpha",
            job_name="family_morning_brief",
            job_index=1,
            builder=Mock(),
            trigger={"hour": 8, "minute": 10},
            trigger_index=1,
        )
        current_now = datetime(2026, 3, 29, 8, 31, tzinfo=ZoneInfo("Asia/Seoul"))

        with (
            patch("server.scheduler._iter_room_job_specs", return_value=[spec]),
            patch("server.scheduler._deliver_job_message") as mocked_deliver,
            patch("server.scheduler._safe_record_scheduler_event") as mocked_record,
        ):
            delivered = _deliver_recently_due_jobs(
                "Asia/Seoul",
                900,
                current_now=current_now,
            )

        self.assertEqual(delivered, [])
        mocked_deliver.assert_not_called()
        mocked_record.assert_not_called()


class SchedulerDeliveryResultTest(unittest.TestCase):
    def test_hanall_job_builder_disables_raw_admin_payload(self) -> None:
        with patch("server.scheduler.build_hanall_news_brief", return_value="brief") as mocked_build:
            result = JOB_BUILDERS["hanall_news_brief"]("admin_test_room")

        self.assertEqual(result, "brief")
        mocked_build.assert_called_once_with(
            room_key="admin_test_room",
            raise_on_error=True,
            send_raw_to_admin=False,
        )

    def test_deliver_job_message_records_queued_for_polling(self) -> None:
        with (
            patch("server.scheduler.deliver_room_messages") as mocked_deliver,
            patch("server.scheduler.record_job_run") as mocked_record,
        ):
            mocked_deliver.return_value = {
                "ok": True,
                "transport": "polling",
                "via": "polling_outbox",
                "queued": True,
                "delivered": False,
                "outbox_ids": [11],
                "error": None,
            }

            _deliver_job_message("family_morning_brief", "room-alpha", lambda room_key: "brief")

        mocked_record.assert_called_once()
        self.assertEqual(mocked_record.call_args.args[1], "queued")

    def test_deliver_job_message_records_dedupe_skip_as_skipped(self) -> None:
        with (
            patch("server.scheduler.deliver_room_messages") as mocked_deliver,
            patch("server.scheduler.record_job_run") as mocked_record,
        ):
            mocked_deliver.return_value = {
                "ok": True,
                "transport": "polling",
                "via": "dedupe_skip",
                "queued": False,
                "delivered": False,
                "outbox_ids": [],
                "error": None,
            }

            _deliver_job_message("family_morning_brief", "room-alpha", lambda room_key: "brief")

        mocked_record.assert_called_once()
        self.assertEqual(mocked_record.call_args.args[1], "skipped")


if __name__ == "__main__":
    unittest.main()
