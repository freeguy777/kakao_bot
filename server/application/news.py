from __future__ import annotations

import logging
import re

import requests

from server.application.delivery import deliver_room_messages
from server.application.errors import FeatureExecutionError
from server.application.hanall_research import build_hanall_known_events_context
from server.application.prompting import run_prompt_by_key_raw
from server.config import get_admin_room_key
from server.utils import make_trace_id, now_kst

logger = logging.getLogger(__name__)


def get_news_summary() -> str:
    return "오늘 뉴스 요약: 주요 시장 이벤트와 종목 이슈를 추후 API 연동으로 대체할 예정입니다."


def _normalize_hanall_news_text(raw_text: str) -> str:
    normalized = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("LLM 응답이 비어 있습니다.")
    normalized = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", normalized, count=1)
    normalized = re.sub(r"\s*```$", "", normalized, count=1)
    normalized = normalized.strip()
    normalized = normalized.replace("**", "").replace("__", "").replace("`", "")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    if not normalized:
        raise ValueError("정리 후 브리핑 텍스트가 비어 있습니다.")
    if not normalized.startswith("[한올/Immunovant 24시간 브리핑]"):
        normalized = f"[한올/Immunovant 24시간 브리핑]\n{normalized}"
    return normalized


def _send_hanall_news_raw_to_admin(*, room_key: str | None, raw_text: str) -> None:
    admin_room_key = get_admin_room_key(room_key)
    if not admin_room_key:
        return

    normalized = str(raw_text or "").strip() or "(empty)"
    message = (
        "[hanall_news raw]\n"
        f"source_room_key: {room_key or '-'}\n"
        f"captured_at: {now_kst().strftime('%Y-%m-%d %H:%M:%S KST')}\n"
        "payload_kind: text\n"
        f"raw_length: {len(str(raw_text or ''))}\n\n"
        f"{normalized}"
    )
    try:
        deliver_room_messages(
            room_key=admin_room_key,
            message=message,
            trace_id=make_trace_id(),
            source_type="admin:hanall_news_raw",
            meta={
                "source_room_key": room_key or "",
                "payload_kind": "text",
            },
            allow_fallback=True,
        )
    except Exception as exc:
        logger.warning(
            "failed to enqueue hanall raw payload to admin room source_room_key=%s admin_room_key=%s error=%s",
            room_key,
            admin_room_key,
            exc,
        )


def build_hanall_news_brief(
    room_key: str | None = None,
    *,
    raise_on_error: bool = False,
    send_raw_to_admin: bool = False,
) -> str:
    try:
        raw_response = run_prompt_by_key_raw("hanall_news_prompt")
        if send_raw_to_admin:
            _send_hanall_news_raw_to_admin(room_key=room_key, raw_text=raw_response)
        normalized = _normalize_hanall_news_text(raw_response)
        return normalized
    except TimeoutError as exc:
        logger.warning("hanall news brief timed out error=%s", exc)
        if raise_on_error:
            raise FeatureExecutionError(
                "hanall_news_brief",
                "한올 뉴스 브리핑 생성이 지연되어 방 전송을 생략했습니다.",
            ) from exc
        return (
            "한올 뉴스 브리핑 생성이 아직 끝나지 않았습니다.\n"
            "모델 응답이 지연되고 있어 잠시 후 다시 시도해 주세요."
        )
    except requests.RequestException as exc:
        logger.warning("hanall news brief request failed error=%s", exc)
        if raise_on_error:
            raise FeatureExecutionError(
                "hanall_news_brief",
                "한올 뉴스 브리핑 외부 API 호출이 실패해 방 전송을 생략했습니다.",
            ) from exc
        return (
            "한올 뉴스 브리핑을 지금 가져오지 못했습니다.\n"
            "외부 API 연결 상태를 확인한 뒤 다시 시도해 주세요."
        )
    except ValueError as exc:
        logger.warning("hanall news brief text normalization failed error=%s", exc)
        if raise_on_error:
            raise FeatureExecutionError(
                "hanall_news_brief",
                "한올 뉴스 브리핑 텍스트 정리에 실패해 방 전송을 생략했습니다.",
            ) from exc
        return (
            "한올 뉴스 브리핑 텍스트 정리에 실패했습니다.\n"
            "모델 응답이 비어 있거나 형식이 깨져 잠시 후 다시 시도해 주세요."
        )
    except Exception as exc:
        logger.exception("hanall news brief unexpected failure", exc_info=exc)
        if raise_on_error:
            raise FeatureExecutionError(
                "hanall_news_brief",
                "한올 뉴스 브리핑 생성 중 예기치 않은 오류가 발생해 방 전송을 생략했습니다.",
            ) from exc
        raise
