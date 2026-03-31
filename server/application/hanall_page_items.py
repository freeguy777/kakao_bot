from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from server.core.hanall_news_models import GeneratedKnownEvent, OfficialPageItem
from server.utils import now_kst

_MONTH_NAME_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\b", re.IGNORECASE)
_EVENT_COMPLETION_MARKERS = (
    "confirmed",
    "completed",
    "held",
    "filed",
    "발표 완료",
    "개최 완료",
    "완료",
    "완료됨",
    "확정",
    "종료",
)
_TZ_OFFSETS = {
    "KST": timezone(timedelta(hours=9)),
    "UTC": timezone.utc,
    "GMT": timezone.utc,
    "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
    "ET": timezone(timedelta(hours=-4)),
    "CST": timezone(timedelta(hours=-6)),
    "CDT": timezone(timedelta(hours=-5)),
    "CT": timezone(timedelta(hours=-5)),
    "PST": timezone(timedelta(hours=-8)),
    "PDT": timezone(timedelta(hours=-7)),
    "PT": timezone(timedelta(hours=-7)),
    "JST": timezone(timedelta(hours=9)),
}


def _normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_identity_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_space(value).lower())


def _normalized_title_token(value: Any, *, limit: int = 32) -> str:
    tokens = [token for token in re.split(r"[^a-z0-9]+", _normalize_space(value).lower()) if token]
    if not tokens:
        return ""
    return "".join(tokens[:4])[:limit]


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _parse_datetime_text(value: Any) -> tuple[datetime | None, bool]:
    text = _normalize_space(value)
    if not text:
        return None, False
    localized_tz = now_kst().tzinfo

    iso_match = re.search(
        r"(?P<year>\d{4})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})"
        r"(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?:\s*(?P<tz>[A-Z]{2,4}))?)?",
        text,
    )
    if iso_match:
        tzinfo = _TZ_OFFSETS.get((iso_match.group("tz") or "KST").upper(), localized_tz)
        hour = int(iso_match.group("hour") or 9)
        minute = int(iso_match.group("minute") or 0)
        parsed = datetime(
            int(iso_match.group("year")),
            int(iso_match.group("month")),
            int(iso_match.group("day")),
            hour,
            minute,
            tzinfo=tzinfo,
        ).astimezone(localized_tz)
        return parsed, iso_match.group("hour") is None

    korean_match = re.search(
        r"(?P<year>\d{4})\s*년\s*(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일"
        r"(?:\s*(?P<hour>\d{1,2})[:시](?P<minute>\d{2})?)?",
        text,
    )
    if korean_match:
        hour = int(korean_match.group("hour") or 9)
        minute = int(korean_match.group("minute") or 0)
        parsed = datetime(
            int(korean_match.group("year")),
            int(korean_match.group("month")),
            int(korean_match.group("day")),
            hour,
            minute,
            tzinfo=localized_tz,
        )
        return parsed, korean_match.group("hour") is None

    if _MONTH_NAME_RE.search(text):
        month_formats = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")
        month_text = text
        month_text = re.sub(r"\b(\d{1,2}:\d{2})\s*([AaPp][Mm])\b", r" \1 \2", month_text)
        month_text = re.sub(r"\s+", " ", month_text)
        time_match = re.search(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[AaPp][Mm])", month_text)
        tz_match = re.search(r"\b(?P<tz>EST|EDT|ET|UTC|GMT|KST|CST|CDT|CT|PST|PDT|PT|JST)\b", month_text)
        parsed_date: datetime | None = None
        cleaned = re.sub(r"\b\d{1,2}:\d{2}\s*[AaPp][Mm]\b", "", month_text)
        cleaned = re.sub(r"\b(?:EST|EDT|ET|UTC|GMT|KST|CST|CDT|CT|PST|PDT|PT|JST)\b", "", cleaned)
        cleaned = _normalize_space(cleaned.replace("|", " "))
        for month_format in month_formats:
            try:
                parsed_date = datetime.strptime(cleaned, month_format)
                break
            except ValueError:
                continue
        if parsed_date is not None:
            hour = 9
            minute = 0
            inferred_time = True
            if time_match:
                hour = int(time_match.group("hour"))
                if hour == 12:
                    hour = 0
                if time_match.group("ampm").lower() == "pm":
                    hour += 12
                minute = int(time_match.group("minute"))
                inferred_time = False
            tzinfo = _TZ_OFFSETS.get((tz_match.group("tz") if tz_match else "KST").upper(), localized_tz)
            parsed = parsed_date.replace(hour=hour, minute=minute, tzinfo=tzinfo).astimezone(localized_tz)
            return parsed, inferred_time

    return None, False


def parse_known_event_kst(value: Any) -> datetime | None:
    parsed, _ = _parse_datetime_text(value)
    return parsed


def build_page_item_identity(page_item: OfficialPageItem) -> tuple[str, dict[str, Any]]:
    title_token = _normalized_title_token(page_item.item_title)
    source_key = _normalize_identity_token(page_item.source_name) or "page"
    source_specific: dict[str, Any] = {
        "source_name": page_item.source_name,
        "item_type": page_item.item_type,
    }
    if page_item.item_type == "regulatory_notice":
        effective_datetime = page_item.accepted_at or page_item.published_at_kst or page_item.updated_at_kst or "-"
        key = "|".join(
            [
                source_key,
                "regulatory",
                _normalize_identity_token(page_item.document_id) or _normalize_identity_token(page_item.item_url or page_item.page_url),
                _normalize_identity_token(page_item.filing_type),
                _normalize_identity_token(effective_datetime),
            ]
        )
        source_specific.update(
            {
                "identity_type": "regulatory_notice",
                "document_id": page_item.document_id,
                "filing_type": page_item.filing_type,
                "accepted_at": page_item.accepted_at,
                "published_at_kst": page_item.published_at_kst,
            }
        )
        return key, source_specific
    if page_item.item_type == "trial_registry_update":
        effective_datetime = page_item.last_update_posted or page_item.updated_at_kst or page_item.published_at_kst or "-"
        key = "|".join(
            [
                source_key,
                "trial",
                _normalize_identity_token(page_item.trial_id) or _normalize_identity_token(page_item.item_url or page_item.page_url),
                _normalize_identity_token(effective_datetime),
            ]
        )
        source_specific.update(
            {
                "identity_type": "trial_registry_update",
                "trial_id": page_item.trial_id,
                "last_update_posted": page_item.last_update_posted,
                "updated_at_kst": page_item.updated_at_kst,
            }
        )
        return key, source_specific
    if page_item.item_type == "career_posting":
        effective_date = (page_item.published_at_kst or page_item.updated_at_kst or "-")[:10]
        key = "|".join(
            [
                source_key,
                "career",
                _normalize_identity_token(page_item.item_url or page_item.page_url),
                title_token,
                _normalize_identity_token(effective_date),
            ]
        )
        source_specific.update(
            {
                "identity_type": "career_posting",
                "item_url": page_item.item_url or page_item.page_url,
                "title_token": title_token,
                "posted_date": effective_date,
            }
        )
        return key, source_specific
    if page_item.item_type in {"press_release", "presentation", "earnings", "page_update", "pipeline_program"}:
        effective_datetime = page_item.published_at_kst or page_item.updated_at_kst or page_item.scheduled_for_kst or "-"
        key = "|".join(
            [
                source_key,
                _normalize_identity_token(page_item.item_type),
                _normalize_identity_token(page_item.item_url or page_item.page_url),
                title_token,
                _normalize_identity_token(effective_datetime),
            ]
        )
        source_specific.update(
            {
                "identity_type": page_item.item_type,
                "item_url": page_item.item_url or page_item.page_url,
                "title_token": title_token,
                "published_at_kst": page_item.published_at_kst,
                "updated_at_kst": page_item.updated_at_kst,
            }
        )
        return key, source_specific
    if page_item.item_type == "investor_event":
        key = "|".join(
            [
                source_key,
                "event",
                _normalize_identity_token(page_item.scheduled_for_kst),
                title_token,
            ]
        )
        source_specific.update(
            {
                "identity_type": "generated_event_seed",
                "scheduled_for_kst": page_item.scheduled_for_kst,
                "title_token": title_token,
            }
        )
        return key, source_specific
    key = "|".join(
        [
            source_key,
            _normalize_identity_token(page_item.item_type),
            _normalize_identity_token(page_item.item_url or page_item.page_url),
            title_token or _normalize_identity_token(page_item.item_title),
        ]
    )
    source_specific.update(
        {
            "identity_type": "generic_page_item",
            "item_url": page_item.item_url or page_item.page_url,
            "title_token": title_token,
        }
    )
    return key, source_specific


def build_page_item_fingerprint(page_item: OfficialPageItem) -> str:
    fingerprint_source = "|".join(
        [
            _normalize_space(page_item.item_identity_key).lower(),
            _normalize_space(page_item.source_name).lower(),
            _normalize_space(page_item.page_name).lower(),
            _normalize_space(page_item.item_title).lower(),
            _normalize_space(page_item.item_url or page_item.page_url).lower(),
            _normalize_space(page_item.item_type).lower(),
            _normalize_space(page_item.stage_status).lower(),
            _normalize_space(page_item.document_id).lower(),
            _normalize_space(page_item.document_type).lower(),
            _normalize_space(page_item.trial_id).lower(),
            _normalize_space(page_item.asset).lower(),
            _normalize_space(page_item.target_moa).lower(),
            _normalize_space(page_item.indication).lower(),
            _normalize_space(page_item.phase).lower(),
            _normalize_space(page_item.recruitment_status).lower(),
            _normalize_space(page_item.enrollment).lower(),
            _normalize_space(page_item.primary_completion_date).lower(),
            _normalize_space(page_item.last_update_posted).lower(),
            _normalize_space(",".join(page_item.site_countries)).lower(),
            _normalize_space(page_item.regulator).lower(),
            _normalize_space(page_item.exchange).lower(),
            _normalize_space(page_item.filed_at).lower(),
            _normalize_space(page_item.accepted_at).lower(),
            _normalize_space(page_item.event_action).lower(),
            _normalize_space(",".join(page_item.key_numbers)).lower(),
            _normalize_space(page_item.regulatory_phrase).lower(),
            _normalize_space(page_item.published_at_kst).lower(),
            _normalize_space(page_item.updated_at_kst).lower(),
            _normalize_space(page_item.scheduled_for_kst).lower(),
        ]
    )
    return hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()


def build_page_item_canonical_payload(page_item: OfficialPageItem) -> dict[str, Any]:
    return {
        "title": _normalize_space(page_item.item_title),
        "status": _normalize_space(page_item.event_status),
        "stage_status": _normalize_space(page_item.stage_status),
        "recruitment_status": _normalize_space(page_item.recruitment_status),
        "enrollment": _normalize_space(page_item.enrollment),
        "primary_completion_date": _normalize_space(page_item.primary_completion_date),
        "last_update_posted": _normalize_space(page_item.last_update_posted),
        "phase": _normalize_space(page_item.phase),
        "site_countries": [_normalize_space(country) for country in page_item.site_countries if _normalize_space(country)],
        "filing_type": _normalize_space(page_item.filing_type),
        "accepted_at": _normalize_space(page_item.accepted_at),
        "published_at_kst": _normalize_space(page_item.published_at_kst),
        "updated_at_kst": _normalize_space(page_item.updated_at_kst),
        "key_numbers": [_normalize_space(value) for value in page_item.key_numbers if _normalize_space(value)],
        "event_action": _normalize_space(page_item.event_action),
        "asset": _normalize_space(page_item.asset),
        "indication": _normalize_space(page_item.indication),
        "target_moa": _normalize_space(page_item.target_moa),
        "regulator": _normalize_space(page_item.regulator),
        "exchange": _normalize_space(page_item.exchange),
        "regulatory_phrase": _normalize_space(page_item.regulatory_phrase),
    }


def compute_changed_fields(
    current_payload: dict[str, Any],
    previous_payload: dict[str, Any] | None,
) -> list[str]:
    if not previous_payload:
        return []
    changed_fields: list[str] = []
    for field_name, current_value in current_payload.items():
        previous_value = previous_payload.get(field_name)
        if current_value != previous_value:
            changed_fields.append(field_name)
    priority = [
        "title",
        "status",
        "stage_status",
        "recruitment_status",
        "enrollment",
        "primary_completion_date",
        "last_update_posted",
        "phase",
        "site_countries",
        "filing_type",
        "accepted_at",
        "published_at_kst",
        "updated_at_kst",
        "key_numbers",
        "event_action",
        "asset",
        "indication",
        "target_moa",
    ]
    ordered = [field_name for field_name in priority if field_name in changed_fields]
    ordered.extend(field_name for field_name in changed_fields if field_name not in ordered)
    return ordered[:5]


def classify_event_freshness(
    *,
    scheduled_for_kst: str,
    fact: str,
    status_note: str,
    current_now: datetime | None = None,
) -> str:
    reference_now = current_now or now_kst()
    combined = _normalize_space(f"{fact} {status_note}").lower()
    if any(marker in combined for marker in _EVENT_COMPLETION_MARKERS):
        return "completed_unknown"
    scheduled_at = parse_known_event_kst(scheduled_for_kst)
    if scheduled_at is None:
        return "upcoming"
    localized = scheduled_at.astimezone(reference_now.tzinfo or now_kst().tzinfo)
    if localized.date() > reference_now.date():
        return "upcoming"
    if localized.date() == reference_now.date():
        return "due_today"
    return "stale"


def _generated_event_fact(title: str, freshness_status: str) -> tuple[str, str]:
    normalized_title = _normalize_space(title) or "공식 일정"
    if freshness_status == "due_today":
        return f"{normalized_title} 일정이 오늘 예정임", "오늘 예정, 결과 공지 또는 후속 자료 확인 필요"
    if freshness_status == "upcoming":
        return f"{normalized_title} 일정이 예정되어 있음", "공식 일정 페이지 기준 upcoming event"
    if freshness_status == "completed_unknown":
        return f"{normalized_title} 일정 완료 여부 추가 확인 필요", "공식 일정 페이지 기준 완료 여부 불명"
    return f"{normalized_title} 일정 예정일 경과, 후속 공시 확인 필요", "예정일 경과, 후속 공시 확인 필요"


def build_generated_known_events_from_page_items(
    page_items: list[OfficialPageItem],
    *,
    current_now: datetime | None = None,
) -> list[GeneratedKnownEvent]:
    reference_now = current_now or now_kst()
    generated_events: list[GeneratedKnownEvent] = []
    for page_item in page_items:
        if page_item.source_group != "company_official":
            continue
        if page_item.item_type not in {"investor_event", "earnings", "presentation"}:
            continue
        if not _has_value(page_item.scheduled_for_kst):
            continue
        fact, default_status_note = _generated_event_fact(page_item.item_title, page_item.event_status or "upcoming")
        basis_bits = [
            f"page_name={page_item.page_name}",
            f"item_type={page_item.item_type}",
            f"detection_method={page_item.detection_method or '-'}",
        ]
        if page_item.published_at_kst:
            basis_bits.append(f"published_at={page_item.published_at_kst}")
        if page_item.updated_at_kst:
            basis_bits.append(f"updated_at={page_item.updated_at_kst}")
        if page_item.raw_snapshot.get("time_inferred"):
            basis_bits.append("time inferred from date-only official event entry")
        freshness_status = classify_event_freshness(
            scheduled_for_kst=page_item.scheduled_for_kst,
            fact=fact,
            status_note=default_status_note,
            current_now=reference_now,
        )
        status_note = _generated_event_fact(page_item.item_title, freshness_status)[1]
        if page_item.raw_snapshot.get("time_inferred"):
            status_note = f"{status_note} | time inferred as 09:00 KST from date-only entry"
        event_date = page_item.scheduled_for_kst[:10]
        event_id = f"{_normalize_identity_token(page_item.entity)}:{_normalize_identity_token(page_item.item_type)}:{event_date}:{_normalize_identity_token(page_item.item_title)[:24]}"
        event_identity_key = "|".join(
            [
                _normalize_identity_token(page_item.entity),
                _normalize_identity_token(page_item.item_type),
                _normalize_identity_token(page_item.scheduled_for_kst),
                _normalized_title_token(page_item.item_title),
            ]
        )
        generated_events.append(
            GeneratedKnownEvent(
                event_id=event_id,
                entity=page_item.entity,
                category=page_item.item_type,
                scheduled_for_kst=page_item.scheduled_for_kst,
                fact=_generated_event_fact(page_item.item_title, freshness_status)[0],
                basis=" | ".join(basis_bits),
                primary_source=page_item.item_url or page_item.page_url,
                status_note=status_note,
                event_source_type="generated_official",
                stale=freshness_status == "stale",
                freshness_status=freshness_status,
                event_identity_key=event_identity_key,
                source_name=page_item.source_name,
                source_url=page_item.page_url,
            )
        )

    deduped: list[GeneratedKnownEvent] = []
    seen: set[str] = set()
    for event in sorted(generated_events, key=lambda item: (item.scheduled_for_kst, item.entity, item.category)):
        key = event.event_identity_key or _event_identity_key(event)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def _manual_yaml_event_to_generated_event(raw_event: dict[str, Any], *, current_now: datetime) -> GeneratedKnownEvent:
    scheduled_for_kst = _normalize_space(raw_event.get("scheduled_for_kst")) or "-"
    entity = _normalize_space(raw_event.get("entity")) or "-"
    category = _normalize_space(raw_event.get("category")) or "investor_event"
    fact = _normalize_space(raw_event.get("fact")) or "-"
    basis = _normalize_space(raw_event.get("basis")) or "-"
    primary_source = _normalize_space(raw_event.get("primary_source")) or "-"
    status_note = _normalize_space(raw_event.get("status_note")) or "-"
    freshness_status = classify_event_freshness(
        scheduled_for_kst=scheduled_for_kst,
        fact=fact,
        status_note=status_note,
        current_now=current_now,
    )
    if freshness_status == "stale":
        fact = f"{entity} 일정 예정일 경과, 후속 공시 확인 필요"
        status_note = "예정일 경과, 후속 공시 확인 필요"
    event_id = _normalize_space(raw_event.get("event_id")) or (
        f"{_normalize_identity_token(entity)}:{_normalize_identity_token(category)}:{scheduled_for_kst[:10]}"
    )
    return GeneratedKnownEvent(
        event_id=event_id,
        entity=entity,
        category=category,
        scheduled_for_kst=scheduled_for_kst,
        fact=fact,
        basis=basis,
        primary_source=primary_source,
        status_note=status_note,
        event_source_type="manual_yaml",
        stale=freshness_status == "stale",
        freshness_status=freshness_status,
        event_identity_key="|".join(
            [
                _normalize_identity_token(entity),
                _normalize_identity_token(category),
                _normalize_identity_token(scheduled_for_kst),
                _normalized_title_token(fact),
            ]
        ),
        source_name="local_known_events",
        source_url=primary_source,
    )


def _event_identity_key(event: GeneratedKnownEvent) -> str:
    if event.event_identity_key:
        return event.event_identity_key
    normalized_parts = [
        _normalize_identity_token(event.entity),
        _normalize_identity_token(event.category),
        _normalize_identity_token(event.scheduled_for_kst),
        _normalized_title_token(event.fact),
    ]
    return "|".join(normalized_parts) if any(normalized_parts) else event.event_id


def _event_coarse_identity_key(event: GeneratedKnownEvent) -> str:
    return "|".join(
        [
            _normalize_identity_token(event.entity),
            _normalize_identity_token(event.category),
            _normalize_identity_token(event.scheduled_for_kst),
        ]
    )


def merge_known_events(
    manual_yaml_events: list[dict[str, Any]],
    generated_events: list[GeneratedKnownEvent | dict[str, Any]],
    *,
    current_now: datetime | None = None,
) -> list[GeneratedKnownEvent]:
    reference_now = current_now or now_kst()
    merged_by_key: dict[str, GeneratedKnownEvent] = {}
    manual_key_by_coarse: dict[str, str] = {}

    for raw_event in manual_yaml_events:
        if not isinstance(raw_event, dict):
            continue
        manual_event = _manual_yaml_event_to_generated_event(raw_event, current_now=reference_now)
        key = _event_identity_key(manual_event)
        merged_by_key[key] = manual_event
        manual_key_by_coarse.setdefault(_event_coarse_identity_key(manual_event), key)

    for raw_event in generated_events:
        generated_event = raw_event if isinstance(raw_event, GeneratedKnownEvent) else GeneratedKnownEvent.model_validate(raw_event)
        key = _event_identity_key(generated_event)
        existing_key = key
        existing = merged_by_key.get(key)
        if existing is None:
            coarse_key = _event_coarse_identity_key(generated_event)
            existing_key = manual_key_by_coarse.get(coarse_key, key)
            existing = merged_by_key.get(existing_key)
        if existing is None:
            merged_by_key[key] = generated_event
            continue
        merged_payload = existing.model_dump(mode="json")
        for field_name, field_value in generated_event.model_dump(mode="json").items():
            if _has_value(field_value) or field_name in {"event_source_type", "stale", "freshness_status"}:
                merged_payload[field_name] = field_value
        for field_name, field_value in existing.model_dump(mode="json").items():
            if not _has_value(merged_payload.get(field_name)) and _has_value(field_value):
                merged_payload[field_name] = field_value
        merged_payload["event_source_type"] = generated_event.event_source_type or merged_payload.get("event_source_type")
        if existing_key != key and existing_key in merged_by_key:
            merged_by_key.pop(existing_key, None)
        merged_by_key[key] = GeneratedKnownEvent.model_validate(merged_payload)

    return sorted(merged_by_key.values(), key=lambda item: (item.scheduled_for_kst, item.entity, item.category))


def build_known_events_context(events: list[GeneratedKnownEvent | dict[str, Any]]) -> str:
    blocks: list[str] = []
    for raw_event in events:
        event = raw_event if isinstance(raw_event, GeneratedKnownEvent) else GeneratedKnownEvent.model_validate(raw_event)
        blocks.append(
            "\n".join(
                [
                    f"- entity: {event.entity}",
                    f"  category: {event.category}",
                    f"  scheduled_for_kst: {event.scheduled_for_kst}",
                    f"  fact: {event.fact}",
                    f"  basis: {event.basis}",
                    f"  primary_source: {event.primary_source}",
                    f"  status_note: {event.status_note}",
                ]
            )
        )
    if not blocks:
        return "- 오늘 날짜에 해당하는 로컬 예정 이벤트 없음"
    return "\n".join(blocks)
