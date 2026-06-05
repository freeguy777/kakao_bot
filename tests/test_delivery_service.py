from __future__ import annotations

from app import constants
from app.repositories import DeliveryRepository, sqlite_policy_from_settings
from app.schemas import DeliveryQueueSnapshot, DeliveryResult
from app.services.admin_notify import AdminNotifyService
from app.services.delivery_service import DeliveryService


class FakeSocketClient:
    def __init__(self, responses: list[object] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("socket send should not be called")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if response is None:
            return DeliveryResult(message_id=str(kwargs["message_id"]), status=constants.ACK_OK)
        return response

    async def probe(self) -> dict[str, str]:
        return {"status": "reachable", "mode": "connect_only"}


class FakeQueueSnapshotRepository:
    def __init__(self, snapshot: DeliveryQueueSnapshot) -> None:
        self._snapshot = snapshot

    def get_queue_snapshot(self) -> DeliveryQueueSnapshot:
        return self._snapshot


def build_delivery_service(settings) -> DeliveryService:
    return DeliveryService(
        settings=settings,
        delivery_repository=object(),
        socket_client=object(),
        admin_notifier=object(),
    )


async def test_send_text_queues_pending_message_without_socket_call(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    admin_notifier = AdminNotifyService(test_settings)
    socket_client = FakeSocketClient()
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=repository,
        socket_client=socket_client,
        admin_notifier=admin_notifier,
    )

    results = await service.send_text("friend-room", "test message", package_name="custom.pkg")

    assert results[0].status == constants.ACK_OK
    assert socket_client.calls == []

    stored = repository.get_message(results[0].message_id)
    assert stored is not None
    assert stored.package_name == "custom.pkg"
    assert stored.target_room == "friend-room"
    assert stored.text == "test message"
    assert stored.status == constants.OUTBOUND_STATUS_PENDING
    assert stored.attempt_count == 0


async def test_send_text_queues_each_chunk_as_pending_row(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    socket_client = FakeSocketClient()
    settings = test_settings.model_copy(update={"message_chunk_limit": 100})
    service = DeliveryService(
        settings=settings,
        delivery_repository=repository,
        socket_client=socket_client,
        admin_notifier=AdminNotifyService(settings),
    )

    results = await service.send_text("friend-room", ("a" * 100) + "\n" + ("b" * 100))

    assert [result.status for result in results] == [constants.ACK_OK, constants.ACK_OK]
    assert socket_client.calls == []
    first = repository.get_message(results[0].message_id)
    second = repository.get_message(results[1].message_id)
    assert first is not None
    assert second is not None
    assert first.status == constants.OUTBOUND_STATUS_PENDING
    assert second.status == constants.OUTBOUND_STATUS_PENDING
    assert first.chunk_index == 1
    assert second.chunk_index == 2
    assert first.total_chunks == 2
    assert second.total_chunks == 2


async def test_retry_message_returns_existing_message_to_pending_without_socket_call(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    socket_client = FakeSocketClient()
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=repository,
        socket_client=socket_client,
        admin_notifier=AdminNotifyService(test_settings),
    )

    first_results = await service.send_text("friend-room", "test message", package_name="custom.pkg")
    repository.mark_failed(
        first_results[0].message_id,
        failure_type=constants.FAILURE_API,
        error_code="gateway_cannot_reply",
        error_message="bot.canReply returned false",
    )
    retry_result = await service.retry_message(first_results[0].message_id)

    assert retry_result.status == constants.ACK_OK
    assert retry_result.message_id == first_results[0].message_id
    assert socket_client.calls == []
    stored = repository.get_message(first_results[0].message_id)
    assert stored is not None
    assert stored.status == constants.OUTBOUND_STATUS_PENDING
    assert stored.package_name == "custom.pkg"
    assert stored.failure_type == constants.FAILURE_API


async def test_retry_message_returns_fatal_error_for_missing_message(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=repository,
        socket_client=FakeSocketClient(),
        admin_notifier=AdminNotifyService(test_settings),
    )

    retry_result = await service.retry_message("missing-message")

    assert retry_result.status == constants.ACK_FATAL_ERROR
    assert retry_result.error_message == "message not found"


async def test_socket_status_reports_polling_delivery(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    admin_notifier = AdminNotifyService(test_settings)
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=repository,
        socket_client=FakeSocketClient([None]),
        admin_notifier=admin_notifier,
    )

    assert await service.socket_status() == "socket delivery disabled; polling outbox active"


def test_split_message_uses_1600_char_default_limit(test_settings) -> None:
    service = build_delivery_service(test_settings)

    assert service._split_message("a" * 1600) == ["a" * 1600]
    assert service._split_message("a" * 1601) == ["a" * 1600, "a"]


def test_split_message_prefers_newline_near_limit(test_settings) -> None:
    settings = test_settings.model_copy(update={"message_chunk_limit": 120})
    service = build_delivery_service(settings)

    assert service._split_message(("a" * 110) + "\n" + ("b" * 20)) == [("a" * 110), ("b" * 20)]


def test_split_message_falls_back_to_space_when_newline_is_too_early(test_settings) -> None:
    settings = test_settings.model_copy(update={"message_chunk_limit": 120})
    service = build_delivery_service(settings)

    assert service._split_message(("a" * 10) + "\n" + ("b" * 89) + " " + ("c" * 30)) == [
        ("a" * 10) + "\n" + ("b" * 89),
        ("c" * 30),
    ]


def test_queue_snapshot_formats_korean_status_message(test_settings) -> None:
    admin_notifier = AdminNotifyService(test_settings)
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=FakeQueueSnapshotRepository(
            DeliveryQueueSnapshot(
                pending_count=2,
                inflight_count=3,
                failed_count=1,
                latest_failed_ids=["msg-1"],
            )
        ),
        socket_client=FakeSocketClient([None]),
        admin_notifier=admin_notifier,
    )

    assert service.queue_snapshot() == "대기 2건 / 전송중 3건 / 실패 1건 / 최근 실패: msg-1"
