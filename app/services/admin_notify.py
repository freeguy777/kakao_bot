from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from app import constants
from app.config import Settings

logger = logging.getLogger(__name__)

SenderType = Callable[[str, str], Awaitable[object]]


class AdminNotifyService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sender: SenderType | None = None
        self._timezone = ZoneInfo(settings.app_timezone)

    def attach_sender(self, sender: SenderType) -> None:
        self._sender = sender

    async def notify(self, text: str) -> None:
        if self._sender is None:
            logger.warning("admin_sender_not_attached", extra={"message_text": text})
            return
        await self._sender(self._settings.admin_room_name, text)

    async def notify_feature_error(
        self,
        *,
        room_name: str,
        feature_name: str,
        error_message: str,
        failure_type: str = constants.FAILURE_API,
    ) -> None:
        now = datetime.now(self._timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
        message = (
            "[기능 실패]\n"
            f"방: {room_name}\n"
            f"기능: {feature_name}\n"
            f"유형: {failure_type}\n"
            f"시각: {now}\n"
            f"오류: {error_message}"
        )
        await self.notify(message)

    async def notify_delivery_failure(
        self,
        *,
        room_name: str,
        message_id: str,
        error_message: str,
        failure_type: str,
    ) -> None:
        now = datetime.now(self._timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
        message = (
            "[전송 실패]\n"
            f"방: {room_name}\n"
            f"message_id: {message_id}\n"
            f"유형: {failure_type}\n"
            f"시각: {now}\n"
            f"오류: {error_message}"
        )
        await self.notify(message)
