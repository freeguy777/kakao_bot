from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app import constants
from app.config import Settings
from app.errors import FatalDeliveryError, RetryableDeliveryError
from app.repositories import DeliveryRepository
from app.schemas import DeliveryResult
from app.services.socket_client import SocketClient


class DeliveryService:
    def __init__(
        self,
        *,
        settings: Settings,
        delivery_repository: DeliveryRepository,
        socket_client: SocketClient,
        admin_notifier: object,
    ) -> None:
        self._settings = settings
        self._delivery_repository = delivery_repository
        self._socket_client = socket_client
        self._admin_notifier = admin_notifier

    async def send_text(
        self,
        target_room: str,
        text: str,
        *,
        package_name: str | None = None,
        correlation_key: str | None = None,
        failure_type: str = constants.FAILURE_DELIVERY,
        suppress_admin_report: bool = False,
    ) -> list[DeliveryResult]:
        results: list[DeliveryResult] = []
        chunks = self._split_message(text)
        resolved_package_name = package_name or self._settings.default_package_name
        for index, chunk in enumerate(chunks, start=1):
            message_id = str(uuid4())
            self._delivery_repository.create_message(
                message_id=message_id,
                target_room=target_room,
                package_name=resolved_package_name,
                text=chunk,
                chunk_index=index,
                total_chunks=len(chunks),
                correlation_key=correlation_key,
            )
            results.append(
                await self._deliver_existing_message(
                    message_id=message_id,
                    target_room=target_room,
                    text=chunk,
                    package_name=resolved_package_name,
                    failure_type=failure_type,
                    suppress_admin_report=suppress_admin_report,
                )
            )
        return results

    async def retry_message(self, message_id: str) -> DeliveryResult:
        record = self._delivery_repository.get_message(message_id)
        if record is None:
            return DeliveryResult(
                message_id=message_id,
                status=constants.ACK_FATAL_ERROR,
                failure_type=constants.FAILURE_DELIVERY,
                error_message="message not found",
            )
        self._delivery_repository.mark_pending(message_id)
        return await self._deliver_existing_message(
            message_id=message_id,
            target_room=record.target_room,
            text=record.text,
            package_name=record.package_name,
            failure_type=record.failure_type or constants.FAILURE_DELIVERY,
            suppress_admin_report=(record.target_room == self._settings.admin_room_name),
        )

    async def socket_status(self) -> str:
        try:
            response = await self._socket_client.probe()
            return f"transport reachable ({response.get('mode', 'connect_only')})"
        except Exception as exc:  # noqa: BLE001
            return f"transport unreachable ({exc})"

    async def resolve_delivery_ack(self, result: DeliveryResult) -> bool:
        return await self._socket_client.resolve_delivery_ack(result)

    async def register_delivery_ack_waiter(self, message_id: str):
        return await self._socket_client.register_delivery_ack_waiter(message_id)

    def queue_snapshot(self) -> str:
        snapshot = self._delivery_repository.get_queue_snapshot()
        failed = ", ".join(snapshot.latest_failed_ids) if snapshot.latest_failed_ids else "없음"
        return f"대기 {snapshot.pending_count}건 / 실패 {snapshot.failed_count}건 / 최근 실패: {failed}"

    @staticmethod
    def all_delivered(results: list[DeliveryResult]) -> bool:
        return bool(results) and all(result.status == constants.ACK_OK for result in results)

    @staticmethod
    def summarize_results(results: list[DeliveryResult]) -> str:
        if not results:
            return "no delivery results"
        failed = [
            f"{result.message_id}:{result.status}:{result.failure_type or 'unknown'}:{result.error_message or ''}"
            for result in results
            if result.status != constants.ACK_OK
        ]
        return " | ".join(failed) if failed else "all delivered"

    async def _deliver_existing_message(
        self,
        *,
        message_id: str,
        target_room: str,
        text: str,
        package_name: str,
        failure_type: str,
        suppress_admin_report: bool,
    ) -> DeliveryResult:
        last_error_message: str | None = None
        last_error_code: str | None = None
        for attempt_no in range(1, self._settings.socket_max_retries + 1):
            try:
                ack_result = await self._socket_client.send_message(
                    message_id=message_id,
                    target_room=target_room,
                    text=text,
                    package_name=package_name,
                )
                if ack_result.status == constants.ACK_OK:
                    accepted_at = datetime.now(timezone.utc)
                    self._delivery_repository.record_attempt(
                        message_id=message_id,
                        attempt_no=attempt_no,
                        status=constants.ACK_OK,
                        ack_received_at=accepted_at,
                    )
                    self._delivery_repository.mark_success(message_id)
                    return DeliveryResult(message_id=message_id, status=constants.ACK_OK)

                last_error_message = ack_result.error_message or ack_result.error_code or "gateway delivery failed"
                last_error_code = ack_result.error_code
                self._delivery_repository.record_attempt(
                    message_id=message_id,
                    attempt_no=attempt_no,
                    status=ack_result.status,
                    error_code=last_error_code,
                    error_message=last_error_message,
                )
                if ack_result.status == constants.ACK_FATAL_ERROR:
                    return await self._finalize_failure(
                        message_id=message_id,
                        target_room=target_room,
                        status=constants.ACK_FATAL_ERROR,
                        failure_type=failure_type,
                        error_code=last_error_code or "gateway_fatal_error",
                        error_message=last_error_message,
                        suppress_admin_report=suppress_admin_report,
                    )
                if attempt_no >= self._settings.socket_max_retries:
                    return await self._finalize_failure(
                        message_id=message_id,
                        target_room=target_room,
                        status=constants.ACK_RETRYABLE_ERROR,
                        failure_type=failure_type,
                        error_code=last_error_code or "gateway_retryable_error",
                        error_message=last_error_message,
                        suppress_admin_report=suppress_admin_report,
                    )
                await asyncio.sleep(self._settings.socket_retry_backoff_seconds * attempt_no)
            except FatalDeliveryError as exc:
                last_error_message = str(exc)
                last_error_code = exc.error_code or "socket_fatal_error"
                self._delivery_repository.record_attempt(
                    message_id=message_id,
                    attempt_no=attempt_no,
                    status=constants.ACK_FATAL_ERROR,
                    error_code=last_error_code,
                    error_message=last_error_message,
                )
                return await self._finalize_failure(
                    message_id=message_id,
                    target_room=target_room,
                    status=constants.ACK_FATAL_ERROR,
                    failure_type=constants.FAILURE_SOCKET,
                    error_code=last_error_code,
                    error_message=last_error_message,
                    suppress_admin_report=suppress_admin_report,
                )
            except RetryableDeliveryError as exc:
                last_error_message = str(exc)
                last_error_code = exc.error_code or "socket_retryable_error"
                self._delivery_repository.record_attempt(
                    message_id=message_id,
                    attempt_no=attempt_no,
                    status=constants.ACK_RETRYABLE_ERROR,
                    error_code=last_error_code,
                    error_message=last_error_message,
                )
                if attempt_no >= self._settings.socket_max_retries:
                    return await self._finalize_failure(
                        message_id=message_id,
                        target_room=target_room,
                        status=constants.ACK_RETRYABLE_ERROR,
                        failure_type=constants.FAILURE_SOCKET,
                        error_code=last_error_code,
                        error_message=last_error_message,
                        suppress_admin_report=suppress_admin_report,
                    )
                await asyncio.sleep(self._settings.socket_retry_backoff_seconds * attempt_no)
        return DeliveryResult(
            message_id=message_id,
            status=constants.ACK_RETRYABLE_ERROR,
            failure_type=constants.FAILURE_SOCKET,
            error_code=last_error_code,
            error_message=last_error_message,
        )

    async def _finalize_failure(
        self,
        *,
        message_id: str,
        target_room: str,
        status: str,
        failure_type: str,
        error_code: str,
        error_message: str,
        suppress_admin_report: bool,
    ) -> DeliveryResult:
        self._delivery_repository.mark_failed(
            message_id,
            failure_type=failure_type,
            error_code=error_code,
            error_message=error_message,
        )
        if not suppress_admin_report and target_room != self._settings.admin_room_name:
            await self._admin_notifier.notify_delivery_failure(
                room_name=target_room,
                message_id=message_id,
                error_message=error_message,
                failure_type=failure_type,
            )
        return DeliveryResult(
            message_id=message_id,
            status=status,
            failure_type=failure_type,
            error_code=error_code,
            error_message=error_message,
        )

    def _split_message(self, text: str) -> list[str]:
        limit = max(100, self._settings.message_chunk_limit)
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text.strip()
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, limit)
            if split_at < int(limit * 0.6):
                split_at = remaining.rfind(" ", 0, limit)
            if split_at < int(limit * 0.5):
                split_at = limit
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        return [chunk for chunk in chunks if chunk]
