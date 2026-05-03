from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.schemas import NormalizedInboundEvent


class AdminCommandService:
    def __init__(
        self,
        *,
        settings: Settings,
        room_registry: object,
        delivery_service: object,
        scheduler_service: object | None = None,
    ) -> None:
        self._settings = settings
        self._room_registry = room_registry
        self._delivery_service = delivery_service
        self._scheduler_service = scheduler_service
        self._timezone = ZoneInfo(settings.app_timezone)
        self._feature_pattern = re.compile(
            r"^@기능설정\s+(.+)\s+(youtube_summary|llm_chat|hanall_briefing|weather|child_age)\s+(on|off)$"
        )
        self._feature_query_pattern = re.compile(r"^@기능조회\s+(.+)$")
        self._retry_pattern = re.compile(r"^@재전송\s+([A-Za-z0-9\-]+)$")
        self._hanall_publish_pattern = re.compile(r"^@한올발행\s+(.+)$")

    async def handle_command(self, event: NormalizedInboundEvent) -> None:
        if event.room != self._settings.admin_room_name:
            return
        content = event.content.strip()
        if content == "@상태":
            await self._reply(self._build_status_message())
            return
        if content == "@방목록":
            await self._reply(self._build_room_list_message())
            return
        if content == "@전송큐":
            await self._reply(self._delivery_service.queue_snapshot())
            return
        if content == "@소켓상태":
            await self._reply(f"소켓 상태: {await self._delivery_service.socket_status()}")
            return
        if content == "@진단":
            await self._reply(await self._build_diagnostic_message())
            return
        if match := self._retry_pattern.match(content):
            result = await self._delivery_service.retry_message(match.group(1))
            await self._reply(f"재전송 결과: {result.status} ({result.message_id})")
            return
        if match := self._hanall_publish_pattern.match(content):
            room_name = match.group(1).strip()
            room = self._room_registry.resolve_room(room_name)
            if room is None:
                await self._reply(f"알 수 없는 방: {room_name}")
                return
            if not room.features.get("hanall_briefing"):
                await self._reply(f"한올 브리핑 비활성 방: {room_name}")
                return
            publish_hanall_room = getattr(self._scheduler_service, "publish_hanall_room", None)
            if not callable(publish_hanall_room):
                await self._reply("한올 발행 기능을 사용할 수 없습니다.")
                return
            try:
                await publish_hanall_room(room_name, force=True)
            except Exception as exc:  # noqa: BLE001
                await self._reply(f"한올 발행 실패: {room_name} / {exc}")
                return
            await self._reply(f"한올 발행 완료: {room_name}")
            return
        if match := self._feature_query_pattern.match(content):
            room_name = match.group(1).strip()
            snapshot = self._room_registry.room_feature_snapshot(room_name)
            if snapshot is None:
                await self._reply(f"알 수 없는 방: {room_name}")
                return
            lines = [f"[기능조회] {room_name}"] + [f"- {key}: {'on' if value else 'off'}" for key, value in snapshot.items()]
            await self._reply("\n".join(lines))
            return
        if match := self._feature_pattern.match(content):
            room_name, feature_name, state = match.groups()
            if not self._room_registry.has_room(room_name):
                await self._reply(f"알 수 없는 방: {room_name}")
                return
            self._room_registry.set_feature_override(room_name, feature_name, state == "on")
            await self._reply(f"설정 완료: {room_name} / {feature_name} = {state}")
            return
        await self._reply(
            "지원 명령: @상태, @방목록, @전송큐, @소켓상태, @진단, @재전송 <id>, "
            "@한올발행 <room>, @기능조회 <room>, @기능설정 <room> <feature> on|off"
        )

    def _build_status_message(self) -> str:
        now = datetime.now(self._timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
        return f"[상태]\n시간: {now}\n전송큐: {self._delivery_service.queue_snapshot()}"

    def _build_room_list_message(self) -> str:
        lines = ["[방목록]"]
        for room in self._room_registry.list_rooms():
            enabled = [name for name, value in room.features.items() if value]
            lines.append(f"- {room.name}: {', '.join(enabled) if enabled else '활성 기능 없음'}")
        return "\n".join(lines)

    async def _build_diagnostic_message(self) -> str:
        return (
            "[진단]\n"
            f"소켓: {await self._delivery_service.socket_status()}\n"
            f"큐: {self._delivery_service.queue_snapshot()}"
        )

    async def _reply(self, text: str) -> None:
        await self._delivery_service.send_text(
            self._settings.admin_room_name,
            text,
            suppress_admin_report=True,
        )
