from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.config import DeliveryPolicy, _normalize_room_policy
from server.scheduler import _build_scheduler_job_id, _iter_room_job_specs


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


if __name__ == "__main__":
    unittest.main()
