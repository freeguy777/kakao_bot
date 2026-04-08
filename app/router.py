from __future__ import annotations

import logging
from pathlib import Path

from app.config import Settings
from app.errors import ConfigurationError
from app.repositories import FeatureOverrideRepository
from app.schemas import EffectiveRoomConfig, NormalizedInboundEvent

logger = logging.getLogger(__name__)


class RoomRegistry:
    def __init__(self, settings: Settings, override_repository: FeatureOverrideRepository) -> None:
        self._settings = settings
        self._override_repository = override_repository
        self._room_config_path = self._resolve_config_path(settings.room_config_path)
        self._room_config_signature: tuple[int, int] | None = None
        self._rooms: dict[str, object] = {}
        self._load_rooms()

    def resolve_room(self, room_name: str) -> EffectiveRoomConfig | None:
        base_room = self._rooms.get(room_name)
        if base_room is None:
            return None
        overrides = self._override_repository.get_overrides(room_name)
        features = base_room.features.model_dump()
        for feature_name, enabled in overrides.items():
            if feature_name in features:
                features[feature_name] = enabled
        persona_path = self._resolve_persona_path(base_room.persona_path)
        return EffectiveRoomConfig(
            name=base_room.name,
            admin=base_room.admin,
            package_name=base_room.package_name or self._settings.default_package_name,
            persona_path=persona_path,
            features=features,
            hanall_publish_time=base_room.hanall_publish_time,
            weather=base_room.weather,
        )

    def list_rooms(self) -> list[EffectiveRoomConfig]:
        return [room for room_name in self._rooms for room in [self.resolve_room(room_name)] if room is not None]

    def has_room(self, room_name: str) -> bool:
        return room_name in self._rooms

    def reload_if_config_changed(self) -> bool:
        try:
            signature = self._room_config_signature_for_file()
        except Exception:  # noqa: BLE001
            logger.exception("room_config_signature_failed", extra={"path": str(self._room_config_path)})
            return False
        if signature == self._room_config_signature:
            return False
        try:
            self._load_rooms(expected_signature=signature)
        except Exception:  # noqa: BLE001
            logger.exception("room_config_reload_failed", extra={"path": str(self._room_config_path)})
            return False
        return True

    def load_persona_text(self, room_name: str) -> str:
        room = self.resolve_room(room_name)
        if room is None or room.persona_path is None:
            return ""
        path = room.persona_path
        if not path.exists():
            logger.warning("persona_file_missing", extra={"room": room_name, "path": str(path)})
            return ""
        return path.read_text(encoding="utf-8").strip()

    def set_feature_override(self, room_name: str, feature_name: str, enabled: bool) -> None:
        self._override_repository.set_override(room_name, feature_name, enabled)

    def room_feature_snapshot(self, room_name: str) -> dict[str, bool] | None:
        room = self.resolve_room(room_name)
        if room is None:
            return None
        return room.features

    def _resolve_persona_path(self, persona_path: Path | None) -> Path | None:
        if persona_path is None:
            return None
        if persona_path.is_absolute():
            return persona_path
        return Path.cwd() / persona_path

    def _resolve_config_path(self, path: Path) -> Path:
        if path.is_absolute():
            return path
        return Path.cwd() / path

    def _load_rooms(self, *, expected_signature: tuple[int, int] | None = None) -> None:
        loaded = self._settings.load_rooms().rooms
        self._rooms = {room.name: room for room in loaded}
        self._room_config_signature = expected_signature or self._room_config_signature_for_file()

    def _room_config_signature_for_file(self) -> tuple[int, int]:
        if not self._room_config_path.exists():
            raise ConfigurationError(f"Config file not found: {self._room_config_path}")
        stat_result = self._room_config_path.stat()
        return (stat_result.st_mtime_ns, stat_result.st_size)


class MessageRouter:
    def __init__(
        self,
        *,
        settings: Settings,
        room_registry: RoomRegistry,
        admin_command_service: object,
        chat_service: object,
        youtube_service: object,
        weather_service: object,
    ) -> None:
        self._settings = settings
        self._room_registry = room_registry
        self._admin_command_service = admin_command_service
        self._chat_service = chat_service
        self._youtube_service = youtube_service
        self._weather_service = weather_service

    async def handle_event(self, event: NormalizedInboundEvent) -> None:
        if event.room == self._settings.admin_room_name and event.content.startswith("@"):
            await self._admin_command_service.handle_command(event)
            return

        room = self._room_registry.resolve_room(event.room)
        if room is None:
            logger.info("room_not_configured", extra={"room": event.room})
            return

        if event.content.startswith("@날씨") and room.features.get("weather"):
            await self._weather_service.handle_on_demand(room)
            return

        if event.content.startswith("!") and room.features.get("llm_chat"):
            await self._chat_service.handle_message(room, event)
            return

        if room.features.get("youtube_summary"):
            for url in self._youtube_service.extract_youtube_urls(event.content):
                await self._youtube_service.handle_url(room, event, url)

    @property
    def room_registry(self) -> RoomRegistry:
        return self._room_registry
