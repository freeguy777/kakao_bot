from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from pydantic import ValidationError

from app.config import Settings
from app.errors import FatalDeliveryError, RetryableDeliveryError
from app.schemas import MessengerBotSocketEnvelope, MessengerBotSocketEnvelopeData, SocketControlCommand

logger = logging.getLogger(__name__)


class SocketClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._last_success_at: datetime | None = None

    async def send_message(self, *, message_id: str, target_room: str, text: str, package_name: str | None = None) -> None:
        try:
            resolved_package_name = package_name or self._settings.default_package_name
            if not message_id.strip():
                raise FatalDeliveryError("message_id is required")
            if not target_room.strip():
                raise FatalDeliveryError("target_room is required")
            if not text.strip():
                raise FatalDeliveryError("text is required")
            command = SocketControlCommand(
                message_id=message_id,
                token=self._settings.socket_shared_token,
                target_room=target_room,
                text=text,
                package_name=resolved_package_name,
            )
            envelope = MessengerBotSocketEnvelope(
                data=MessengerBotSocketEnvelopeData(
                    botName=self._settings.messengerbot_bot_name,
                    authorName=self._settings.mb_socket_control_author_name,
                    roomName=self._settings.mb_socket_control_room_name,
                    isGroupChat=False,
                    packageName=resolved_package_name,
                    message=json.dumps(command.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                )
            )
            payload = json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        except ValidationError as exc:
            raise FatalDeliveryError(f"Invalid built-in socket payload: {exc}") from exc

        await self._write_line(payload)
        self._last_success_at = datetime.now(timezone.utc)

    async def probe(self) -> dict[str, str]:
        await self._connect_only()
        self._last_success_at = datetime.now(timezone.utc)
        return {"status": "reachable", "mode": "connect_only"}

    async def close(self) -> None:
        return None

    async def _connect_only(self) -> None:
        async with self._lock:
            _reader = None
            writer = None
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._settings.smartphone_host, self._settings.smartphone_socket_port),
                    timeout=self._settings.socket_connect_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("socket_probe_failed", extra={"error": str(exc)})
                raise RetryableDeliveryError(str(exc)) from exc
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:  # noqa: BLE001
                        pass

    async def _write_line(self, payload: str) -> None:
        async with self._lock:
            _reader = None
            writer = None
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._settings.smartphone_host, self._settings.smartphone_socket_port),
                    timeout=self._settings.socket_connect_timeout_seconds,
                )
                writer.write((payload + "\n").encode("utf-8"))
                await writer.drain()
            except Exception as exc:  # noqa: BLE001
                logger.warning("socket_write_failed", extra={"error": str(exc)})
                raise RetryableDeliveryError(str(exc)) from exc
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:  # noqa: BLE001
                        pass

    @property
    def last_success_at(self) -> datetime | None:
        return self._last_success_at
