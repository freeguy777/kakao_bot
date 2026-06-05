from __future__ import annotations

from uuid import uuid4

from app import constants
from app.config import Settings
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
                failure_type=failure_type,
            )
            results.append(DeliveryResult(message_id=message_id, status=constants.ACK_OK))
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
        return DeliveryResult(
            message_id=message_id,
            status=constants.ACK_OK,
            failure_type=record.failure_type or constants.FAILURE_DELIVERY,
        )

    async def socket_status(self) -> str:
        return "socket delivery disabled; polling outbox active"

    async def resolve_delivery_ack(self, result: DeliveryResult) -> bool:
        return await self._socket_client.resolve_delivery_ack(result)

    async def register_delivery_ack_waiter(self, message_id: str):
        return await self._socket_client.register_delivery_ack_waiter(message_id)

    def queue_snapshot(self) -> str:
        snapshot = self._delivery_repository.get_queue_snapshot()
        failed = ", ".join(snapshot.latest_failed_ids) if snapshot.latest_failed_ids else "없음"
        return (
            f"대기 {snapshot.pending_count}건 / 전송중 {snapshot.inflight_count}건 / "
            f"실패 {snapshot.failed_count}건 / 최근 실패: {failed}"
        )

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
