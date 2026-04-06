from __future__ import annotations

import re

import httpx

from app import constants
from app.config import Settings
from app.errors import ConfigurationError, ExternalAPIError
from app.schemas import EffectiveRoomConfig, NormalizedInboundEvent, PromptLibrary


class YouTubeService:
    def __init__(self, *, settings: Settings, prompts: PromptLibrary, delivery_service: object, admin_notifier: object) -> None:
        self._settings = settings
        self._prompts = prompts
        self._delivery_service = delivery_service
        self._admin_notifier = admin_notifier
        self._pattern = re.compile(constants.YOUTUBE_URL_PATTERN, re.IGNORECASE)

    def extract_youtube_urls(self, text: str) -> list[str]:
        return [match.group(1) for match in self._pattern.finditer(text)]

    async def handle_url(self, room: EffectiveRoomConfig, event: NormalizedInboundEvent, url: str) -> None:
        try:
            summary = await self.summarize_url(url)
            await self._delivery_service.send_text(
                room.name,
                summary,
                package_name=room.package_name,
                correlation_key=event.log_id,
            )
        except Exception as exc:  # noqa: BLE001
            await self._admin_notifier.notify_feature_error(
                room_name=room.name,
                feature_name="youtube_summary",
                error_message=f"{url} | {exc}",
            )

    async def summarize_url(self, url: str) -> str:
        if not self._settings.gemini_api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        template = self._prompts.youtube_summary["template"].replace("__SOURCE_LABEL__", "공개 YouTube 영상")
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self._settings.gemini_youtube_model}:generateContent"
        payload = self._build_generate_content_payload(url=url, prompt=template)
        async with httpx.AsyncClient(timeout=self._settings.gemini_timeout_seconds) as client:
            try:
                response = await client.post(
                    endpoint,
                    headers={"x-goog-api-key": self._settings.gemini_api_key},
                    json=payload,
                )
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                raise ExternalAPIError("Gemini YouTube video understanding timed out") from exc
            except httpx.HTTPStatusError as exc:
                raise self._map_http_error(exc) from exc
            except httpx.HTTPError as exc:
                raise ExternalAPIError(f"Gemini YouTube video understanding request failed: {exc}") from exc
        data = response.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "\n".join(part.get("text", "") for part in parts if part.get("text")).strip()
        if not text:
            raise ExternalAPIError("Gemini returned an empty YouTube summary")
        return text

    @staticmethod
    def _build_generate_content_payload(*, url: str, prompt: str) -> dict[str, object]:
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "file_data": {
                                "file_uri": url,
                                "mime_type": "video/*",
                            }
                        },
                        {"text": prompt},
                    ],
                }
            ]
        }

    @staticmethod
    def _map_http_error(exc: httpx.HTTPStatusError) -> ExternalAPIError:
        status_code = exc.response.status_code
        response_text = exc.response.text.lower()
        guarded_keywords = ("youtube", "video", "private", "blocked", "unavailable", "permission", "public")
        if status_code in {400, 403, 404} and any(keyword in response_text for keyword in guarded_keywords):
            return ExternalAPIError("YouTube video is private, blocked, or inaccessible for Gemini video understanding")
        if status_code in {408, 504}:
            return ExternalAPIError("Gemini YouTube video understanding timed out")
        return ExternalAPIError(f"Gemini YouTube video understanding failed with status {status_code}")
