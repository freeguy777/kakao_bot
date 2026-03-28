from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import requests
from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
    YouTubeTranscriptApiException,
)

from server.application.prompting import render_prompt_template
from server.infra.llm_clients import call_gemini_parts, call_gemini_text
from server.infra.sqlite_store import save_processed_video
from server.settings import get_settings
from server.utils import extract_video_id, extract_youtube_urls, normalize_youtube_public_url, normalize_youtube_url, safe_truncate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class YouTubeCollectionResult:
    messages: list[str]
    processed_video_ids: list[str]
    skipped_video_ids: list[str]
    failed_urls: list[str]
    failure_details: list[dict[str, Any]]


@dataclass(frozen=True)
class YouTubeSummaryResult:
    summary: str
    failure_reason: str | None = None
    attempts: int = 1


def split_long_message(text: str, limit: int = 3000) -> list[str]:
    normalized = str(text).strip()
    if not normalized:
        return []
    if len(normalized) <= limit:
        return [normalized]

    chunks: list[str] = []
    current = ""
    for line in normalized.splitlines() or [normalized]:
        line = line.strip()
        if not line:
            continue
        if not current:
            current = line
            continue
        candidate = f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def detect_message_features(message: str) -> dict[str, Any]:
    youtube_urls = extract_youtube_urls(message)
    return {
        "has_youtube_url": bool(youtube_urls),
        "youtube_urls": youtube_urls,
    }


def _format_multiline_summary(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    normalized = re.sub(r"(?ms)\n*video_id:\s*.*$", "", normalized)
    normalized = re.sub(r"(?ms)\n*transcript:\s*.*$", "", normalized)
    normalized = normalized.replace("**", "").replace("__", "").replace("```", "").replace("`", "")
    normalized = normalized.replace("$", "").replace("> ", "")
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\(([^()\n]+)\s*\n\s*([^)]+)\)", r"(\1 \2)", normalized)
    normalized = re.sub(r"([<>=+\-])\s*\n\s*([A-Za-z0-9가-힣])", r"\1 \2", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    normalized = re.sub(r"(?<!\n)(##\s)", r"\n\1", normalized)
    normalized = re.sub(r"(?<!\n)(###\s)", r"\n\1", normalized)
    normalized = re.sub(r"(?<!\n)(-\s)", r"\n\1", normalized)
    normalized = re.sub(r"(?<!\n)(\*\s)", r"\n\1", normalized)
    normalized = re.sub(r"(?<!\n)(---)", r"\n\1", normalized)
    normalized = re.sub(r"(?m)^핵심 요약:", "◆ 핵심 요약:", normalized)
    normalized = re.sub(r"(?m)^한 줄 요약$", "• 한 줄 요약", normalized)
    normalized = re.sub(r"(?m)^주요 포인트$", "• 주요 포인트", normalized)
    normalized = re.sub(r"(?m)^인사이트$", "• 인사이트", normalized)
    return normalized.strip()


def _truncate_multiline_text(text: str, limit: int) -> str:
    formatted = _format_multiline_summary(text)
    if len(formatted) <= limit:
        return formatted

    search_window = formatted[:limit]
    min_index = max(0, int(limit * 0.6))
    for marker in ["\n\n", "\n", ". ", "! ", "? ", "다. ", "요. "]:
        cut = search_window.rfind(marker)
        if cut >= min_index:
            end = cut if marker.startswith("\n") else cut + len(marker.strip())
            return f"{search_window[:end].rstrip()}\n..."
    return f"{search_window[: max(0, limit - 4)].rstrip()}\n..."


def _fetch_youtube_transcript_text(video_id: str) -> str:
    api = YouTubeTranscriptApi()
    transcript = api.fetch(video_id, languages=["ko", "en"])
    text = " ".join(snippet.text.strip() for snippet in transcript if snippet.text.strip())
    return " ".join(text.split())


def _is_low_signal_transcript(transcript_text: str) -> bool:
    normalized = " ".join((transcript_text or "").split()).strip()
    if not normalized:
        return True

    stripped = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", normalized)
    stripped = re.sub(r"[^0-9A-Za-z가-힣 ]+", " ", stripped)
    tokens = [token for token in stripped.split() if token]
    signal_chars = len("".join(tokens))
    if signal_chars < 30:
        return True
    if len(tokens) < 6:
        return True
    return False


def _call_gemini_short_summary(video_id: str, transcript_text: str) -> str:
    settings = get_settings()
    transcript_excerpt = safe_truncate(transcript_text, settings.llm.youtube_transcript_char_limit)
    prompt = render_prompt_template(
        "youtube_summary_transcript_prompt",
        {
            "__VIDEO_ID__": video_id,
            "__TRANSCRIPT__": transcript_excerpt,
        },
    )
    text = call_gemini_text(
        feature_key="youtube_summary",
        prompt_text=prompt,
        max_output_tokens=settings.llm.youtube_summary_max_output_tokens,
        preserve_newlines=True,
    )
    return _truncate_multiline_text(text, settings.llm.youtube_summary_char_limit)


def _call_gemini_video_summary(video_url: str) -> str:
    settings = get_settings()
    prompt = render_prompt_template(
        "youtube_summary_video_prompt",
        {"__SOURCE_LABEL__": "공개 YouTube 영상 직접 분석"},
    )
    text = call_gemini_parts(
        feature_key="youtube_summary",
        parts=[
            {"file_data": {"file_uri": video_url, "mime_type": "video/*"}},
            {"text": prompt},
        ],
        max_output_tokens=settings.llm.youtube_summary_max_output_tokens,
        preserve_newlines=True,
        timeout=settings.llm.gemini_timeout_seconds,
    )
    return _truncate_multiline_text(text, settings.llm.youtube_summary_char_limit)


def _format_failure_reason(prefix: str, exc: Exception | None = None) -> str:
    if exc is None:
        return prefix
    return safe_truncate(f"{prefix}: {type(exc).__name__}: {exc}", 240)


def _summarize_youtube_url_once(room_key: str, url: str) -> YouTubeSummaryResult:
    source_url = normalize_youtube_public_url(url)
    normalized_url = normalize_youtube_url(url)
    video_id = extract_video_id(normalized_url)
    if not video_id:
        logger.info("youtube video_id missing room_key=%s url=%s", room_key, url)
        return YouTubeSummaryResult(summary="", failure_reason="video_id missing")

    settings = get_settings()
    mode = settings.llm.youtube_summary_mode.strip().lower() or "hybrid"
    started_at = perf_counter()

    if mode == "gemini_video":
        try:
            summary = _call_gemini_video_summary(source_url)
            if summary:
                save_processed_video(room_key, video_id, normalized_url)
                logger.info(
                    "youtube summary completed room_key=%s video_id=%s mode=gemini_video elapsed_ms=%.1f",
                    room_key,
                    video_id,
                    (perf_counter() - started_at) * 1000,
                )
                return YouTubeSummaryResult(summary=summary)
        except requests.HTTPError as exc:
            logger.warning("youtube gemini video http error video_id=%s error=%s", video_id, exc)
            return YouTubeSummaryResult(summary="", failure_reason=_format_failure_reason("gemini video http error", exc))
        except requests.RequestException as exc:
            logger.warning("youtube gemini video request failed video_id=%s error=%s", video_id, exc)
            return YouTubeSummaryResult(summary="", failure_reason=_format_failure_reason("gemini video request failed", exc))
        except Exception as exc:
            logger.warning("youtube gemini video failed video_id=%s error=%s", video_id, exc)
            return YouTubeSummaryResult(summary="", failure_reason=_format_failure_reason("gemini video failed", exc))

    transcript_failure_reason = ""
    try:
        transcript_started_at = perf_counter()
        transcript_text = _fetch_youtube_transcript_text(video_id)
        transcript_fetch_elapsed_ms = (perf_counter() - transcript_started_at) * 1000
        if not transcript_text:
            logger.info("youtube transcript empty room_key=%s video_id=%s", room_key, video_id)
            transcript_failure_reason = "transcript empty"
        elif _is_low_signal_transcript(transcript_text):
            logger.info("youtube transcript low signal room_key=%s video_id=%s", room_key, video_id)
            transcript_failure_reason = "transcript low signal"
        else:
            summary_started_at = perf_counter()
            summary = _call_gemini_short_summary(video_id, transcript_text)
            if summary:
                save_processed_video(room_key, video_id, normalized_url)
                logger.info(
                    "youtube summary completed room_key=%s video_id=%s mode=transcript transcript_fetch_ms=%.1f llm_ms=%.1f total_ms=%.1f",
                    room_key,
                    video_id,
                    transcript_fetch_elapsed_ms,
                    (perf_counter() - summary_started_at) * 1000,
                    (perf_counter() - started_at) * 1000,
                )
                return YouTubeSummaryResult(summary=summary)
            transcript_failure_reason = "transcript summary empty response"
    except (NoTranscriptFound, TranscriptsDisabled, CouldNotRetrieveTranscript):
        logger.info("youtube transcript unavailable room_key=%s video_id=%s", room_key, video_id)
        transcript_failure_reason = "transcript unavailable"
    except YouTubeTranscriptApiException as exc:
        logger.warning("youtube transcript error video_id=%s error=%s", video_id, exc)
        transcript_failure_reason = _format_failure_reason("transcript api error", exc)
    except requests.HTTPError as exc:
        logger.warning("youtube summary http error video_id=%s error=%s", video_id, exc)
        transcript_failure_reason = _format_failure_reason("transcript http error", exc)
    except requests.RequestException as exc:
        logger.warning("youtube summary request failed video_id=%s error=%s", video_id, exc)
        transcript_failure_reason = _format_failure_reason("transcript request failed", exc)
    except Exception as exc:
        logger.exception("youtube summary failed video_id=%s", video_id, exc_info=exc)
        transcript_failure_reason = _format_failure_reason("transcript unexpected error", exc)

    if mode != "hybrid":
        return YouTubeSummaryResult(summary="", failure_reason=transcript_failure_reason or "summary unavailable")

    fallback_failure_reason = ""
    try:
        summary = _call_gemini_video_summary(source_url)
        if summary:
            save_processed_video(room_key, video_id, normalized_url)
            logger.info(
                "youtube summary completed room_key=%s video_id=%s mode=hybrid_gemini_video total_ms=%.1f",
                room_key,
                video_id,
                (perf_counter() - started_at) * 1000,
            )
            return YouTubeSummaryResult(summary=summary)
        fallback_failure_reason = "gemini video empty response"
    except requests.HTTPError as exc:
        logger.warning("youtube hybrid gemini video http error video_id=%s error=%s", video_id, exc)
        fallback_failure_reason = _format_failure_reason("gemini video http error", exc)
    except requests.RequestException as exc:
        logger.warning("youtube hybrid gemini video request failed video_id=%s error=%s", video_id, exc)
        fallback_failure_reason = _format_failure_reason("gemini video request failed", exc)
    except Exception as exc:
        logger.warning("youtube hybrid gemini video failed video_id=%s error=%s", video_id, exc)
        fallback_failure_reason = _format_failure_reason("gemini video failed", exc)

    combined_reason = transcript_failure_reason or fallback_failure_reason or "summary unavailable"
    if transcript_failure_reason and fallback_failure_reason:
        combined_reason = f"{transcript_failure_reason} | fallback={fallback_failure_reason}"
    return YouTubeSummaryResult(summary="", failure_reason=combined_reason)


def summarize_youtube_url_result(room_key: str, url: str, retry_count: int = 1) -> YouTubeSummaryResult:
    normalized_retry_count = max(0, int(retry_count))
    total_attempts = normalized_retry_count + 1
    source_url = normalize_youtube_public_url(url)
    video_id = extract_video_id(source_url) or "-"
    last_result = YouTubeSummaryResult(summary="", failure_reason="summary unavailable", attempts=1)

    for attempt in range(1, total_attempts + 1):
        last_result = _summarize_youtube_url_once(room_key, source_url)
        if last_result.summary:
            return YouTubeSummaryResult(summary=last_result.summary, failure_reason=None, attempts=attempt)
        if attempt < total_attempts:
            logger.info(
                "youtube summary retry scheduled room_key=%s video_id=%s next_attempt=%s/%s reason=%s",
                room_key,
                video_id,
                attempt + 1,
                total_attempts,
                last_result.failure_reason or "summary unavailable",
            )

    return YouTubeSummaryResult(
        summary="",
        failure_reason=last_result.failure_reason or "summary unavailable",
        attempts=total_attempts,
    )


def summarize_youtube_url(room_key: str, url: str) -> str:
    return summarize_youtube_url_result(room_key, url).summary


def collect_youtube_summary_messages(
    room_key: str,
    urls: list[str],
    *,
    message_length_limit: int = 3000,
) -> YouTubeCollectionResult:
    messages: list[str] = []
    processed_video_ids: list[str] = []
    skipped_video_ids: list[str] = []
    failed_urls: list[str] = []
    failure_details: list[dict[str, Any]] = []
    seen_video_ids: set[str] = set()

    for url in urls:
        source_url = normalize_youtube_public_url(url)
        video_id = extract_video_id(source_url)
        if not video_id:
            failed_urls.append(url)
            continue
        if video_id in seen_video_ids:
            skipped_video_ids.append(video_id)
            continue
        seen_video_ids.add(video_id)
        summary_result = summarize_youtube_url_result(room_key, source_url, retry_count=1)
        if not summary_result.summary:
            failed_urls.append(url)
            failure_details.append(
                {
                    "url": url,
                    "video_id": video_id,
                    "reason": summary_result.failure_reason or "summary unavailable",
                    "attempts": summary_result.attempts,
                }
            )
            continue
        processed_video_ids.append(video_id)
        messages.extend(split_long_message(summary_result.summary, limit=message_length_limit))

    return YouTubeCollectionResult(
        messages=messages,
        processed_video_ids=processed_video_ids,
        skipped_video_ids=skipped_video_ids,
        failed_urls=failed_urls,
        failure_details=failure_details,
    )
