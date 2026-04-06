from __future__ import annotations

import httpx

from app.errors import ExternalAPIError
from app.services.youtube_service import YouTubeService


def test_youtube_summary_payload_uses_video_input_part(test_settings) -> None:
    service = YouTubeService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        delivery_service=object(),
        admin_notifier=object(),
    )

    payload = service._build_generate_content_payload(
        url="https://www.youtube.com/watch?v=abc123",
        prompt="summarize this video",
    )

    parts = payload["contents"][0]["parts"]
    assert parts[0]["file_data"]["file_uri"] == "https://www.youtube.com/watch?v=abc123"
    assert parts[0]["file_data"]["mime_type"] == "video/*"
    assert parts[1]["text"] == "summarize this video"


def test_youtube_http_error_maps_inaccessible_video() -> None:
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(403, request=request, text="private youtube video is not public")
    exc = httpx.HTTPStatusError("forbidden", request=request, response=response)

    mapped = YouTubeService._map_http_error(exc)

    assert isinstance(mapped, ExternalAPIError)
    assert "private, blocked, or inaccessible" in str(mapped)
