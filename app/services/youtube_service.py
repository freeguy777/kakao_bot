from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

from app import constants
from app.config import Settings
from app.errors import ConfigurationError, ExternalAPIError
from app.schemas import EffectiveRoomConfig, NormalizedInboundEvent, PromptLibrary

try:
    from youtube_transcript_api import (
        CouldNotRetrieveTranscript,
        NoTranscriptFound,
        TranscriptsDisabled,
        YouTubeTranscriptApi,
        YouTubeTranscriptApiException,
    )
except ModuleNotFoundError:
    CouldNotRetrieveTranscript = None
    NoTranscriptFound = None
    TranscriptsDisabled = None
    YouTubeTranscriptApi = None
    YouTubeTranscriptApiException = None

logger = logging.getLogger(__name__)

_TRANSCRIPT_UNAVAILABLE_EXCEPTIONS = tuple(
    exc for exc in (CouldNotRetrieveTranscript, NoTranscriptFound, TranscriptsDisabled) if isinstance(exc, type)
)
_TRANSCRIPT_API_EXCEPTIONS = tuple(exc for exc in (YouTubeTranscriptApiException,) if isinstance(exc, type))


@dataclass(slots=True)
class TranscriptSegment:
    start_ms: int
    text: str

    @property
    def timestamp_label(self) -> str:
        total_seconds = max(0, self.start_ms // 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


@dataclass(slots=True)
class TranscriptBundle:
    language_code: str | None
    is_auto_generated: bool
    segments: list[TranscriptSegment]
    estimated_duration_ms: int = 0

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)


@dataclass(slots=True)
class TranscriptRouteAssessment:
    should_use_fast_path: bool
    route_reason: str
    route_source: str


@dataclass(slots=True)
class YouTubeRoutingTrace:
    dynamic_routing_enabled: bool = False
    selected_path: str | None = None
    route_reason: str | None = None
    route_source: str | None = None
    transcript_status: str | None = None
    transcript_language_code: str | None = None
    transcript_fetch_error: str | None = None
    transcript_classifier_error: str | None = None
    transcript_summary_error: str | None = None

    def record_transcript(self, transcript: TranscriptBundle | None) -> None:
        if transcript is None:
            return
        self.transcript_language_code = transcript.language_code

    def to_log_extra(self, *, url: str, error: str | None = None) -> dict[str, object]:
        extra: dict[str, object | None] = {
            "url": url,
            "dynamic_routing_enabled": self.dynamic_routing_enabled,
            "selected_path": self.selected_path,
            "route_reason": self.route_reason,
            "route_source": self.route_source,
            "transcript_status": self.transcript_status,
            "transcript_language_code": self.transcript_language_code,
            "transcript_fetch_error": self.transcript_fetch_error,
            "transcript_classifier_error": self.transcript_classifier_error,
            "transcript_summary_error": self.transcript_summary_error,
            "error": error,
        }
        return {key: value for key, value in extra.items() if value is not None}

    def to_admin_context(self) -> str:
        details = [
            f"dynamic={'on' if self.dynamic_routing_enabled else 'off'}",
            f"path={self.selected_path or 'unknown'}",
            f"reason={self.route_reason or 'unknown'}",
        ]
        if self.route_source:
            details.append(f"source={self.route_source}")
        if self.transcript_status:
            details.append(f"transcript_status={self.transcript_status}")
        if self.transcript_language_code:
            details.append(f"lang={self.transcript_language_code}")
        if self.transcript_fetch_error:
            details.append(f"transcript_fetch_error={self.transcript_fetch_error}")
        if self.transcript_classifier_error:
            details.append(f"classifier_error={self.transcript_classifier_error}")
        if self.transcript_summary_error:
            details.append(f"transcript_error={self.transcript_summary_error}")
        return "routing: " + ", ".join(details)


class YouTubeService:
    _WATCH_VIDEO_ID_PATTERN = re.compile(r"(?:v=|/embed/|/shorts/|/live/|youtu\.be/)([\w\-]{6,})")
    _WHITESPACE_PATTERN = re.compile(r"\s+")
    _NOISE_PATTERN = re.compile(r"^\[[^\]]+\]$", re.IGNORECASE)
    _WORD_PATTERN = re.compile(r"[a-zA-Z]+|[가-힣]+")
    _INFORMATIVE_MARKER_PATTERN = re.compile(
        r"(?:설명(?:하겠|드리겠)|정리(?:하겠|해보겠)|소개(?:하겠|해보겠)|분석|비교|원인|결과|방법|핵심|요약|"
        r"먼저|다음|예를 들어|정리하면|즉|왜냐하면|그래서|오늘은|이번에는|현재|지금부터|"
        r"first|next|because|for example|in summary|today|let's|tutorial|review|explained?)",
        re.IGNORECASE,
    )
    _LYRIC_MARKER_PATTERN = re.compile(
        r"(?:\u266a|\u266c|chorus|verse|hook|bridge|refrain|lyrics?|가사|후렴|벌스|브리지|브릿지|"
        r"라라라|na na|la la|yeah yeah|oh oh|woo+)",
        re.IGNORECASE,
    )
    _GARBLED_FRAGMENT_PATTERN = re.compile(r"^[ㄱ-ㅎㅏ-ㅣa-z]{1,3}(?:\s+[ㄱ-ㅎㅏ-ㅣa-z]{1,3}){2,}$", re.IGNORECASE)
    _TRANSCRIPT_SOUND_EFFECT_TOKENS = frozenset(
        {
            "music",
            "bgm",
            "instrumental",
            "intro",
            "outro",
            "applause",
            "clapping",
            "cheering",
            "crowd",
            "laughter",
            "laughing",
            "laughs",
            "sigh",
            "gasp",
            "gasps",
            "breathing",
            "silence",
            "ambient",
            "noise",
            "sound",
            "effect",
            "effects",
            "beep",
            "ringtone",
            "alarm",
            "wind",
            "rain",
            "thunder",
            "engine",
            "horn",
            "typing",
            "footsteps",
            "음악",
            "배경음악",
            "효과음",
            "박수",
            "웃음",
            "환호",
            "탄성",
            "침묵",
            "정적",
            "빗소리",
            "천둥",
            "바람",
            "벨소리",
            "엔진",
            "타자",
            "발걸음",
        }
    )
    _TRANSCRIPT_ROUTE_LABELS = frozenset({"informative", "lyrics", "sound_effect_only", "garbled_or_unclear"})
    _GENERIC_SUMMARY_PATTERNS = (
        re.compile(r"정보가 부족[^\n]{0,80}(영상|자막).{0,40}(요약|분석)할 수 없"),
        re.compile(r"(영상|자막).{0,20}분석할 수 있는 구체적인 내용"),
        re.compile(r"(대본|주요 내용 설명|추가 정보).{0,30}제공해?주시면"),
    )
    _TRANSCRIPT_CLASSIFIER_SAMPLE_SIZE = 12
    _TRANSCRIPT_CLASSIFIER_TIMEOUT_SECONDS = 15
    _LONG_TRANSCRIPT_FAST_PATH_THRESHOLD_MS = 5 * 60 * 1000
    _LONG_TRANSCRIPT_FAST_PATH_EXCLUDED_REASONS = frozenset(
        {
            "transcript_lyrics",
            "transcript_sound_effect_only",
            "transcript_garbled_or_unclear",
        }
    )
    _RETRY_DELAY_SECONDS = 2.0
    _TRANSCRIPT_ROUTE_CLASSIFIER_PROMPT = (
        "Classify whether this YouTube transcript sample is reliable enough to summarize without seeing the video.\n"
        "Return exactly one label and nothing else:\n"
        "- informative\n"
        "- lyrics\n"
        "- sound_effect_only\n"
        "- garbled_or_unclear\n"
        "Use informative only when the transcript contains real spoken explanation, dialogue, or narration that can be summarized on its own.\n"
        "Use lyrics for song lyrics or chant-like lines.\n"
        "Use sound_effect_only for non-speech captions such as music, applause, laughter, or other effects.\n"
        "Use garbled_or_unclear for broken ASR fragments or transcript text that is too unclear to summarize reliably.\n\n"
        "Transcript sample:\n{sample}"
    )
    _RETRYABLE_STATUS_CODES = frozenset({503})
    _MAX_SUMMARY_RETRIES = 1

    def __init__(self, *, settings: Settings, prompts: PromptLibrary, delivery_service: object, admin_notifier: object) -> None:
        self._settings = settings
        self._prompts = prompts
        self._delivery_service = delivery_service
        self._admin_notifier = admin_notifier
        self._pattern = re.compile(constants.YOUTUBE_URL_PATTERN, re.IGNORECASE)

    def extract_youtube_urls(self, text: str) -> list[str]:
        return [match.group(1) for match in self._pattern.finditer(text)]

    async def handle_url(self, room: EffectiveRoomConfig, event: NormalizedInboundEvent, url: str) -> None:
        routing_trace = YouTubeRoutingTrace(dynamic_routing_enabled=self._settings.youtube_dynamic_routing_enabled)
        try:
            summary = await self.summarize_url(url, trace=routing_trace)
            if self._is_generic_summary_response(summary):
                logger.info(
                    "youtube_summary_generic_response_suppressed",
                    extra=routing_trace.to_log_extra(url=url, error="generic_response_suppressed"),
                )
                await self._admin_notifier.notify_feature_error(
                    room_name=room.name,
                    feature_name="youtube_summary",
                    error_message=self._build_generic_response_admin_message(
                        url=url,
                        response=summary,
                        routing_trace=routing_trace,
                    ),
                )
                return
            await self._delivery_service.send_text(
                room.name,
                summary,
                package_name=room.package_name,
                correlation_key=event.log_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("youtube_summary_failed", extra=routing_trace.to_log_extra(url=url, error=str(exc)))
            await self._admin_notifier.notify_feature_error(
                room_name=room.name,
                feature_name="youtube_summary",
                error_message=f"{url} | {exc} | {routing_trace.to_admin_context()}",
            )

    async def summarize_url(self, url: str, *, trace: YouTubeRoutingTrace | None = None) -> str:
        routing_trace = trace or YouTubeRoutingTrace()
        routing_trace.dynamic_routing_enabled = self._settings.youtube_dynamic_routing_enabled
        if self._settings.youtube_dynamic_routing_enabled:
            transcript = await self._try_fetch_transcript(url, trace=routing_trace)
            assessment = await self._assess_transcript_route(url, transcript, trace=routing_trace)
            routing_trace.record_transcript(transcript)
            force_transcript_fast_path = self._should_force_transcript_fast_path(transcript, assessment)
            if assessment.should_use_fast_path or force_transcript_fast_path:
                routing_trace.selected_path = "transcript_fast_path"
                routing_trace.route_reason = (
                    "transcript_long_form_override" if force_transcript_fast_path else assessment.route_reason
                )
                routing_trace.route_source = "duration_rule" if force_transcript_fast_path else assessment.route_source
                try:
                    logger.info("youtube_summary_fast_path", extra=routing_trace.to_log_extra(url=url))
                    return await self._summarize_transcript(url, transcript)
                except Exception as exc:  # noqa: BLE001
                    routing_trace.selected_path = "video_understanding"
                    routing_trace.route_reason = "transcript_summary_failed"
                    routing_trace.transcript_summary_error = str(exc)
                    logger.warning("youtube_summary_fast_path_failed", extra=routing_trace.to_log_extra(url=url, error=str(exc)))
            else:
                routing_trace.selected_path = "video_understanding"
                routing_trace.route_reason = self._resolve_video_fallback_reason(assessment.route_reason, routing_trace)
                routing_trace.route_source = assessment.route_source
                logger.info("youtube_summary_video_fallback", extra=routing_trace.to_log_extra(url=url))
        else:
            routing_trace.selected_path = "video_understanding"
            routing_trace.route_reason = "dynamic_routing_disabled"
        return await self._summarize_video_url(url)

    async def _summarize_video_url(self, url: str) -> str:
        if not self._settings.gemini_api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        template = self._prompts.youtube_summary["template"].replace("__SOURCE_LABEL__", "공개 YouTube 영상")
        endpoint = self._build_gemini_generate_content_endpoint(self._settings.gemini_youtube_model)
        payload = self._build_generate_content_payload(url=self._normalize_public_video_url(url), prompt=template)
        return await self._request_summary(endpoint=endpoint, payload=payload, error_label="Gemini YouTube video understanding")

    async def _summarize_transcript(self, url: str, transcript: TranscriptBundle) -> str:
        if not self._settings.gemini_api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        primary_model = self._settings.gemini_youtube_transcript_model
        template = self._resolve_transcript_summary_template(primary_model).replace("__SOURCE_LABEL__", "공개 YouTube 영상 자막")
        transcript_text = self._format_transcript_for_prompt(transcript)
        prompt = (
            f"{template}\n\n"
            "# Transcript Instructions\n"
            "- 아래 자막과 타임스탬프에 근거한 내용만 요약할 것.\n"
            "- 화면 정보가 없어서 확인할 수 없는 내용은 추측하지 말 것.\n"
            "- 타임스탬프는 자막에 포함된 값만 사용할 것.\n\n"
            "# Transcript\n"
            f"{transcript_text}"
        )
        endpoint = self._build_gemini_generate_content_endpoint(primary_model)
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]
        }
        try:
            return await self._request_summary(
                endpoint=endpoint,
                payload=payload,
                error_label="Gemini transcript summarization",
            )
        except ExternalAPIError as exc:
            fallback_model = self._resolve_transcript_fallback_model(primary_model)
            if exc.status_code not in {429, 503} or not fallback_model:
                raise
            logger.warning(
                "youtube_transcript_summary_model_fallback",
                extra={
                    "url": url,
                    "primary_model": primary_model,
                    "fallback_model": fallback_model,
                    "status_code": exc.status_code,
                    "fallback_delay_seconds": self._settings.gemini_youtube_transcript_fallback_delay_seconds,
                },
            )
            await asyncio.sleep(self._settings.gemini_youtube_transcript_fallback_delay_seconds)
            fallback_endpoint = self._build_gemini_generate_content_endpoint(fallback_model)
            return await self._request_summary(
                endpoint=fallback_endpoint,
                payload=payload,
                error_label=f"Gemini transcript summarization fallback ({fallback_model})",
            )

    def _resolve_transcript_summary_template(self, primary_model: str) -> str:
        use_lite_prompt = "lite" in primary_model.lower() and self._prompts.youtube_summary_lite is not None
        prompt_block = self._prompts.youtube_summary_lite if use_lite_prompt else self._prompts.youtube_summary
        return str(prompt_block["template"])

    @classmethod
    def _is_generic_summary_response(cls, text: str) -> bool:
        normalized = cls._WHITESPACE_PATTERN.sub(" ", text).strip()
        if not normalized:
            return False
        return any(pattern.search(normalized) for pattern in cls._GENERIC_SUMMARY_PATTERNS)

    @staticmethod
    def _build_generic_response_admin_message(
        *,
        url: str,
        response: str,
        routing_trace: YouTubeRoutingTrace,
    ) -> str:
        return (
            "일반방 전송 차단: 유튜브 요약 generic 응답\n"
            f"url: {url}\n"
            f"응답: {response}\n"
            f"{routing_trace.to_admin_context()}"
        )

    def _resolve_transcript_fallback_model(self, primary_model: str) -> str | None:
        candidates = (
            self._normalize_optional_model_name(self._settings.gemini_youtube_transcript_fallback_model),
            self._normalize_optional_model_name(self._settings.gemini_youtube_model),
        )
        for candidate in candidates:
            if candidate and candidate != primary_model:
                return candidate
        return None

    async def _request_summary(
        self,
        *,
        endpoint: str,
        payload: dict[str, object],
        error_label: str,
        empty_response_message: str = "Gemini returned an empty YouTube summary",
        timeout_seconds: int | None = None,
    ) -> str:
        response: httpx.Response | None = None
        timeout = timeout_seconds or self._settings.gemini_timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(self._MAX_SUMMARY_RETRIES + 1):
                try:
                    response = await client.post(
                        endpoint,
                        headers={"x-goog-api-key": self._settings.gemini_api_key},
                        json=payload,
                    )
                    response.raise_for_status()
                    break
                except httpx.TimeoutException as exc:
                    raise ExternalAPIError(f"{error_label} timed out") from exc
                except httpx.HTTPStatusError as exc:
                    if self._should_retry_status(exc.response.status_code, attempt):
                        await asyncio.sleep(self._RETRY_DELAY_SECONDS)
                        logger.warning(
                            "youtube_summary_retrying_after_503",
                            extra={
                                "endpoint": endpoint,
                                "error_label": error_label,
                                "status_code": exc.response.status_code,
                                "attempt": attempt + 1,
                            },
                        )
                        continue
                    raise self._map_http_error(exc, error_label) from exc
                except httpx.HTTPError as exc:
                    raise ExternalAPIError(f"{error_label} request failed: {exc}") from exc
        if response is None:
            raise ExternalAPIError(f"{error_label} failed before receiving a response")
        data = response.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "\n".join(part.get("text", "") for part in parts if part.get("text")).strip()
        if not text:
            raise ExternalAPIError(empty_response_message)
        return text

    async def _try_fetch_transcript(self, url: str, *, trace: YouTubeRoutingTrace | None = None) -> TranscriptBundle | None:
        video_id = self._extract_video_id(url)
        if not video_id:
            if trace is not None:
                trace.transcript_status = "video_id_missing"
            return None

        try:
            transcript = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_transcript_bundle_sync, video_id),
                timeout=self._settings.youtube_transcript_timeout_seconds,
            )
        except asyncio.TimeoutError:
            if trace is not None:
                trace.transcript_status = "transcript_timeout"
                trace.transcript_fetch_error = f"timeout>{self._settings.youtube_transcript_timeout_seconds}s"
            logger.info(
                "youtube_transcript_fetch_timed_out",
                extra={"url": url, "transcript_status": "transcript_timeout"},
            )
            return None
        except Exception as exc:  # noqa: BLE001
            transcript_status, log_level = self._classify_transcript_exception(exc)
            if trace is not None:
                trace.transcript_status = transcript_status
                trace.transcript_fetch_error = f"{type(exc).__name__}: {exc}"
            getattr(logger, log_level)(
                "youtube_transcript_fetch_failed",
                extra={"url": url, "transcript_status": transcript_status, "error": str(exc)},
            )
            return None

        if trace is not None:
            trace.transcript_status = "transcript_ready"
        return transcript

    def _fetch_transcript_bundle_sync(self, video_id: str) -> TranscriptBundle | None:
        if YouTubeTranscriptApi is None:
            raise ConfigurationError("youtube-transcript-api is not installed")

        transcript = YouTubeTranscriptApi().fetch(video_id, languages=["ko", "en"])
        return self._build_transcript_bundle(transcript)

    @classmethod
    def _build_transcript_bundle(cls, transcript: object) -> TranscriptBundle | None:
        segments: list[TranscriptSegment] = []
        estimated_duration_ms = 0
        for snippet in transcript:
            text = cls._clean_caption_text(getattr(snippet, "text", ""))
            if not text:
                continue
            start_value = getattr(snippet, "start", None)
            if start_value is None:
                start_value = getattr(snippet, "offset", None)
            try:
                start_ms = max(0, int(float(start_value) * 1000))
            except (TypeError, ValueError):
                continue
            duration_value = getattr(snippet, "duration", 0.0)
            try:
                duration_ms = max(0, int(float(duration_value) * 1000))
            except (TypeError, ValueError):
                duration_ms = 0
            segments.append(TranscriptSegment(start_ms=start_ms, text=text))
            estimated_duration_ms = max(estimated_duration_ms, start_ms + duration_ms)

        if not segments:
            return None

        return TranscriptBundle(
            language_code=cls._coerce_optional_str(getattr(transcript, "language_code", None)),
            is_auto_generated=bool(
                getattr(transcript, "is_generated", getattr(transcript, "is_auto_generated", False))
            ),
            segments=segments,
            estimated_duration_ms=estimated_duration_ms or max(segment.start_ms for segment in segments),
        )

    @staticmethod
    def _classify_transcript_exception(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, ConfigurationError):
            return "transcript_client_unavailable", "warning"
        if isinstance(exc, _TRANSCRIPT_UNAVAILABLE_EXCEPTIONS):
            return "transcript_unavailable", "info"
        if isinstance(exc, _TRANSCRIPT_API_EXCEPTIONS):
            return "transcript_api_error", "warning"
        return "transcript_unexpected_error", "warning"

    async def _assess_transcript_route(
        self,
        url: str,
        transcript: TranscriptBundle | None,
        *,
        trace: YouTubeRoutingTrace | None = None,
    ) -> TranscriptRouteAssessment:
        if transcript is None:
            return TranscriptRouteAssessment(False, "transcript_unavailable", "local")

        local_label = self._classify_transcript_locally(transcript)
        if local_label != "ambiguous":
            return TranscriptRouteAssessment(local_label == "informative", f"transcript_{local_label}", "local")

        try:
            classifier_label = await self._classify_transcript_with_model(transcript)
        except Exception as exc:  # noqa: BLE001
            if trace is not None:
                trace.transcript_classifier_error = str(exc)
            logger.warning(
                "youtube_transcript_route_classifier_failed",
                extra=(trace or YouTubeRoutingTrace()).to_log_extra(url=url, error=str(exc)),
            )
            return TranscriptRouteAssessment(False, "transcript_classifier_failed", "classifier")
        return TranscriptRouteAssessment(
            classifier_label == "informative",
            f"transcript_{classifier_label}",
            "classifier",
        )

    @staticmethod
    def _resolve_video_fallback_reason(route_reason: str, trace: YouTubeRoutingTrace) -> str:
        if route_reason != "transcript_unavailable":
            return route_reason
        if trace.transcript_status and trace.transcript_status != "transcript_ready":
            return trace.transcript_status
        return route_reason

    @classmethod
    def _should_force_transcript_fast_path(
        cls,
        transcript: TranscriptBundle | None,
        assessment: TranscriptRouteAssessment,
    ) -> bool:
        if transcript is None or assessment.should_use_fast_path:
            return False
        if transcript.estimated_duration_ms < cls._LONG_TRANSCRIPT_FAST_PATH_THRESHOLD_MS:
            return False
        return assessment.route_reason not in cls._LONG_TRANSCRIPT_FAST_PATH_EXCLUDED_REASONS

    def _format_transcript_for_prompt(self, transcript: TranscriptBundle) -> str:
        lines: list[str] = []
        total_chars = 0
        limit = self._settings.youtube_transcript_max_chars
        for segment in transcript.segments:
            line = f"- {segment.timestamp_label} {segment.text}"
            next_total = total_chars + len(line) + 1
            if lines and next_total > limit:
                break
            lines.append(line)
            total_chars = next_total
        return "\n".join(lines)

    @classmethod
    def _extract_video_id(cls, url: str) -> str | None:
        match = cls._WATCH_VIDEO_ID_PATTERN.search(url)
        return match.group(1) if match else None

    @classmethod
    def _normalize_public_video_url(cls, url: str) -> str:
        video_id = cls._extract_video_id(url)
        if not video_id:
            return url
        return f"https://www.youtube.com/watch?v={video_id}"

    @classmethod
    def _clean_caption_text(cls, text: str) -> str:
        normalized = cls._WHITESPACE_PATTERN.sub(" ", str(text).replace("\n", " ")).strip()
        if not normalized or cls._NOISE_PATTERN.match(normalized):
            return ""
        return normalized

    @classmethod
    def _normalize_segment_text(cls, text: str) -> str:
        return cls._WHITESPACE_PATTERN.sub(" ", text).strip().lower()

    def _classify_transcript_locally(self, transcript: TranscriptBundle) -> str:
        normalized_lines = [self._normalize_segment_text(segment.text) for segment in transcript.segments]
        normalized_lines = [line for line in normalized_lines if line]
        if not normalized_lines:
            return "ambiguous"
        if all(self._looks_like_sound_effect_line(line) for line in normalized_lines):
            return "sound_effect_only"
        if any(self._looks_like_informative_line(line) for line in normalized_lines):
            return "informative"
        if any(self._looks_like_lyric_line(line) for line in normalized_lines):
            return "lyrics"
        if self._looks_like_fragmentary_transcript(normalized_lines):
            return "garbled_or_unclear"
        if all(self._looks_like_garbled_line(line) for line in normalized_lines):
            return "garbled_or_unclear"
        return "ambiguous"

    async def _classify_transcript_with_model(self, transcript: TranscriptBundle) -> str:
        if not self._settings.gemini_api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        prompt = self._TRANSCRIPT_ROUTE_CLASSIFIER_PROMPT.format(sample=self._build_transcript_classifier_sample(transcript))
        endpoint = self._build_gemini_generate_content_endpoint(self._settings.gemini_chat_model)
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]
        }
        raw_label = await self._request_summary(
            endpoint=endpoint,
            payload=payload,
            error_label="Gemini transcript routing classification",
            empty_response_message="Gemini transcript routing classification returned an empty response",
            timeout_seconds=min(self._settings.gemini_timeout_seconds, self._TRANSCRIPT_CLASSIFIER_TIMEOUT_SECONDS),
        )
        label = self._parse_transcript_classifier_label(raw_label)
        if label is None:
            raise ExternalAPIError(f"Gemini transcript routing classification returned an unexpected label: {raw_label}")
        return label

    def _build_transcript_classifier_sample(self, transcript: TranscriptBundle) -> str:
        segments = self._select_transcript_segments_for_classification(transcript.segments)
        return "\n".join(f"- {segment.timestamp_label} {segment.text}" for segment in segments)

    def _select_transcript_segments_for_classification(self, segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
        if len(segments) <= self._TRANSCRIPT_CLASSIFIER_SAMPLE_SIZE:
            return segments
        window = self._TRANSCRIPT_CLASSIFIER_SAMPLE_SIZE // 3
        mid = len(segments) // 2
        candidates = segments[:window] + segments[max(0, mid - (window // 2)) : mid + (window // 2)] + segments[-window:]
        selected: list[TranscriptSegment] = []
        seen: set[str] = set()
        for segment in candidates:
            normalized = self._normalize_segment_text(segment.text)
            if not normalized or normalized in seen:
                continue
            selected.append(segment)
            seen.add(normalized)
        return selected or segments[: self._TRANSCRIPT_CLASSIFIER_SAMPLE_SIZE]

    @classmethod
    def _parse_transcript_classifier_label(cls, raw_label: str) -> str | None:
        normalized = raw_label.strip().lower()
        for label in cls._TRANSCRIPT_ROUTE_LABELS:
            pattern = label.replace("_", r"[_\s-]*")
            if re.search(rf"\b{pattern}\b", normalized):
                return label
        return None

    @classmethod
    def _looks_like_sound_effect_line(cls, text: str) -> bool:
        tokens = cls._WORD_PATTERN.findall(text)
        if not tokens:
            return any(marker in text for marker in ("\u266a", "\u266c"))
        return all(token in cls._TRANSCRIPT_SOUND_EFFECT_TOKENS for token in tokens)

    @classmethod
    def _looks_like_informative_line(cls, text: str) -> bool:
        return bool(cls._INFORMATIVE_MARKER_PATTERN.search(text))

    @classmethod
    def _looks_like_lyric_line(cls, text: str) -> bool:
        return bool(cls._LYRIC_MARKER_PATTERN.search(text))

    @classmethod
    def _looks_like_garbled_line(cls, text: str) -> bool:
        if "\ufffd" in text:
            return True
        if cls._GARBLED_FRAGMENT_PATTERN.fullmatch(text):
            return True
        tokens = cls._WORD_PATTERN.findall(text)
        return len(tokens) >= 3 and all(len(token) == 1 for token in tokens)

    @classmethod
    def _looks_like_fragmentary_transcript(cls, lines: list[str]) -> bool:
        tokenized_lines = [cls._WORD_PATTERN.findall(line) for line in lines]
        tokenized_lines = [tokens for tokens in tokenized_lines if tokens]
        if not tokenized_lines:
            return False
        flattened_tokens = [token for tokens in tokenized_lines for token in tokens]
        unique_tokens = {token.lower() for token in flattened_tokens}
        return (
            all(len(tokens) <= 2 for tokens in tokenized_lines)
            and len(flattened_tokens) <= 4
            and len(unique_tokens) <= 3
            and all(len(token) <= 5 for token in unique_tokens)
        )

    @staticmethod
    def _coerce_optional_str(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

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
    def _map_http_error(exc: httpx.HTTPStatusError, error_label: str) -> ExternalAPIError:
        status_code = exc.response.status_code
        response_text = exc.response.text.lower()
        guarded_keywords = ("youtube", "video", "private", "blocked", "unavailable", "permission", "public")
        if status_code in {400, 403, 404} and any(keyword in response_text for keyword in guarded_keywords):
            return ExternalAPIError(
                "YouTube video is private, blocked, or inaccessible for Gemini video understanding",
                status_code=status_code,
            )
        if status_code in {408, 504}:
            return ExternalAPIError(f"{error_label} timed out", status_code=status_code)
        return ExternalAPIError(f"{error_label} failed with status {status_code}", status_code=status_code)

    @classmethod
    def _should_retry_status(cls, status_code: int, attempt: int) -> bool:
        return status_code in cls._RETRYABLE_STATUS_CODES and attempt < cls._MAX_SUMMARY_RETRIES

    @staticmethod
    def _build_gemini_generate_content_endpoint(model_name: str) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

    @staticmethod
    def _normalize_optional_model_name(model_name: str | None) -> str | None:
        if model_name is None:
            return None
        normalized = model_name.strip()
        return normalized or None
