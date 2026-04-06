from __future__ import annotations

from datetime import date

from app import constants
from app.scheduler import SchedulerService
from app.schemas import DeliveryResult


class FakeScheduledJobRepository:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str | None]] = []

    def is_success(self, job_key: str) -> bool:
        return any(key == job_key and status == constants.SCHEDULED_STATUS_SUCCESS for key, status, _ in self.records)

    def mark_status(self, job_key: str, status: str, detail: str | None = None) -> None:
        self.records.append((job_key, status, detail))


class FakeRoomRegistry:
    def __init__(self, room) -> None:
        self._room = room

    def resolve_room(self, room_name: str):
        return self._room if room_name == self._room.name else None

    def list_rooms(self):
        return [self._room]


class FakeRoom:
    def __init__(self, *, name: str, key: str, package_name: str, hanall_publish_time: str | None, features: dict[str, bool], publish_time: str = "08:10") -> None:
        self.name = name
        self.key = key
        self.package_name = package_name
        self.hanall_publish_time = hanall_publish_time
        self.features = features
        self.weather = type("Weather", (), {"publish_time": publish_time})()


class FakeHanallResearchService:
    def __init__(self, artifact_present: bool = True) -> None:
        self.artifact = type("Artifact", (), {"summary_text": "summary", "detail_text": "detail"})()
        self.artifact_present = artifact_present
        self.get_existing_calls = 0
        self.get_or_create_calls = 0

    async def get_or_create_daily_artifact(self, artifact_date: date):
        self.get_or_create_calls += 1
        return self.artifact

    async def get_existing_daily_artifact(self, artifact_date: date):
        self.get_existing_calls += 1
        if self.artifact_present:
            return self.artifact
        return None

    def render_public_message(self, artifact) -> str:
        return artifact.summary_text

    def render_admin_message(self, artifact) -> str:
        return artifact.detail_text


class FakeFamilyBriefService:
    async def build_daily_message(self, room) -> str:
        return "family-brief"


class FakeDeliveryService:
    def __init__(self, responses: list[list[DeliveryResult]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def send_text(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return self.responses.pop(0)

    def all_delivered(self, results: list[DeliveryResult]) -> bool:
        return all(result.status == constants.ACK_OK for result in results)

    def summarize_results(self, results: list[DeliveryResult]) -> str:
        return ",".join(result.status for result in results)


class FakeAdminNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def notify_feature_error(self, **kwargs):
        self.calls.append(kwargs)


async def test_publish_hanall_marks_failed_when_public_delivery_fails(test_settings) -> None:
    room = FakeRoom(
        name="hanall_room",
        key="hanall_openchat",
        package_name="com.kakao.talk",
        hanall_publish_time="09:00",
        features={"hanall_briefing": True},
    )
    scheduled_repo = FakeScheduledJobRepository()
    scheduler = SchedulerService(
        settings=test_settings,
        room_registry=FakeRoomRegistry(room),
        delivery_service=FakeDeliveryService(
            [[DeliveryResult(message_id="m1", status=constants.ACK_RETRYABLE_ERROR, failure_type=constants.FAILURE_SOCKET)]]
        ),
        admin_notifier=FakeAdminNotifier(),
        hanall_research_service=FakeHanallResearchService(),
        family_brief_service=FakeFamilyBriefService(),
        scheduled_job_repository=scheduled_repo,
    )

    await scheduler.publish_hanall_room(room.name)

    assert any(status == constants.SCHEDULED_STATUS_FAILED for _, status, _ in scheduled_repo.records)


async def test_publish_hanall_uses_existing_artifact_for_public_and_admin_delivery(test_settings) -> None:
    room = FakeRoom(
        name="hanall_room",
        key="hanall_openchat",
        package_name="com.kakao.talk",
        hanall_publish_time="09:00",
        features={"hanall_briefing": True},
    )
    scheduled_repo = FakeScheduledJobRepository()
    admin_notifier = FakeAdminNotifier()
    hanall_service = FakeHanallResearchService()
    delivery_service = FakeDeliveryService(
        [
            [DeliveryResult(message_id="m1", status=constants.ACK_OK)],
            [DeliveryResult(message_id="m2", status=constants.ACK_OK)],
        ]
    )
    scheduler = SchedulerService(
        settings=test_settings,
        room_registry=FakeRoomRegistry(room),
        delivery_service=delivery_service,
        admin_notifier=admin_notifier,
        hanall_research_service=hanall_service,
        family_brief_service=FakeFamilyBriefService(),
        scheduled_job_repository=scheduled_repo,
    )

    await scheduler.publish_hanall_room(room.name)

    assert hanall_service.get_existing_calls == 1
    assert hanall_service.get_or_create_calls == 0
    assert admin_notifier.calls == []
    assert len(delivery_service.calls) == 2
    assert delivery_service.calls[0]["args"][0] == room.name
    assert delivery_service.calls[0]["args"][1] == "summary"
    assert delivery_service.calls[0]["kwargs"]["package_name"] == room.package_name
    assert delivery_service.calls[1]["args"][0] == test_settings.admin_room_name
    assert delivery_service.calls[1]["args"][1] == "detail"
    assert delivery_service.calls[1]["kwargs"]["suppress_admin_report"] is True
    assert any(status == constants.SCHEDULED_STATUS_SUCCESS for _, status, _ in scheduled_repo.records)


async def test_publish_hanall_fails_when_collect_artifact_is_missing(test_settings) -> None:
    room = FakeRoom(
        name="hanall_room",
        key="hanall_openchat",
        package_name="com.kakao.talk",
        hanall_publish_time="09:00",
        features={"hanall_briefing": True},
    )
    scheduled_repo = FakeScheduledJobRepository()
    admin_notifier = FakeAdminNotifier()
    hanall_service = FakeHanallResearchService(artifact_present=False)
    delivery_service = FakeDeliveryService([])
    scheduler = SchedulerService(
        settings=test_settings,
        room_registry=FakeRoomRegistry(room),
        delivery_service=delivery_service,
        admin_notifier=admin_notifier,
        hanall_research_service=hanall_service,
        family_brief_service=FakeFamilyBriefService(),
        scheduled_job_repository=scheduled_repo,
    )

    await scheduler.publish_hanall_room(room.name)

    assert hanall_service.get_existing_calls == 1
    assert hanall_service.get_or_create_calls == 0
    assert delivery_service.responses == []
    assert delivery_service.calls == []
    assert any(status == constants.SCHEDULED_STATUS_FAILED for _, status, _ in scheduled_repo.records)
    assert admin_notifier.calls[0]["failure_type"] == constants.FAILURE_RESEARCH


async def test_publish_family_marks_failed_when_delivery_fails(test_settings) -> None:
    room = FakeRoom(
        name="family_room",
        key="family_room",
        package_name="com.kakao.talk",
        hanall_publish_time=None,
        features={"weather": True, "child_age": True},
    )
    scheduled_repo = FakeScheduledJobRepository()
    scheduler = SchedulerService(
        settings=test_settings,
        room_registry=FakeRoomRegistry(room),
        delivery_service=FakeDeliveryService(
            [[DeliveryResult(message_id="m2", status=constants.ACK_FATAL_ERROR, failure_type=constants.FAILURE_DELIVERY)]]
        ),
        admin_notifier=FakeAdminNotifier(),
        hanall_research_service=FakeHanallResearchService(),
        family_brief_service=FakeFamilyBriefService(),
        scheduled_job_repository=scheduled_repo,
    )

    await scheduler.publish_family_brief(room.name)

    assert any(status == constants.SCHEDULED_STATUS_FAILED for _, status, _ in scheduled_repo.records)
