from __future__ import annotations

import httpx
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.errors import ConfigurationError, ExternalAPIError
from app.services.youtube_service import TranscriptBundle, TranscriptSegment, YouTubeRoutingTrace, YouTubeService


class FakeFetchedTranscript:
    def __init__(self, snippets, *, language_code: str | None = "ko", is_generated: bool = False) -> None:
        self._snippets = list(snippets)
        self.language_code = language_code
        self.is_generated = is_generated

    def __iter__(self):
        return iter(self._snippets)


class FakeGeminiResponse:
    def __init__(self, *, status_code: int = 200, payload: dict[str, object] | None = None, text: str = "") -> None:
        self._status_code = status_code
        self._payload = payload or {}
        self._text = text

    def raise_for_status(self) -> None:
        if self._status_code < 400:
            return None
        request = httpx.Request("POST", "https://example.com")
        response = httpx.Response(self._status_code, request=request, text=self._text)
        raise httpx.HTTPStatusError("server error", request=request, response=response)

    def json(self) -> dict[str, object]:
        return self._payload


def build_service(test_settings) -> YouTubeService:
    return YouTubeService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        delivery_service=object(),
        admin_notifier=object(),
    )


def test_youtube_summary_payload_uses_video_input_part(test_settings) -> None:
    service = build_service(test_settings)

    payload = service._build_generate_content_payload(
        url="https://www.youtube.com/watch?v=abc123",
        prompt="summarize this video",
    )

    parts = payload["contents"][0]["parts"]
    assert parts[0]["file_data"]["file_uri"] == "https://www.youtube.com/watch?v=abc123"
    assert parts[0]["file_data"]["mime_type"] == "video/*"
    assert parts[1]["text"] == "summarize this video"


async def test_summarize_video_url_normalizes_shorts_url_before_direct_video_request(test_settings) -> None:
    service = build_service(test_settings)
    captured: dict[str, object] = {}

    async def fake_request_summary(*, endpoint: str, payload: dict[str, object], error_label: str, **kwargs: object) -> str:
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["error_label"] = error_label
        return "summary"

    service._request_summary = fake_request_summary  # type: ignore[method-assign]

    result = await service._summarize_video_url("https://youtube.com/shorts/EthTT3ys7ew?si=73u8byA2NbTVwGbC")

    assert result == "summary"
    parts = captured["payload"]["contents"][0]["parts"]  # type: ignore[index]
    assert parts[0]["file_data"]["file_uri"] == "https://www.youtube.com/watch?v=EthTT3ys7ew"
    assert captured["error_label"] == "Gemini YouTube video understanding"


def test_youtube_http_error_maps_inaccessible_video() -> None:
    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(403, request=request, text="private youtube video is not public")
    exc = httpx.HTTPStatusError("forbidden", request=request, response=response)

    mapped = YouTubeService._map_http_error(exc, "Gemini YouTube video understanding")

    assert isinstance(mapped, ExternalAPIError)
    assert "private, blocked, or inaccessible" in str(mapped)


def test_generic_summary_response_matches_insufficient_info_fallback(test_settings) -> None:
    service = build_service(test_settings)

    assert service._is_generic_summary_response(
        "정보가 부족하여 영상 내용을 요약할 수 없습니다. "
        "영상을 분석할 수 있는 구체적인 내용(대본 또는 주요 내용 설명)을 제공해주시면 "
        "요청하신 형식으로 핵심을 정리해 드리겠습니다."
    )


def test_generic_summary_response_does_not_match_structured_summary(test_settings) -> None:
    service = build_service(test_settings)

    assert not service._is_generic_summary_response(
        "◆ 한줄 요약: 시장 금리 급등이 기술주 변동성을 키웠습니다.\n\n"
        "• 주요 포인트\n"
        "- 금리 상승 (00:12): 성장주 밸류에이션 부담이 커졌습니다.\n"
        "- 기술주 약세 (01:04): 대형주 중심으로 차익 실현이 나왔습니다.\n"
        "- 자금 이동 (02:10): 방어주와 현금 비중 확대가 관찰됩니다.\n\n"
        "• 인사이트\n"
        "- 단기 반등보다 금리 방향 확인이 우선입니다."
    )


async def test_request_summary_retries_once_after_503_and_succeeds(test_settings, monkeypatch) -> None:
    service = build_service(test_settings)
    responses = [
        FakeGeminiResponse(status_code=503, text="service unavailable"),
        FakeGeminiResponse(
            payload={"candidates": [{"content": {"parts": [{"text": "retried summary"}]}}]},
        ),
    ]
    captured_calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self._responses = list(responses)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, headers: dict[str, str], json: dict[str, object]):
            captured_calls.append({"url": url, "headers": headers, "json": json})
            return self._responses.pop(0)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.services.youtube_service.asyncio.sleep", fake_sleep)

    summary = await service._request_summary(
        endpoint="https://example.com/generate",
        payload={"contents": []},
        error_label="Gemini YouTube video understanding",
    )

    assert summary == "retried summary"
    assert len(captured_calls) == 2
    assert sleep_calls == [2.0]


async def test_request_summary_retries_once_after_503_then_raises(test_settings, monkeypatch) -> None:
    service = build_service(test_settings)
    responses = [
        FakeGeminiResponse(status_code=503, text="service unavailable"),
        FakeGeminiResponse(status_code=503, text="service unavailable"),
    ]
    captured_calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self._responses = list(responses)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, headers: dict[str, str], json: dict[str, object]):
            captured_calls.append({"url": url, "headers": headers, "json": json})
            return self._responses.pop(0)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.services.youtube_service.asyncio.sleep", fake_sleep)

    with pytest.raises(ExternalAPIError, match=r"failed with status 503"):
        await service._request_summary(
            endpoint="https://example.com/generate",
            payload={"contents": []},
            error_label="Gemini YouTube video understanding",
        )

    assert len(captured_calls) == 2
    assert sleep_calls == [2.0]


async def test_summarize_transcript_uses_default_prompt_and_flash_model(test_settings) -> None:
    service = build_service(test_settings)
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="먼저 핵심 내용을 설명하겠습니다"),
            TranscriptSegment(start_ms=1000, text="정리하면 오늘 영상의 중요한 포인트입니다"),
        ],
    )
    captured: dict[str, object] = {}

    async def fake_request_summary(*, endpoint: str, payload: dict[str, object], error_label: str, **kwargs: object) -> str:
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["error_label"] = error_label
        return "summary"

    service._request_summary = fake_request_summary  # type: ignore[method-assign]

    summary = await service._summarize_transcript("https://youtu.be/abc123xyz00", transcript)

    assert summary == "summary"
    assert captured["endpoint"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert captured["error_label"] == "Gemini transcript summarization"
    prompt = captured["payload"]["contents"][0]["parts"][0]["text"]  # type: ignore[index]
    assert "단 1분 만에 파악할 수 있도록 핵심만 정리해주는 '프로 요약러'" in prompt
    assert "공개 YouTube 영상 자막" in prompt
    assert "# Transcript" in prompt


async def test_summarize_transcript_uses_lite_prompt_when_lite_model_is_selected(test_settings) -> None:
    test_settings.gemini_youtube_transcript_model = "gemini-2.5-flash-lite"
    service = build_service(test_settings)
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[TranscriptSegment(start_ms=0, text="먼저 핵심 내용을 설명하겠습니다")],
    )
    captured: dict[str, object] = {}

    async def fake_request_summary(*, endpoint: str, payload: dict[str, object], error_label: str, **kwargs: object) -> str:
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["error_label"] = error_label
        return "summary"

    service._request_summary = fake_request_summary  # type: ignore[method-assign]

    summary = await service._summarize_transcript("https://youtu.be/abc123xyz00", transcript)

    assert summary == "summary"
    assert captured["endpoint"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
    prompt = captured["payload"]["contents"][0]["parts"][0]["text"]  # type: ignore[index]
    assert "빠르고 핵심만 정리해주는 프로 요약러" in prompt


async def test_summarize_transcript_falls_back_to_lite_model_after_503(test_settings) -> None:
    service = build_service(test_settings)
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="먼저 핵심 내용을 설명하겠습니다"),
            TranscriptSegment(start_ms=1000, text="정리하면 오늘 영상의 중요한 포인트입니다"),
        ],
    )
    calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    async def fake_request_summary(*, endpoint: str, payload: dict[str, object], error_label: str, **kwargs: object) -> str:
        calls.append({"endpoint": endpoint, "payload": payload, "error_label": error_label})
        if len(calls) == 1:
            raise ExternalAPIError("Gemini transcript summarization failed with status 503", status_code=503)
        return "fallback summary"

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    service._request_summary = fake_request_summary  # type: ignore[method-assign]
    original_sleep = service._summarize_transcript.__globals__["asyncio"].sleep
    service._summarize_transcript.__globals__["asyncio"].sleep = fake_sleep

    try:
        summary = await service._summarize_transcript("https://youtu.be/abc123xyz00", transcript)
    finally:
        service._summarize_transcript.__globals__["asyncio"].sleep = original_sleep

    assert summary == "fallback summary"
    assert len(calls) == 2
    assert sleep_calls == [1.0]
    assert calls[0]["endpoint"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert calls[0]["error_label"] == "Gemini transcript summarization"
    assert calls[1]["endpoint"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
    assert calls[1]["error_label"] == "Gemini transcript summarization fallback (gemini-2.5-flash-lite)"


async def test_summarize_transcript_falls_back_to_lite_model_after_429(test_settings) -> None:
    service = build_service(test_settings)
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[TranscriptSegment(start_ms=0, text="먼저 핵심 내용을 설명하겠습니다")],
    )
    calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    async def fake_request_summary(*, endpoint: str, payload: dict[str, object], error_label: str, **kwargs: object) -> str:
        calls.append({"endpoint": endpoint, "payload": payload, "error_label": error_label})
        if len(calls) == 1:
            raise ExternalAPIError("Gemini transcript summarization failed with status 429", status_code=429)
        return "fallback summary"

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    service._request_summary = fake_request_summary  # type: ignore[method-assign]
    original_sleep = service._summarize_transcript.__globals__["asyncio"].sleep
    service._summarize_transcript.__globals__["asyncio"].sleep = fake_sleep

    try:
        summary = await service._summarize_transcript("https://youtu.be/abc123xyz00", transcript)
    finally:
        service._summarize_transcript.__globals__["asyncio"].sleep = original_sleep

    assert summary == "fallback summary"
    assert len(calls) == 2
    assert sleep_calls == [1.0]
    assert calls[1]["endpoint"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
    assert calls[1]["error_label"] == "Gemini transcript summarization fallback (gemini-2.5-flash-lite)"


async def test_summarize_transcript_uses_video_model_as_fallback_when_primary_and_fallback_match(test_settings) -> None:
    test_settings.gemini_youtube_transcript_model = "gemini-2.5-flash"
    test_settings.gemini_youtube_transcript_fallback_model = "gemini-2.5-flash"
    test_settings.gemini_youtube_model = "gemini-2.5-flash-lite"
    service = build_service(test_settings)
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[TranscriptSegment(start_ms=0, text="먼저 핵심 내용을 설명하겠습니다")],
    )
    calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    async def fake_request_summary(*, endpoint: str, payload: dict[str, object], error_label: str, **kwargs: object) -> str:
        calls.append({"endpoint": endpoint, "payload": payload, "error_label": error_label})
        if len(calls) == 1:
            raise ExternalAPIError("Gemini transcript summarization failed with status 503", status_code=503)
        return "fallback summary"

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    service._request_summary = fake_request_summary  # type: ignore[method-assign]
    original_sleep = service._summarize_transcript.__globals__["asyncio"].sleep
    service._summarize_transcript.__globals__["asyncio"].sleep = fake_sleep

    try:
        summary = await service._summarize_transcript("https://youtu.be/abc123xyz00", transcript)
    finally:
        service._summarize_transcript.__globals__["asyncio"].sleep = original_sleep

    assert summary == "fallback summary"
    assert len(calls) == 2
    assert sleep_calls == [1.0]
    assert calls[0]["endpoint"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    assert calls[1]["endpoint"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"


def test_extract_video_id_supports_common_single_video_urls(test_settings) -> None:
    service = build_service(test_settings)

    assert service._extract_video_id("https://www.youtube.com/watch?v=abc123xyz00") == "abc123xyz00"
    assert service._extract_video_id("https://youtu.be/abc123xyz00?si=test") == "abc123xyz00"
    assert service._extract_video_id("https://www.youtube.com/shorts/abc123xyz00") == "abc123xyz00"
    assert service._extract_video_id("https://www.youtube.com/live/abc123xyz00?feature=share") == "abc123xyz00"


def test_extract_youtube_urls_supports_mobile_music_live_and_embed_urls(test_settings) -> None:
    service = build_service(test_settings)
    text = "\n".join(
        [
            "https://m.youtube.com/watch?v=9lVpPpUsibQ&t=3s&pp=2AEDkAIB",
            "https://music.youtube.com/watch?v=abc123xyz00&si=test",
            "https://www.youtube.com/live/abc123xyz00?feature=share",
            "https://www.youtube.com/embed/abc123xyz00?start=30",
            "https://youtu.be/abc123xyz00?si=test",
        ]
    )

    urls = service.extract_youtube_urls(text)

    assert urls == [
        "https://m.youtube.com/watch?v=9lVpPpUsibQ&t=3s&pp=2AEDkAIB",
        "https://music.youtube.com/watch?v=abc123xyz00&si=test",
        "https://www.youtube.com/live/abc123xyz00?feature=share",
        "https://www.youtube.com/embed/abc123xyz00?start=30",
        "https://youtu.be/abc123xyz00?si=test",
    ]


def test_extract_youtube_urls_skips_unsupported_non_video_urls(test_settings) -> None:
    service = build_service(test_settings)
    text = "\n".join(
        [
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/clip/Ugkxyz123",
            "https://www.youtube.com/@channelname",
        ]
    )

    assert service.extract_youtube_urls(text) == []


def test_build_transcript_bundle_uses_api_snippets_and_metadata(test_settings) -> None:
    service = build_service(test_settings)
    fetched = FakeFetchedTranscript(
        [
            SimpleNamespace(start=0.0, duration=1.0, text="첫 문장"),
            SimpleNamespace(start=1.5, duration=2.0, text="둘째 문장"),
        ],
        language_code="ko",
        is_generated=True,
    )

    transcript = service._build_transcript_bundle(fetched)

    assert transcript is not None
    assert transcript.language_code == "ko"
    assert transcript.is_auto_generated is True
    assert transcript.estimated_duration_ms == 3500
    assert transcript.segments == [
        TranscriptSegment(start_ms=0, text="첫 문장"),
        TranscriptSegment(start_ms=1500, text="둘째 문장"),
    ]


def test_build_transcript_bundle_discards_noise_only_snippets(test_settings) -> None:
    service = build_service(test_settings)
    fetched = FakeFetchedTranscript(
        [
            SimpleNamespace(start=0.0, text="[Music]"),
            SimpleNamespace(start=1.0, text="   "),
        ],
        language_code="ko",
        is_generated=False,
    )

    assert service._build_transcript_bundle(fetched) is None


def test_classify_transcript_locally_marks_sound_effect_only(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="♪ instrumental ♪"),
            TranscriptSegment(start_ms=1000, text="applause"),
            TranscriptSegment(start_ms=2000, text="웃음"),
        ],
    )

    assert service._classify_transcript_locally(transcript) == "sound_effect_only"


def test_classify_transcript_locally_marks_informative(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="먼저 오늘 업데이트 핵심 내용을 설명하겠습니다"),
            TranscriptSegment(start_ms=1000, text="정리하면 배터리 효율이 이전보다 개선됐습니다"),
        ],
    )

    assert service._classify_transcript_locally(transcript) == "informative"


def test_classify_transcript_locally_marks_lyrics_with_marker(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="chorus"),
            TranscriptSegment(start_ms=1000, text="♪ stay with me tonight ♪"),
        ],
    )

    assert service._classify_transcript_locally(transcript) == "lyrics"


def test_classify_transcript_locally_marks_short_fragments_as_garbled_or_unclear(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    transcript = TranscriptBundle(
        language_code="en",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="Heat"),
            TranscriptSegment(start_ms=1000, text="Heat"),
        ],
    )

    assert service._classify_transcript_locally(transcript) == "garbled_or_unclear"


async def test_try_fetch_transcript_marks_video_id_missing(test_settings) -> None:
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()

    transcript = await service._try_fetch_transcript("https://example.com/not-youtube", trace=trace)

    assert transcript is None
    assert trace.transcript_status == "video_id_missing"


async def test_try_fetch_transcript_marks_client_unavailable(test_settings) -> None:
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    service._fetch_transcript_bundle_sync = lambda video_id: (_ for _ in ()).throw(
        ConfigurationError("youtube-transcript-api is not installed")
    )

    transcript = await service._try_fetch_transcript("https://youtu.be/abc123xyz00", trace=trace)

    assert transcript is None
    assert trace.transcript_status == "transcript_client_unavailable"
    assert "youtube-transcript-api is not installed" in (trace.transcript_fetch_error or "")


async def test_try_fetch_transcript_returns_bundle_from_api(test_settings) -> None:
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript_bundle = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[TranscriptSegment(start_ms=0, text="충분히 긴 자막 내용입니다" * 10)],
    )
    service._fetch_transcript_bundle_sync = lambda video_id: transcript_bundle

    transcript = await service._try_fetch_transcript("https://youtu.be/abc123xyz00", trace=trace)

    assert transcript is transcript_bundle
    assert trace.transcript_status == "transcript_ready"


async def test_summarize_url_uses_transcript_fast_path_when_transcript_is_informative(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(
                start_ms=index * 1000,
                text=f"먼저 항목 {index}의 핵심 내용을 설명하겠습니다",
            )
            for index in range(30)
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._summarize_transcript = AsyncMock(return_value="fast summary")
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://youtu.be/abc123xyz00", trace=trace)

    assert result == "fast summary"
    assert trace.selected_path == "transcript_fast_path"
    assert trace.route_reason == "transcript_informative"
    assert trace.route_source == "local"
    assert trace.transcript_status is None
    assert trace.transcript_language_code == "ko"
    service._summarize_transcript.assert_awaited_once()
    service._summarize_video_url.assert_not_awaited()


async def test_summarize_url_uses_transcript_fast_path_for_shorts_when_transcript_is_informative(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(
                start_ms=index * 1000,
                text=f"정리하면 쇼츠 장면 {index}의 의미를 설명하겠습니다",
            )
            for index in range(30)
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._summarize_transcript = AsyncMock(return_value="shorts fast summary")
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://www.youtube.com/shorts/abc123xyz00", trace=trace)

    assert result == "shorts fast summary"
    assert trace.selected_path == "transcript_fast_path"
    assert trace.route_reason == "transcript_informative"
    assert trace.route_source == "local"
    service._summarize_transcript.assert_awaited_once()
    service._summarize_video_url.assert_not_awaited()


async def test_summarize_url_uses_classifier_when_local_route_is_ambiguous(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="바람이 불어오고 네가 서 있던 거리"),
            TranscriptSegment(start_ms=1000, text="오래된 기억이 천천히 다시 떠오른다"),
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._classify_transcript_with_model = AsyncMock(return_value="informative")
    service._summarize_transcript = AsyncMock(return_value="classified fast summary")
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://youtu.be/abc123xyz00", trace=trace)

    assert result == "classified fast summary"
    assert trace.selected_path == "transcript_fast_path"
    assert trace.route_reason == "transcript_informative"
    assert trace.route_source == "classifier"
    service._classify_transcript_with_model.assert_awaited_once()
    service._summarize_transcript.assert_awaited_once()
    service._summarize_video_url.assert_not_awaited()


async def test_summarize_url_falls_back_to_video_when_local_route_is_lyrics(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="chorus"),
            TranscriptSegment(start_ms=1000, text="♪ stay with me tonight ♪"),
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._classify_transcript_with_model = AsyncMock(return_value="informative")
    service._summarize_transcript = AsyncMock(return_value="fast summary")
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://youtu.be/abc123xyz00", trace=trace)

    assert result == "slow summary"
    assert trace.selected_path == "video_understanding"
    assert trace.route_reason == "transcript_lyrics"
    assert trace.route_source == "local"
    service._classify_transcript_with_model.assert_not_awaited()
    service._summarize_transcript.assert_not_awaited()
    service._summarize_video_url.assert_awaited_once()


async def test_summarize_url_falls_back_to_video_for_short_fragmentary_transcript_without_classifier(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript = TranscriptBundle(
        language_code="en",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="Heat"),
            TranscriptSegment(start_ms=1000, text="Heat"),
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._classify_transcript_with_model = AsyncMock(return_value="informative")
    service._summarize_transcript = AsyncMock(return_value="fast summary")
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://www.youtube.com/shorts/EthTT3ys7ew", trace=trace)

    assert result == "slow summary"
    assert trace.selected_path == "video_understanding"
    assert trace.route_reason == "transcript_garbled_or_unclear"
    assert trace.route_source == "local"
    service._classify_transcript_with_model.assert_not_awaited()
    service._summarize_transcript.assert_not_awaited()
    service._summarize_video_url.assert_awaited_once()


async def test_summarize_url_falls_back_to_video_when_classifier_fails(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(start_ms=0, text="바람이 불어오고 네가 서 있던 거리"),
            TranscriptSegment(start_ms=1000, text="오래된 기억이 천천히 다시 떠오른다"),
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._classify_transcript_with_model = AsyncMock(
        side_effect=ExternalAPIError("Gemini transcript routing classification timed out")
    )
    service._summarize_transcript = AsyncMock(return_value="fast summary")
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://youtu.be/abc123xyz00", trace=trace)

    assert result == "slow summary"
    assert trace.selected_path == "video_understanding"
    assert trace.route_reason == "transcript_classifier_failed"
    assert trace.route_source == "classifier"
    assert trace.transcript_classifier_error == "Gemini transcript routing classification timed out"
    service._summarize_transcript.assert_not_awaited()
    service._summarize_video_url.assert_awaited_once()


async def test_summarize_url_uses_transcript_fast_path_for_long_transcript_when_classifier_fails(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        estimated_duration_ms=301000,
        segments=[
            TranscriptSegment(start_ms=0, text="바람이 불어오고 네가 서 있던 거리"),
            TranscriptSegment(start_ms=280000, text="오래된 기억이 천천히 다시 떠오른다"),
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._classify_transcript_with_model = AsyncMock(
        side_effect=ExternalAPIError("Gemini transcript routing classification timed out")
    )
    service._summarize_transcript = AsyncMock(return_value="forced fast summary")
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://youtu.be/abc123xyz00", trace=trace)

    assert result == "forced fast summary"
    assert trace.selected_path == "transcript_fast_path"
    assert trace.route_reason == "transcript_long_form_override"
    assert trace.route_source == "duration_rule"
    assert trace.transcript_classifier_error == "Gemini transcript routing classification timed out"
    service._summarize_transcript.assert_awaited_once()
    service._summarize_video_url.assert_not_awaited()


async def test_summarize_url_keeps_video_fallback_for_long_lyrics_transcript(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        estimated_duration_ms=301000,
        segments=[
            TranscriptSegment(start_ms=0, text="chorus"),
            TranscriptSegment(start_ms=280000, text="♪ stay with me tonight ♪"),
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._classify_transcript_with_model = AsyncMock(return_value="informative")
    service._summarize_transcript = AsyncMock(return_value="fast summary")
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://youtu.be/abc123xyz00", trace=trace)

    assert result == "slow summary"
    assert trace.selected_path == "video_understanding"
    assert trace.route_reason == "transcript_lyrics"
    assert trace.route_source == "local"
    service._classify_transcript_with_model.assert_not_awaited()
    service._summarize_transcript.assert_not_awaited()
    service._summarize_video_url.assert_awaited_once()


async def test_summarize_url_falls_back_to_video_when_transcript_is_missing(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    service._try_fetch_transcript = AsyncMock(return_value=None)
    service._summarize_transcript = AsyncMock(return_value="fast summary")
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://youtu.be/abc123xyz00", trace=trace)

    assert result == "slow summary"
    assert trace.selected_path == "video_understanding"
    assert trace.route_reason == "transcript_unavailable"
    service._summarize_transcript.assert_not_awaited()
    service._summarize_video_url.assert_awaited_once()


async def test_summarize_url_falls_back_to_video_when_fast_path_raises(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    service = build_service(test_settings)
    trace = YouTubeRoutingTrace()
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(
                start_ms=index * 1000,
                text=f"먼저 자막 {index}의 핵심 맥락을 설명하겠습니다",
            )
            for index in range(30)
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._summarize_transcript = AsyncMock(side_effect=RuntimeError("boom"))
    service._summarize_video_url = AsyncMock(return_value="slow summary")

    result = await service.summarize_url("https://youtu.be/abc123xyz00", trace=trace)

    assert result == "slow summary"
    assert trace.selected_path == "video_understanding"
    assert trace.route_reason == "transcript_summary_failed"
    assert trace.transcript_summary_error == "boom"
    service._summarize_transcript.assert_awaited_once()
    service._summarize_video_url.assert_awaited_once()


async def test_handle_url_notifies_admin_with_routing_context_on_video_timeout(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    delivery_service = SimpleNamespace(send_text=AsyncMock())
    admin_notifier = SimpleNamespace(notify_feature_error=AsyncMock())
    service = YouTubeService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        delivery_service=delivery_service,
        admin_notifier=admin_notifier,
    )
    service._try_fetch_transcript = AsyncMock(return_value=None)
    service._summarize_video_url = AsyncMock(side_effect=ExternalAPIError("Gemini YouTube video understanding timed out"))

    await service.handle_url(
        SimpleNamespace(name="테스트하는방방방", package_name="com.kakao.talk"),
        SimpleNamespace(log_id="log-123"),
        "https://youtu.be/abc123xyz00",
    )

    admin_notifier.notify_feature_error.assert_awaited_once()
    kwargs = admin_notifier.notify_feature_error.await_args.kwargs
    assert kwargs["room_name"] == "테스트하는방방방"
    assert "Gemini YouTube video understanding timed out" in kwargs["error_message"]
    assert "routing: dynamic=on, path=video_understanding, reason=transcript_unavailable" in kwargs["error_message"]


async def test_handle_url_suppresses_generic_summary_and_notifies_admin_only(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    delivery_service = SimpleNamespace(send_text=AsyncMock())
    admin_notifier = SimpleNamespace(notify_feature_error=AsyncMock())
    service = YouTubeService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        delivery_service=delivery_service,
        admin_notifier=admin_notifier,
    )
    service.summarize_url = AsyncMock(
        return_value=(
            "정보가 부족하여 영상 내용을 요약할 수 없습니다. "
            "영상을 분석할 수 있는 구체적인 내용(대본 또는 주요 내용 설명)을 제공해주시면 "
            "요청하신 형식으로 핵심을 정리해 드리겠습니다."
        )
    )

    await service.handle_url(
        SimpleNamespace(name="테스트하는방방방", package_name="com.kakao.talk"),
        SimpleNamespace(log_id="log-123"),
        "https://youtu.be/abc123xyz00",
    )

    delivery_service.send_text.assert_not_awaited()
    admin_notifier.notify_feature_error.assert_awaited_once()
    kwargs = admin_notifier.notify_feature_error.await_args.kwargs
    assert kwargs["room_name"] == "테스트하는방방방"
    assert "일반방 전송 차단: 유튜브 요약 generic 응답" in kwargs["error_message"]
    assert "정보가 부족하여 영상 내용을 요약할 수 없습니다." in kwargs["error_message"]


async def test_handle_url_notifies_admin_when_transcript_summary_falls_back_then_video_times_out(test_settings) -> None:
    test_settings.youtube_dynamic_routing_enabled = True
    delivery_service = SimpleNamespace(send_text=AsyncMock())
    admin_notifier = SimpleNamespace(notify_feature_error=AsyncMock())
    service = YouTubeService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        delivery_service=delivery_service,
        admin_notifier=admin_notifier,
    )
    transcript = TranscriptBundle(
        language_code="ko",
        is_auto_generated=False,
        segments=[
            TranscriptSegment(
                start_ms=index * 1000,
                text=f"정리하면 자막 {index}의 핵심 내용을 설명하겠습니다",
            )
            for index in range(30)
        ],
    )
    service._try_fetch_transcript = AsyncMock(return_value=transcript)
    service._summarize_transcript = AsyncMock(side_effect=RuntimeError("boom"))
    service._summarize_video_url = AsyncMock(side_effect=ExternalAPIError("Gemini YouTube video understanding timed out"))

    await service.handle_url(
        SimpleNamespace(name="테스트하는방방방", package_name="com.kakao.talk"),
        SimpleNamespace(log_id="log-123"),
        "https://youtu.be/abc123xyz00",
    )

    admin_notifier.notify_feature_error.assert_awaited_once()
    kwargs = admin_notifier.notify_feature_error.await_args.kwargs
    assert "routing: dynamic=on, path=video_understanding, reason=transcript_summary_failed" in kwargs["error_message"]
    assert "transcript_error=boom" in kwargs["error_message"]
