from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from server.application.delivery import build_delivery_dedupe_key, deliver_room_messages, notify_admin_error
from server.application.youtube import collect_youtube_summary_messages, detect_message_features
from server.config import RoomConfig, resolve_room_policy
from server.core.contracts import MessageEventRequest, build_standard_response
from server.infra.sqlite_store import (
    has_processed_message,
    save_processed_message,
    save_room_target,
)
from server.utils import make_trace_id, safe_truncate

logger = logging.getLogger(__name__)


def _build_youtube_failure_message(failed_urls: list[str]) -> str:
    first_url = str(failed_urls[0]).strip() if failed_urls else ""
    message = "유튜브 요약을 지금 가져오지 못했습니다.\n잠시 후 다시 시도해 주세요."
    if first_url:
        message += f"\nurl: {first_url}"
    return message


def _normalize_failure_details(raw_details: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(raw_details, list):
        return normalized
    for item in raw_details:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "url": str(item.get("url", "")).strip(),
                "video_id": str(item.get("video_id", "")).strip(),
                "reason": str(item.get("reason", "")).strip(),
                "attempts": int(item.get("attempts", 1) or 1),
            }
        )
    return normalized


def _build_youtube_admin_failure_detail(
    *,
    failed_urls: list[str],
    failure_details: list[dict[str, Any]],
    had_successful_messages: bool,
) -> str:
    parts = [
        "유튜브 요약 부분 실패" if had_successful_messages else "유튜브 요약 실패",
        f"url_count={len(failed_urls)}",
    ]
    if failure_details:
        examples: list[str] = []
        for item in failure_details[:2]:
            url = str(item.get("url", "")).strip()
            attempts = int(item.get("attempts", 1) or 1)
            reason = safe_truncate(str(item.get("reason", "")).strip(), 180)
            detail = f"url={url}"
            if attempts > 1:
                detail += f" attempts={attempts}"
            if reason:
                detail += f" reason={reason}"
            examples.append(detail)
        if examples:
            parts.extend(examples)
    return safe_truncate(" | ".join(part for part in parts if part), 700)


@dataclass(slots=True)
class MessageEventUseCase:
    room_policy_resolver: Callable[[str | None, str | None], RoomConfig | None] = resolve_room_policy
    room_target_saver: Callable[[str, str | None, str | None], None] = save_room_target
    processed_message_checker: Callable[[str, str], bool] = has_processed_message
    processed_message_saver: Callable[[str, str, str, str, str], None] = save_processed_message
    feature_detector: Callable[[str], dict[str, Any]] = detect_message_features
    youtube_message_collector: Callable[..., Any] = collect_youtube_summary_messages
    room_message_deliverer: Callable[..., dict[str, Any]] = deliver_room_messages
    delivery_dedupe_key_builder: Callable[..., str] = build_delivery_dedupe_key
    admin_notifier: Callable[..., None] = notify_admin_error
    trace_id_factory: Callable[[], str] = make_trace_id

    def handle(self, payload: MessageEventRequest | dict[str, Any]) -> dict[str, Any]:
        request = payload if isinstance(payload, MessageEventRequest) else MessageEventRequest.model_validate(payload)
        trace_id = self.trace_id_factory()
        room = self.room_policy_resolver(request.room_name, request.channel_id)
        if room is None:
            return build_standard_response(
                ok=False,
                trace_id=trace_id,
                action="ignored",
                messages=[],
                error="등록되지 않은 방입니다. channel_id 또는 display_name 설정을 확인해 주세요.",
                meta={"room_name": request.room_name, "channel_id": request.channel_id},
            )

        room_key = room.room_key
        self.room_target_saver(room_key, request.room_name, request.channel_id)

        if not request.log_id:
            return build_standard_response(
                ok=False,
                trace_id=trace_id,
                action="rejected",
                messages=[],
                error="log_id가 비어 있습니다.",
                meta={"room_key": room_key},
            )

        if self.processed_message_checker(room_key, request.log_id):
            return build_standard_response(
                ok=True,
                trace_id=trace_id,
                action="duplicate_message",
                messages=[],
                error=None,
                meta={"room_key": room_key, "log_id": request.log_id},
            )

        self.processed_message_saver(
            room_key,
            request.log_id,
            safe_truncate(request.sender or "", 100),
            request.message,
            request.received_at or "",
        )

        features = self.feature_detector(request.message)
        messages: list[str] = []
        action = "ignored"
        meta: dict[str, Any] = {
            "room_key": room_key,
            "package_name": request.package_name,
            "features": features,
        }

        if features["has_youtube_url"]:
            if room.features.youtube_summary:
                action = "youtube_summary"
                result = self.youtube_message_collector(
                    room_key,
                    list(features["youtube_urls"]),
                    message_length_limit=room.delivery.message_length_limit,
                )
                messages = result.messages
                meta["youtube"] = {
                    "processed_video_ids": result.processed_video_ids,
                    "skipped_video_ids": result.skipped_video_ids,
                    "failed_urls": result.failed_urls,
                }
                failure_details = _normalize_failure_details(getattr(result, "failure_details", []))
                if failure_details:
                    meta["youtube"]["failure_details"] = failure_details
                if result.failed_urls:
                    if messages and not request.is_group_chat:
                        messages.append(_build_youtube_failure_message(result.failed_urls))
                    if not messages:
                        action = "youtube_summary_no_reply"
                        if not request.is_group_chat:
                            messages = [_build_youtube_failure_message(result.failed_urls)]
                    self.admin_notifier(
                        room_key=room_key,
                        feature_key="youtube_summary",
                        trace_id=trace_id,
                        detail=_build_youtube_admin_failure_detail(
                            failed_urls=result.failed_urls,
                            failure_details=failure_details,
                            had_successful_messages=bool(result.processed_video_ids),
                        ),
                        meta={
                            "failed_urls": result.failed_urls,
                            "failure_details": failure_details,
                            "event_log_id": request.log_id,
                            "sender": request.sender,
                            "is_group_chat": request.is_group_chat,
                        },
                    )
            else:
                action = "feature_disabled"

        if messages:
            queued_messages = list(messages)
            try:
                delivery_result = self.room_message_deliverer(
                    room_key=room_key,
                    messages=queued_messages,
                    trace_id=trace_id,
                    source_type=f"message_event:{action}",
                    meta={
                        "event_action": action,
                        "event_log_id": request.log_id,
                        "event_sender": request.sender,
                    },
                    dedupe_key=self.delivery_dedupe_key_builder(
                        room_key=room_key,
                        source_type=f"message_event:{action}",
                        messages=queued_messages,
                        hint=request.log_id or trace_id,
                    ),
                )
                meta["delivery"] = delivery_result
                meta["delivery_mode"] = delivery_result.get("via")
                meta["queued_message_count"] = len(queued_messages)
                if delivery_result.get("ok") or delivery_result.get("outbox_ids"):
                    messages = []
            except Exception as exc:
                logger.exception(
                    "message delivery queue failed trace_id=%s room_key=%s action=%s",
                    trace_id,
                    room_key,
                    action,
                    exc_info=exc,
                )
                meta["delivery"] = {
                    "ok": False,
                    "transport": "polling",
                    "via": "error",
                    "queued": False,
                    "delivered": False,
                    "trace_id": trace_id,
                    "room_key": room_key,
                    "messages": queued_messages,
                    "ack": None,
                    "error": str(exc),
                    "outbox_ids": [],
                }
                meta["delivery_mode"] = "error"

        logger.info(
            "message handled trace_id=%s room_key=%s sender=%s action=%s message_count=%s",
            trace_id,
            room_key,
            request.sender,
            action,
            len(messages),
        )
        return build_standard_response(
            ok=True,
            trace_id=trace_id,
            action=action,
            messages=messages,
            error=None,
            meta=meta,
        )
