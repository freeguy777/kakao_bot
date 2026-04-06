from __future__ import annotations

import httpx

from app.config import Settings
from app.errors import ConfigurationError, ExternalAPIError
from app.schemas import EffectiveRoomConfig, NormalizedInboundEvent, PromptLibrary


class ChatService:
    def __init__(self, *, settings: Settings, prompts: PromptLibrary, room_registry: object, delivery_service: object, admin_notifier: object) -> None:
        self._settings = settings
        self._prompts = prompts
        self._room_registry = room_registry
        self._delivery_service = delivery_service
        self._admin_notifier = admin_notifier

    async def handle_message(self, room: EffectiveRoomConfig, event: NormalizedInboundEvent) -> None:
        user_text = event.content[1:].strip()
        if not user_text:
            return
        try:
            reply = await self.generate_reply(room.name, user_text)
            await self._delivery_service.send_text(room.name, reply, package_name=room.package_name)
        except Exception as exc:  # noqa: BLE001
            await self._admin_notifier.notify_feature_error(room_name=room.name, feature_name="llm_chat", error_message=str(exc))

    async def generate_reply(self, room_name: str, user_text: str) -> str:
        if not self._settings.gemini_api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        persona = self._room_registry.load_persona_text(room_name)
        prompt = (
            f"{self._prompts.chat_default_system}\n\n"
            f"[방 페르소나]\n{persona or '기본 페르소나 사용'}\n\n"
            f"[사용자 입력]\n{user_text}"
        )
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._settings.gemini_chat_model}:generateContent"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        async with httpx.AsyncClient(timeout=self._settings.gemini_timeout_seconds) as client:
            response = await client.post(url, params={"key": self._settings.gemini_api_key}, json=payload)
            response.raise_for_status()
        data = response.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "\n".join(part.get("text", "") for part in parts if part.get("text")).strip()
        if not text:
            raise ExternalAPIError("Gemini returned an empty response")
        return text
