from __future__ import annotations

from collections import Counter
import logging
import re
from datetime import timedelta

import requests

from server.application.delivery import deliver_room_messages
from server.application.errors import FeatureExecutionError
from server.application.hanall_reporting import build_ranked_issue_list
from server.application.hanall_news_pipeline import (
    _final_text_has_required_sections,
    normalize_hanall_final_text,
    run_hanall_news_pipeline,
)
from server.config import get_admin_room_key, get_room_policy
from server.utils import make_trace_id, now_kst, smart_truncate

logger = logging.getLogger(__name__)
PUBLIC_REQUIRED_SECTION_HEADINGS = (
    "📌 요약",
    "📅 오늘 예정 이벤트",
    "🏢 회사 직접 업데이트",
    "🧭 경쟁사 관련 업데이트",
    "🗺 경쟁 구도 한눈에 보기",
    "🔎 확인한 자료",
    "⚠ 추가 확인이 필요한 단서",
)


def get_news_summary() -> str:
    return "오늘 뉴스 요약: 주요 시장 이벤트와 종목 이슈를 추후 API 연동으로 대체할 예정입니다."


def _normalize_hanall_news_text(raw_text: str) -> str:
    normalized = normalize_hanall_final_text(raw_text)
    if not normalized:
        raise ValueError("LLM 응답이 비어 있습니다.")
    normalized = normalized.replace("**", "").replace("__", "").replace("`", "")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    if not normalized:
        raise ValueError("정리 후 브리핑 텍스트가 비어 있습니다.")
    if not normalized.startswith("[한올/Immunovant 24시간 브리핑]"):
        normalized = f"[한올/Immunovant 24시간 브리핑]\n{normalized}"
    if not _final_text_has_required_sections(normalized):
        raise ValueError("최종 브리핑 필수 섹션이 누락되었습니다.")
    return normalized


def _normalize_public_hanall_text(raw_text: str) -> str:
    normalized = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    normalized = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", normalized, count=1)
    normalized = re.sub(r"\s*```$", "", normalized, count=1)
    normalized = normalized.replace("**", "").replace("__", "").replace("`", "")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if normalized and not normalized.startswith("[한올/Immunovant 24시간 브리핑]"):
        normalized = f"[한올/Immunovant 24시간 브리핑]\n{normalized}"
    return normalized


def _public_text_has_required_sections(text: str) -> bool:
    normalized = _normalize_public_hanall_text(text)
    if not normalized:
        return False
    indexes = []
    for heading in PUBLIC_REQUIRED_SECTION_HEADINGS:
        match = re.search(rf"(?m)^{re.escape(heading)}$", normalized)
        if match is None:
            return False
        indexes.append(match.start())
    return indexes == sorted(indexes)


def _should_render_public_hanall_text(room_key: str | None) -> bool:
    normalized_room_key = str(room_key or "").strip()
    if not normalized_room_key:
        return False
    admin_room_key = str(get_admin_room_key(normalized_room_key) or "").strip()
    if not admin_room_key:
        return False
    return normalized_room_key != admin_room_key


def _public_source_label(source_group: str | None, source_name: str | None) -> str:
    normalized_group = str(source_group or "").strip()
    normalized_name = str(source_name or "").strip()
    group_map = {
        "company_official": "회사 공식 자료",
        "competitor_official": "경쟁사 공식 자료",
        "regulator_disclosure": "규제·공시 자료",
        "trial_registry": "임상 등록 자료",
        "discovery_only": "참고 자료",
    }
    name_map = {
        "clinicaltrials": "ClinicalTrials.gov",
        "sec_api": "SEC 공시",
        "sec": "SEC 공시",
        "opendart": "전자공시(OpenDART)",
        "company_official": "회사 공식 자료",
        "competitor_official": "경쟁사 공식 자료",
        "trial_registry": "임상 등록 자료",
        "regulator_disclosure": "규제·공시 자료",
        "discovery_only": "참고 자료",
    }
    return name_map.get(normalized_name) or group_map.get(normalized_group) or normalized_name or normalized_group or "확인 자료"


def _public_source_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "checked":
        return "확인 완료"
    if normalized.startswith("http_429"):
        return "응답 제한"
    if normalized.startswith("http_403"):
        return "접근 제한"
    if normalized.startswith("http_404"):
        return "조회 결과 없음"
    if normalized in {"request_error", "fetch_error", "collector_exception"}:
        return "연결 오류"
    if normalized in {"disabled", "approval_gated_disabled"}:
        return "현재 미사용"
    return "추가 확인 필요"


def _public_gap_type(gap_type: str | None) -> str:
    normalized = str(gap_type or "").strip().lower()
    if normalized.startswith("http_429"):
        return "응답 제한"
    if normalized.startswith("http_403"):
        return "접근 제한"
    if normalized.startswith("http_404"):
        return "조회 결과 없음"
    if normalized in {"request_error", "fetch_error"}:
        return "연결 오류"
    if normalized == "collector_exception":
        return "수집 예외"
    return "추가 확인 필요"


def _public_group_label(source_group: str | None) -> str:
    return _public_source_label(source_group, None)


def _public_omission_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"completed", "done", "deterministic_base", "defaulted"}:
        return "점검 완료"
    if normalized in {"monitoring", "in_progress"}:
        return "모니터링 중"
    if normalized == "open":
        return "후속 확인 필요"
    return "추가 확인 필요"


def _compact_join(parts: list[str]) -> str:
    return " | ".join([str(part).strip() for part in parts if str(part or "").strip()])


def _public_provenance_text(item) -> str | None:
    strengths = []
    for entries in getattr(item, "field_provenance", {}).values():
        for entry in entries:
            label = str(getattr(entry, "provenance_strength", "") or getattr(entry, "source_type", "")).strip()
            if not label or label in strengths:
                continue
            strengths.append(label)
    if not strengths:
        return None
    joined = ", ".join(strengths[:2])
    if "official" in joined or "regulator" in joined or "registry" in joined:
        return "확인 근거: 공식 자료 재확인"
    return f"확인 근거: {joined}"


def _render_public_titles_line(label: str, items: list, *, max_items: int) -> str:
    if not items:
        return f"- {label}: 없음"
    titles = []
    for item in items[:max_items]:
        entity = str(getattr(item, "entity", "") or "").strip()
        title = str(getattr(item, "title", "") or "").strip()
        if entity and title:
            titles.append("{0} '{1}'".format(entity, title))
        elif title:
            titles.append("'{0}'".format(title))
        elif entity:
            titles.append(entity)
    return "- {0} {1}건: {2}".format(label, len(items), ", ".join(titles) if titles else "없음")


def _render_public_summary_lines(stage1_output, *, max_items: int) -> list[str]:
    lines: list[str] = []
    unverified_count = len(stage1_output.unverified_leads)

    lines.append(
        _render_public_titles_line(
            "회사 직접 업데이트",
            stage1_output.company_direct_confirmed,
            max_items=max_items,
        )
    )
    lines.append(
        _render_public_titles_line(
            "경쟁사 관련 업데이트",
            stage1_output.competitor_relevant_confirmed,
            max_items=max_items,
        )
    )
    if stage1_output.today_scheduled_events:
        lines.append(
            _render_public_titles_line(
                "오늘 예정 이벤트",
                stage1_output.today_scheduled_events,
                max_items=max_items,
            )
        )
    if unverified_count:
        lines.append(f"- 아직 공식 자료로 확정하지 못한 단서가 {unverified_count}건 있어 후속 확인이 필요합니다.")

    ranked = build_ranked_issue_list(stage1_output=stage1_output, limit=max_items)
    for item in ranked[:max_items]:
        detail_bits = [item.get("asset"), item.get("indication"), item.get("event_action")]
        detail_text = ", ".join(str(bit).strip() for bit in detail_bits if str(bit or "").strip())
        lines.append(
            f"- 핵심 이슈: {item.get('entity') or '-'} 관련 '{item.get('title') or '-'}'"
            + (f" ({detail_text})" if detail_text else "")
        )
    return lines[: max_items + 5]


def _render_public_stage_item(item) -> list[str]:
    lines = [f"- {item.entity}: {item.title}"]
    lines.append(
        _compact_join(
            [
                f"확인 시각: {item.published_at_kst or item.updated_at_kst}",
                f"출처: {_public_source_label(item.source_group, item.source_name)}",
            ]
        )
    )

    key_bits = [
        f"시험 번호: {item.trial_id}" if item.trial_id else "",
        f"문서 번호: {item.document_id}" if item.document_id else "",
        f"대상 약물: {item.asset}" if item.asset else "",
        f"대상 질환: {item.indication}" if item.indication else "",
        f"지역: {item.region}" if item.region else "",
        f"개발 단계: {item.stage_status}" if item.stage_status else "",
        f"임상 단계: {item.phase}" if item.phase else "",
        f"환자 모집: {item.recruitment_status}" if item.recruitment_status else "",
        f"참여 규모: {item.enrollment}" if item.enrollment else "",
        f"주요 완료 예정일: {item.primary_completion_date}" if item.primary_completion_date else "",
        f"최근 등록 갱신일: {item.last_update_posted}" if item.last_update_posted else "",
        f"규제기관: {item.regulator}" if item.regulator else "",
        f"거래소: {item.exchange}" if item.exchange else "",
        f"공시 종류: {item.filing_type}" if item.filing_type else "",
        f"제출 시각: {item.filed_at}" if item.filed_at else "",
        f"접수 시각: {item.accepted_at}" if item.accepted_at else "",
        f"핵심 조치: {item.event_action}" if item.event_action else "",
    ]
    key_line = _compact_join(key_bits[:6])
    if key_line:
        lines.append(key_line)
    secondary_line = _compact_join(key_bits[6:])
    if secondary_line:
        lines.append(secondary_line)
    if item.site_countries:
        lines.append(f"진행 국가: {', '.join(item.site_countries)}")
    if item.changed_fields:
        lines.append(f"최근 바뀐 항목: {', '.join(item.changed_fields[:5])}")
    if item.target_moa:
        lines.append(f"작용 방식: {item.target_moa}")
    if item.regulatory_phrase:
        lines.append(f"규제 문구: {item.regulatory_phrase}")
    if item.key_numbers:
        lines.append(f"핵심 수치: {', '.join(item.key_numbers[:4])}")
    provenance_text = _public_provenance_text(item)
    if provenance_text:
        lines.append(provenance_text)
    if item.summary or item.source_note:
        lines.append(f"핵심 내용: {smart_truncate(item.summary or item.source_note or '', 180)}")
    lines.append(f"원문 링크: {item.primary_source_url or '-'}")
    return [line for line in lines if str(line or "").strip()]


def _render_public_unverified_item(item) -> list[str]:
    lines = [f"- {item.entity}: {item.title}"]
    if item.reason_unverified:
        lines.append(f"아직 확정하지 못한 이유: {smart_truncate(item.reason_unverified, 140)}")
    if item.missing_verification_target:
        lines.append(f"추가로 필요한 확인: {item.missing_verification_target}")
    if item.suggested_official_followup_queries:
        lines.append("다음 확인 검색어: " + ", ".join(item.suggested_official_followup_queries[:2]))
    lines.append(f"원문 링크: {item.primary_source_url or '-'}")
    return [line for line in lines if str(line or "").strip()]


def _render_public_stage_block(title: str, items: list, *, empty_text: str, max_items: int) -> list[str]:
    lines = [title]
    if not items:
        lines.append(empty_text)
        return lines
    for item in items[:max_items]:
        if title == "⚠ 추가 확인이 필요한 단서":
            lines.extend(_render_public_unverified_item(item))
        else:
            lines.extend(_render_public_stage_item(item))
    if len(items) > max_items:
        lines.append(f"- 그 외 {len(items) - max_items}건은 관리자용 상세 브리핑에서 계속 확인할 수 있습니다.")
    return lines


def _render_public_competitor_map(entries: list, *, max_items: int) -> list[str]:
    lines = ["🗺 경쟁 구도 한눈에 보기"]
    if not entries:
        lines.append("- 현재 추적 중인 경쟁사 스냅샷 없음")
        return lines
    for entry in entries[:max_items]:
        lines.append(f"- {entry.competitor}")
        lines.append(
            _compact_join(
                [
                    f"약물: {entry.asset}" if entry.asset else "약물: 확인 중",
                    f"질환: {entry.indication}" if entry.indication else "질환: 확인 중",
                ]
            )
        )
        lines.append(f"단계: {entry.stage_status or '확인 중'}")
        detail = _compact_join(
            [
                f"구분: {entry.layer}" if entry.layer else "",
                f"지역: {entry.region}" if entry.region else "",
            ]
        )
        if detail:
            lines.append(detail)
        if entry.target_moa:
            lines.append(f"작용 방식: {entry.target_moa}")
    return lines


def _render_public_source_logs(entries: list, *, max_items: int) -> list[str]:
    lines = ["🔎 확인한 자료"]
    if not entries:
        lines.append("- 이번 점검에서 확인한 자료가 없습니다.")
        return lines
    group_counter = Counter(_public_group_label(getattr(entry, "source_group", None)) for entry in entries)
    status_counter = Counter(_public_source_status(getattr(entry, "status", "")) for entry in entries)
    lines.append(f"- 점검한 자료는 총 {len(entries)}곳입니다.")
    lines.append(
        "- 자료 구분: "
        + ", ".join(f"{group} {count}곳" for group, count in group_counter.most_common(4))
    )
    lines.append(
        "- 점검 결과: "
        + ", ".join(f"{status} {count}건" for status, count in status_counter.most_common(4))
    )
    for entry in entries[:max_items]:
        note_bits = [
            _public_source_label(getattr(entry, "source_group", None), getattr(entry, "source_name", None)),
            _public_source_status(getattr(entry, "status", None)),
            str(getattr(entry, "checked_at_kst", "") or "").strip(),
        ]
        lines.append("- " + _compact_join(note_bits))
    return lines


def _render_public_coverage_gaps(entries: list, *, max_items: int) -> list[str]:
    lines = ["Coverage Gaps"]
    if not entries:
        lines.append("- 이번 점검에서 큰 공백은 없었습니다.")
        return lines
    lines.append(f"- 추가 확인이 필요한 항목은 총 {len(entries)}건입니다.")
    lines.append("- 세부 점검 공백과 기술적인 확인 항목은 관리자방 상세 브리핑에서 따로 확인합니다.")
    return lines


def _render_public_omission_audit(entries: list, *, max_items: int) -> list[str]:
    lines = ["Omission Audit"]
    if not entries:
        lines.append("- 현재 누락 점검상 특이사항은 없습니다.")
        return lines
    axis_counter = Counter(str(entry.axis or "기타") for entry in entries)
    lines.append(
        "- 점검 축: "
        + ", ".join(f"{axis} {count}건" for axis, count in axis_counter.most_common(3))
    )
    for entry in entries[:max_items]:
        scope = " / ".join(
            [
                str(entry.axis or "").strip(),
                str(entry.source_group or "").strip(),
                str(entry.indication or "").strip(),
                str(entry.region or "").strip(),
            ]
        )
        scope = " / ".join([part for part in scope.split(" / ") if part])
        lines.append(
            "- "
            + _compact_join(
                [
                    smart_truncate(str(entry.topic or "누락 점검"), 50),
                    _public_omission_status(getattr(entry, "status", None)),
                    scope,
                ]
            )
        )
    return lines


def _build_public_hanall_news_text(*, room_key: str, pipeline_result) -> str:
    stage1_output = pipeline_result.stage1_output
    room = get_room_policy(room_key)
    max_items = max(3, min(int(getattr(getattr(room, "news", None), "max_items", 5) or 5), 5))
    current_now = now_kst()
    window_start = current_now - timedelta(hours=24)
    company_count = len(stage1_output.company_direct_confirmed)
    competitor_count = len(stage1_output.competitor_relevant_confirmed)
    merged_source_logs = list(stage1_output.checked_source_log) + list(pipeline_result.rss_collection.checked_source_log)

    lines = [
        "[한올/Immunovant 24시간 브리핑]",
        "📌 요약",
        f"1. 기준: {current_now.strftime('%Y-%m-%d %H:%M KST')}",
        f"2. 범위: {window_start.strftime('%Y-%m-%d %H:%M KST')} ~ {current_now.strftime('%Y-%m-%d %H:%M KST')}",
        f"3. 커버리지: {stage1_output.coverage.level} ({stage1_output.coverage.rationale})",
        (
            f"4. 공식 자료로 확인된 새 소식: 총 {company_count + competitor_count}건 "
            f"(회사 {company_count}건, 경쟁사 {competitor_count}건)"
        ),
        f"5. 오늘 예정 이벤트: {len(stage1_output.today_scheduled_events)}건",
    ]
    lines.extend(_render_public_summary_lines(stage1_output, max_items=max_items))
    lines.append("")
    lines.extend(
        _render_public_stage_block(
            "📅 오늘 예정 이벤트",
            stage1_output.today_scheduled_events,
            empty_text="- 오늘 예정된 일정은 없거나, 추가 확인이 필요한 상태입니다.",
            max_items=max_items,
        )
    )
    lines.append("")
    lines.extend(
        _render_public_stage_block(
            "🏢 회사 직접 업데이트",
            stage1_output.company_direct_confirmed,
            empty_text="- 지난 24시간 동안 회사가 직접 확인해 준 새 소식은 없었습니다.",
            max_items=max_items,
        )
    )
    lines.append("")
    lines.extend(
        _render_public_stage_block(
            "🧭 경쟁사 관련 업데이트",
            stage1_output.competitor_relevant_confirmed,
            empty_text="- 지난 24시간 동안 경쟁사 쪽 핵심 업데이트는 크지 않았습니다.",
            max_items=max_items,
        )
    )
    lines.append("")
    lines.extend(_render_public_competitor_map(stage1_output.competitor_map_snapshot, max_items=max_items))
    lines.append("")
    lines.extend(_render_public_source_logs(merged_source_logs, max_items=min(max_items, 4)))
    lines.append("")
    lines.extend(
        _render_public_stage_block(
            "⚠ 추가 확인이 필요한 단서",
            stage1_output.unverified_leads,
            empty_text="- 현재 추가 확인이 필요한 단서는 많지 않습니다.",
            max_items=max_items,
        )
    )
    public_text = _normalize_public_hanall_text("\n".join(lines).strip())
    if not _public_text_has_required_sections(public_text):
        return _normalize_hanall_news_text(pipeline_result.final_text)
    return public_text


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


def _send_hanall_news_detailed_to_admin(*, room_key: str | None, detailed_text: str) -> None:
    source_room_key = str(room_key or "").strip()
    admin_room_key = str(get_admin_room_key(source_room_key) or "").strip()
    if not source_room_key or not admin_room_key or source_room_key == admin_room_key:
        return

    message = (
        "[한올 브리핑 상세본]\n"
        f"source_room_key: {source_room_key}\n"
        f"captured_at: {now_kst().strftime('%Y-%m-%d %H:%M:%S KST')}\n\n"
        f"{str(detailed_text or '').strip()}"
    )
    try:
        deliver_room_messages(
            room_key=admin_room_key,
            message=message,
            trace_id=make_trace_id(),
            source_type="admin:hanall_news_detailed_copy",
            meta={
                "source_room_key": source_room_key,
                "payload_kind": "detailed_brief",
            },
            allow_fallback=True,
        )
    except Exception as exc:
        logger.warning(
            "failed to enqueue hanall detailed payload to admin room source_room_key=%s admin_room_key=%s error=%s",
            source_room_key,
            admin_room_key,
            exc,
        )


def build_hanall_news_brief(
    room_key: str | None = None,
    *,
    raise_on_error: bool = False,
    send_raw_to_admin: bool = False,
    send_detailed_to_admin: bool = False,
) -> str:
    try:
        pipeline_result = run_hanall_news_pipeline(room_key=room_key)
        raw_response = pipeline_result.raw_output_text
        detailed_text = _normalize_hanall_news_text(pipeline_result.final_text)
        if send_raw_to_admin:
            _send_hanall_news_raw_to_admin(room_key=room_key, raw_text=raw_response)
        if send_detailed_to_admin:
            _send_hanall_news_detailed_to_admin(room_key=room_key, detailed_text=detailed_text)
        if _should_render_public_hanall_text(room_key):
            normalized = _build_public_hanall_news_text(
                room_key=str(room_key),
                pipeline_result=pipeline_result,
            )
        else:
            normalized = detailed_text
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
