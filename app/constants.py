from __future__ import annotations

ACK_OK = "ok"
ACK_RETRYABLE_ERROR = "retryable_error"
ACK_FATAL_ERROR = "fatal_error"

FAILURE_RESEARCH = "research_failed"
FAILURE_DELIVERY = "delivery_failed"
FAILURE_SOCKET = "socket_failed"
FAILURE_API = "api_failed"

INBOUND_STATUS_PENDING = "pending"
INBOUND_STATUS_PROCESSING = "processing"
INBOUND_STATUS_PROCESSED = "processed"
INBOUND_STATUS_FAILED = "failed"

OUTBOUND_STATUS_PENDING = "pending"
OUTBOUND_STATUS_SENT = "sent"
OUTBOUND_STATUS_FAILED = "failed"

SCHEDULED_STATUS_SUCCESS = "success"
SCHEDULED_STATUS_FAILED = "failed"

DEFAULT_MESSAGE_CHUNK_LIMIT = 1600
DEFAULT_KMA_BASE_SLOTS = ("0200", "0500", "0800", "1100", "1400", "1700", "2000", "2300")
DEFAULT_KIMI_FORMULA_URIS = ("moonshot/date:latest", "moonshot/web-search:latest")

ADMIN_COMMANDS = {
    "@상태",
    "@방목록",
    "@전송큐",
    "@소켓상태",
    "@진단",
    "@재전송",
    "@기능조회",
    "@기능설정",
}

FEATURE_NAMES = {
    "youtube_summary",
    "llm_chat",
    "hanall_briefing",
    "weather",
    "child_age",
    "admin_commands",
}

YOUTUBE_URL_PATTERN = (
    r"(https?://(?:(?:www|m|music)\.)?(?:youtube\.com/"
    r"(?:watch\?v=[\w\-]{6,}|shorts/[\w\-]{6,}|live/[\w\-]{6,}|embed/[\w\-]{6,})"
    r"|youtu\.be/[\w\-]{6,})(?:[^\s]*)?)"
)
