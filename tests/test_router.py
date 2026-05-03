from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from app import constants
from app.schemas import NormalizedInboundEvent


async def test_router_routes_chat(app) -> None:
    router = app.state.message_router
    router._chat_service.handle_message = AsyncMock()
    router._youtube_service.handle_url = AsyncMock()
    event = NormalizedInboundEvent(
        room="테스트하는방방방",
        content="!요약해줘",
        log_id="chat-1",
        package_name="com.kakao.talk",
        server_received_at=datetime.now(timezone.utc),
    )

    await router.handle_event(event)
    router._chat_service.handle_message.assert_awaited_once()
    router._youtube_service.handle_url.assert_not_called()


async def test_router_routes_youtube(app) -> None:
    router = app.state.message_router
    router._youtube_service.handle_url = AsyncMock()
    event = NormalizedInboundEvent(
        room="테스트하는방방방",
        content="https://youtu.be/abcdefghijk",
        log_id="yt-1",
        package_name="com.kakao.talk",
        server_received_at=datetime.now(timezone.utc),
    )

    await router.handle_event(event)
    router._youtube_service.handle_url.assert_awaited_once()


async def test_admin_command_works_only_in_admin_room(app) -> None:
    service = app.state.admin_command_service
    service._reply = AsyncMock()

    admin_event = NormalizedInboundEvent(
        room="김휘태",
        content="@기능조회 테스트하는방방방",
        log_id="admin-1",
        package_name="com.kakao.talk",
        server_received_at=datetime.now(timezone.utc),
    )
    await service.handle_command(admin_event)
    service._reply.assert_awaited_once()

    service._reply.reset_mock()
    normal_event = NormalizedInboundEvent(
        room="테스트방방방",
        content="@기능조회 테스트하는방방방",
        log_id="admin-2",
        package_name="com.kakao.talk",
        server_received_at=datetime.now(timezone.utc),
    )
    await service.handle_command(normal_event)
    service._reply.assert_not_called()


async def test_admin_command_can_force_publish_hanall_room(app) -> None:
    service = app.state.admin_command_service
    service._reply = AsyncMock()
    service._scheduler_service.publish_hanall_room = AsyncMock()

    event = NormalizedInboundEvent(
        room="김휘태",
        content="@한올발행 테스트하는방방방",
        log_id="admin-publish-1",
        package_name="com.kakao.talk",
        server_received_at=datetime.now(timezone.utc),
    )

    await service.handle_command(event)

    service._scheduler_service.publish_hanall_room.assert_awaited_once_with("테스트하는방방방", force=True)
    service._reply.assert_awaited_once_with("한올 발행 완료: 테스트하는방방방")


def test_admin_command_catalog_includes_supported_command_prefixes() -> None:
    assert {"@재전송", "@한올발행", "@기능조회", "@기능설정"}.issubset(constants.ADMIN_COMMANDS)
