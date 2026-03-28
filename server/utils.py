from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from server.core.trace import make_trace_id
from server.settings import get_timezone

YOUTUBE_URL_PATTERN = re.compile(
    r"((?:https?://)?(?:www\.|m\.)?(?:youtu\.be/[A-Za-z0-9_-]{6,}|youtube\.com/(?:watch\?[^ ]*v=[A-Za-z0-9_-]{6,}|shorts/[A-Za-z0-9_-]{6,}))[^\s]*)",
    re.IGNORECASE,
)


def extract_youtube_urls(text: str) -> list[str]:
    return [match.group(1) for match in YOUTUBE_URL_PATTERN.finditer(text or "")]


def _normalize_url_for_parsing(url: str) -> str:
    normalized = (url or "").strip()
    if not normalized:
        return ""
    if "://" not in normalized and (
        normalized.lower().startswith("youtube.com/")
        or normalized.lower().startswith("www.youtube.com/")
        or normalized.lower().startswith("m.youtube.com/")
        or normalized.lower().startswith("youtu.be/")
    ):
        return f"https://{normalized}"
    return normalized


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(_normalize_url_for_parsing(url))
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        return parsed.path.strip("/").split("/")[0] or None
    if "youtube.com" in host:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if parsed.path.startswith("/shorts/"):
            return parsed.path.split("/shorts/", 1)[1].split("/")[0]
    return None


def normalize_youtube_url(url: str) -> str:
    video_id = extract_video_id(url)
    if not video_id:
        return _normalize_url_for_parsing(url)
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize_youtube_public_url(url: str) -> str:
    normalized = _normalize_url_for_parsing(url)
    video_id = extract_video_id(normalized)
    if not video_id:
        return normalized

    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    if "youtube.com" in host and parsed.path.startswith("/shorts/"):
        return f"https://www.youtube.com/shorts/{video_id}"

    return f"https://www.youtube.com/watch?v={video_id}"


def now_kst() -> datetime:
    return datetime.now(ZoneInfo(get_timezone()))


def safe_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def smart_truncate(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split()).strip()
    if len(normalized) <= limit:
        return normalized

    min_index = max(0, int(limit * 0.6))
    search_window = normalized[:limit]
    for marker in [". ", "! ", "? ", "다. ", "요. ", ".\n", "!\n", "?\n"]:
        cut = search_window.rfind(marker)
        if cut >= min_index:
            end = cut + len(marker.strip())
            return search_window[:end].strip()

    cut = search_window.rfind(" ")
    if cut >= min_index:
        return f"{search_window[:cut].strip()}..."
    return f"{search_window[: max(0, limit - 3)].strip()}..."
