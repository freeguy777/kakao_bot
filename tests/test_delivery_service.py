from __future__ import annotations

from unittest.mock import AsyncMock

from app import constants
from app.errors import FatalDeliveryError, RetryableDeliveryError
from app.repositories import DeliveryRepository, sqlite_policy_from_settings
from app.services.admin_notify import AdminNotifyService
from app.services.delivery_service import DeliveryService


class FakeSocketClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def probe(self) -> dict[str, str]:
        return {"status": "reachable", "mode": "connect_only"}


async def test_delivery_retries_transport_errors_until_success(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    admin_notifier = AdminNotifyService(test_settings)
    admin_notifier.notify_delivery_failure = AsyncMock()
    socket_client = FakeSocketClient([RetryableDeliveryError("socket down"), None])
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=repository,
        socket_client=socket_client,
        admin_notifier=admin_notifier,
    )

    results = await service.send_text("friend-room", "test message", package_name="custom.pkg")

    assert results[0].status == constants.ACK_OK
    assert admin_notifier.notify_delivery_failure.await_count == 0
    assert len(socket_client.calls) == 2
    assert socket_client.calls[0]["message_id"] == socket_client.calls[1]["message_id"]

    stored = repository.get_message(results[0].message_id)
    assert stored is not None
    assert stored.package_name == "custom.pkg"


async def test_delivery_marks_socket_failure_after_retry_exhaustion(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    admin_notifier = AdminNotifyService(test_settings)
    admin_notifier.notify_delivery_failure = AsyncMock()
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=repository,
        socket_client=FakeSocketClient(
            [
                RetryableDeliveryError("socket down"),
                RetryableDeliveryError("socket down"),
                RetryableDeliveryError("socket down"),
            ]
        ),
        admin_notifier=admin_notifier,
    )

    results = await service.send_text("friend-room", "test message", failure_type=constants.FAILURE_API)

    assert results[0].status == constants.ACK_RETRYABLE_ERROR
    assert results[0].failure_type == constants.FAILURE_SOCKET
    stored = repository.get_message(results[0].message_id)
    assert stored is not None
    assert stored.failure_type == constants.FAILURE_SOCKET
    admin_notifier.notify_delivery_failure.assert_awaited_once()


async def test_retry_message_preserves_package_name_and_message_id(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    admin_notifier = AdminNotifyService(test_settings)
    admin_notifier.notify_delivery_failure = AsyncMock()
    socket_client = FakeSocketClient(
        [
            RetryableDeliveryError("socket down"),
            RetryableDeliveryError("socket down"),
            RetryableDeliveryError("socket down"),
            None,
        ]
    )
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=repository,
        socket_client=socket_client,
        admin_notifier=admin_notifier,
    )

    first_results = await service.send_text("friend-room", "test message", package_name="custom.pkg")
    retry_result = await service.retry_message(first_results[0].message_id)

    assert first_results[0].status == constants.ACK_RETRYABLE_ERROR
    assert retry_result.status == constants.ACK_OK
    assert socket_client.calls[-1]["package_name"] == "custom.pkg"
    assert socket_client.calls[-1]["message_id"] == first_results[0].message_id


async def test_delivery_marks_socket_failure_on_fatal_transport_error(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    admin_notifier = AdminNotifyService(test_settings)
    admin_notifier.notify_delivery_failure = AsyncMock()
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=repository,
        socket_client=FakeSocketClient([FatalDeliveryError("invalid built-in socket payload")]),
        admin_notifier=admin_notifier,
    )

    results = await service.send_text("friend-room", "test message")

    assert results[0].status == constants.ACK_FATAL_ERROR
    assert results[0].failure_type == constants.FAILURE_SOCKET
    assert results[0].error_code == "socket_fatal_error"
    admin_notifier.notify_delivery_failure.assert_awaited_once()


async def test_socket_status_reports_transport_reachability(app, test_settings) -> None:
    repository = DeliveryRepository(app.state.delivery_repository._session_factory, sqlite_policy_from_settings(test_settings))
    admin_notifier = AdminNotifyService(test_settings)
    admin_notifier.notify_delivery_failure = AsyncMock()
    service = DeliveryService(
        settings=test_settings,
        delivery_repository=repository,
        socket_client=FakeSocketClient([None]),
        admin_notifier=admin_notifier,
    )

    assert await service.socket_status() == "transport reachable (connect_only)"
