from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from server.application.hanall_competitor_universe import normalize_monitor_indication, normalize_monitor_region
from server.application.hanall_page_items import (
    build_page_item_fingerprint,
    build_page_item_identity,
    classify_event_freshness,
    parse_known_event_kst,
)
from server.application.hanall_research import (
    COMPETITOR_TERMS,
    DIRECT_TERMS,
    _extract_anchor_items,
    _extract_visible_lines,
)
from server.core.hanall_news_models import OfficialPageItem, RawFinding
from server.utils import now_kst

_ITEM_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pipeline_program", ("pipeline", "program", "clinical candidates", "development pipeline")),
    ("press_release", ("press release", "press-release", "news release", "보도자료", "news ")),
    ("presentation", ("presentation", "slides", "deck", "conference presentation")),
    ("earnings", ("earnings", "financial results", "quarter results", "webcast", "conference call")),
    ("investor_event", ("event", "calendar", "conference", "webcast", "fireside", "meeting")),
    ("career_posting", ("career", "careers", "job", "hiring", "recruit", "채용")),
    ("analyst_coverage_update", ("analyst", "coverage", "research")),
    ("trial_registry_update", ("trial", "registry", "study", "recruiting", "status")),
    ("regulatory_notice", ("filing", "disclosure", "notice", "공시", "report")),
)
_ASSET_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("IMVT-1402", ("imvt-1402", "hl161ans")),
    ("HBM9161", ("hbm9161",)),
    ("batoclimab", ("batoclimab", "hl161", "imvt-1401", "rvt-1401")),
    ("tanfanercept", ("tanfanercept", "hl036")),
    ("efgartigimod", ("efgartigimod", "vyvgart")),
    ("rozanolixizumab", ("rozanolixizumab", "rystiggo")),
    ("nipocalimab", ("nipocalimab", "m281")),
    ("teprotumumab", ("teprotumumab", "tepezza")),
)
_TRIAL_ID_PATTERNS = (
    re.compile(r"\b(NCT\d{8})\b", re.IGNORECASE),
    re.compile(r"\b(jRCT\d{10,})\b", re.IGNORECASE),
    re.compile(r"\b(ChiCTR\d{6,})\b", re.IGNORECASE),
    re.compile(r"\b(EUCTR\d{4}-\d{6}-\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(CTIS[-\s]?\d{4}[-\d]+)\b", re.IGNORECASE),
)
_PHASE_PATTERNS = (
    re.compile(r"\b(phase\s*[1234](?:/[1234])?[ab]?)\b", re.IGNORECASE),
    re.compile(r"\b(pivotal)\b", re.IGNORECASE),
)
_DOCUMENT_ID_PATTERN = re.compile(r"\b(\d{5,})\b")
_EVENT_PAGE_SOURCES = {"hanall_ir", "hanall_events", "immunovant_ir_calendar", "immunovant_news_events"}
_COMPANY_PAGE_SOURCES = {
    "hanall_website",
    "hanall_newsroom",
    "hanall_ir",
    "hanall_events",
    "hanall_careers",
    "immunovant_investors",
    "immunovant_press_releases",
    "immunovant_news_events",
    "immunovant_presentations",
    "immunovant_ir_calendar",
    "immunovant_careers",
}
_REGULATOR_LABEL_BY_SOURCE_NAME = {
    "krx": "KRX",
    "kind": "KIND",
    "ema": "EMA",
    "pmda_mhlw": "PMDA/MHLW",
    "nmpa": "NMPA",
    "ctis": "CTIS",
    "jrct": "jRCT",
    "chictr": "ChiCTR",
    "who_ictrp": "WHO ICTRP",
}
_REGION_BY_SOURCE_NAME = {
    "hanall_website": "KR",
    "hanall_newsroom": "KR",
    "hanall_ir": "KR",
    "hanall_events": "KR",
    "hanall_careers": "KR",
    "immunovant_investors": "US",
    "immunovant_press_releases": "US",
    "immunovant_news_events": "US",
    "immunovant_presentations": "US",
    "immunovant_ir_calendar": "US",
    "immunovant_careers": "US",
    "krx": "KR",
    "kind": "KR",
    "ema": "EU",
    "pmda_mhlw": "JP",
    "nmpa": "CN",
    "ctis": "EU",
    "jrct": "JP",
    "chictr": "CN",
    "who_ictrp": "US",
    "roivant_official": "US",
    "roivant_investors": "US",
    "harbour_biomed_pipeline": "CN",
    "harbour_biomed_news": "CN",
    "daewoong_official": "KR",
    "argenx_official": "EU",
    "ucb_official": "EU",
    "jnj_official": "US",
    "amgen_official": "US",
}


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_space(value).lower())


def _source_page_name(source_config: dict[str, Any], source_name: str) -> str:
    return _safe_text(source_config.get("page_name") or source_config.get("source_label") or source_name) or source_name


def _matching_keywords(source_config: dict[str, Any], field_name: str) -> list[str]:
    value = source_config.get(field_name)
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item or "").strip()]
    text = _safe_text(value).lower()
    return [text] if text else []


def _max_items_per_page(source_config: dict[str, Any], *, default: int = 1) -> int:
    raw_value = source_config.get("max_items_per_page", default)
    try:
        return max(1, min(int(raw_value), 10))
    except (TypeError, ValueError):
        return default


def _score_anchor(label: str, href: str, *, include_keywords: list[str], exclude_keywords: list[str], source_name: str) -> int:
    lowered = f"{label} {href}".lower()
    if exclude_keywords and any(keyword in lowered for keyword in exclude_keywords):
        return -999
    score = 1
    if include_keywords and any(keyword in lowered for keyword in include_keywords):
        score += 6
    if source_name in _EVENT_PAGE_SOURCES and any(keyword in lowered for keyword in ("event", "conference", "calendar", "meeting", "webcast", "presentation")):
        score += 5
    if source_name in {"krx", "kind"} and any(keyword in lowered for keyword in ("view more", "공시", "report", "notice")):
        score += 4
    if any(pattern.search(label) for pattern in _TRIAL_ID_PATTERNS):
        score += 5
    if len(_safe_text(label)) >= 18:
        score += 2
    return score


def _best_anchor_candidates(
    *,
    html_text: str,
    base_url: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    source_name: str,
    limit: int,
) -> list[tuple[str, str]]:
    scored: list[tuple[int, str, str]] = []
    for label, href in _extract_anchor_items(html_text, base_url):
        score = _score_anchor(label, href, include_keywords=include_keywords, exclude_keywords=exclude_keywords, source_name=source_name)
        if score < 0 or len(_safe_text(label)) < 4:
            continue
        scored.append((score, label, href))
    scored.sort(key=lambda item: item[0], reverse=True)
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, label, href in scored:
        key = f"{_normalize_key(label)}|{href}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append((label, href))
        if len(deduped) >= limit:
            break
    return deduped


def _search_lines_for_title_context(title: str, visible_lines: list[str]) -> list[str]:
    normalized_tokens = [token for token in re.split(r"\W+", title.lower()) if len(token) >= 4][:4]
    if not normalized_tokens:
        return visible_lines[:8]
    matched_indexes: list[int] = []
    for index, line in enumerate(visible_lines):
        lowered = line.lower()
        if any(token in lowered for token in normalized_tokens):
            matched_indexes.append(index)
        if len(matched_indexes) >= 3:
            break
    if not matched_indexes:
        return visible_lines[:8]
    context_lines: list[str] = []
    seen_indexes: set[int] = set()
    for match_index in matched_indexes:
        for candidate_index in range(max(0, match_index - 2), min(len(visible_lines), match_index + 4)):
            if candidate_index in seen_indexes:
                continue
            seen_indexes.add(candidate_index)
            context_lines.append(visible_lines[candidate_index])
            if len(context_lines) >= 8:
                return context_lines
    return context_lines or visible_lines[:8]


def _parse_datetime_from_lines(title: str, visible_lines: list[str]) -> tuple[datetime | None, bool]:
    for line in _search_lines_for_title_context(title, visible_lines):
        parsed = parse_known_event_kst(line)
        if parsed is not None:
            return parsed, not bool(re.search(r"\d{1,2}:\d{2}", line))
    return None, False


def _infer_item_type(*, source_name: str, page_name: str, title: str, url: str) -> str:
    lowered = f"{source_name} {page_name} {title} {url}".lower()
    for item_type, keywords in _ITEM_TYPE_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return item_type
    if source_name in {"krx", "kind"}:
        return "regulatory_notice"
    if source_name in {"ema", "pmda_mhlw", "nmpa"}:
        return "regulatory_notice"
    if source_name in {"ctis", "jrct", "chictr", "who_ictrp"}:
        return "trial_registry_update"
    return "page_update"


def _infer_asset(*values: Any) -> str | None:
    lowered = " ".join(_safe_text(value).lower() for value in values if _safe_text(value))
    if not lowered:
        return None
    for asset, aliases in _ASSET_RULES:
        if any(alias in lowered for alias in aliases):
            return asset
    return None


def _infer_entity(*, source_name: str, source_group: str, configured_entity: str, title: str, url: str, visible_lines: list[str]) -> str:
    combined = " ".join([source_name, source_group, configured_entity, title, url, " ".join(visible_lines[:5])]).lower()
    if any(term in combined for term in DIRECT_TERMS):
        if "immunovant" in combined or "imvt" in combined:
            return "Immunovant"
        return "HanAll Biopharma"
    if configured_entity:
        return configured_entity
    for competitor_term in COMPETITOR_TERMS:
        if competitor_term in combined:
            return competitor_term.title()
    return "HanAll/Immunovant watch"


def _infer_region(source_name: str, source_group: str, *values: Any) -> str | None:
    text_value = " ".join(_safe_text(value) for value in values if _safe_text(value))
    return normalize_monitor_region(text_value) or _REGION_BY_SOURCE_NAME.get(source_name) or normalize_monitor_region(source_group)


def _extract_trial_id(*values: Any) -> str | None:
    for value in values:
        text = _safe_text(value)
        for pattern in _TRIAL_ID_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1)
    return None


def _extract_phase(*values: Any) -> str | None:
    for value in values:
        text = _safe_text(value)
        for pattern in _PHASE_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1)
    return None


def _extract_sponsor(visible_lines: list[str]) -> str | None:
    for line in visible_lines:
        match = re.search(r"(?:sponsor|company|기관|기관명)\s*[:|-]\s*(?P<value>.+)$", line, re.IGNORECASE)
        if match:
            return _safe_text(match.group("value"))
    return None


def _extract_status(visible_lines: list[str]) -> str | None:
    for line in visible_lines:
        match = re.search(r"(?:status|recruitment|모집상태)\s*[:|-]\s*(?P<value>.+)$", line, re.IGNORECASE)
        if match:
            return _safe_text(match.group("value"))
    return None


def _extract_document_id(url: str, title: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("noticeNo", "no", "docNo", "documentNo"):
        values = query.get(key)
        if values:
            return _safe_text(values[0])
    match = _DOCUMENT_ID_PATTERN.search(title)
    return match.group(1) if match else None


def _normalize_filing_type(title: str) -> str | None:
    lowered = title.lower()
    if "shareholder" in lowered or "주주총회" in lowered:
        return "shareholder_meeting_notice"
    if "earnings" in lowered or "financial" in lowered or "실적" in lowered:
        return "earnings_notice"
    if "presentation" in lowered:
        return "presentation_notice"
    if "report" in lowered or "공시" in lowered or "filing" in lowered or "notice" in lowered:
        return "regulatory_notice"
    return None


def _regulator_label(source_name: str) -> str | None:
    return _REGULATOR_LABEL_BY_SOURCE_NAME.get(source_name)


def _build_page_item(
    *,
    source_name: str,
    source_group: str,
    page_name: str,
    page_url: str,
    title: str,
    item_url: str,
    entity: str,
    item_type: str,
    visible_lines: list[str],
    published_at: datetime | None = None,
    updated_at: datetime | None = None,
    scheduled_for: datetime | None = None,
    time_inferred: bool = False,
    configured_entity: str = "",
) -> OfficialPageItem:
    context_lines = _search_lines_for_title_context(title, visible_lines)
    trial_id = _extract_trial_id(title, item_url, *context_lines)
    asset = _infer_asset(title, item_url, *context_lines)
    indication = normalize_monitor_indication(" ".join([title, " ".join(context_lines)]))
    sponsor = _extract_sponsor(context_lines)
    item = OfficialPageItem(
        source_name=source_name,
        source_group=source_group,
        page_name=page_name,
        page_url=page_url,
        item_title=title,
        item_url=item_url or page_url,
        item_type=item_type,
        entity=entity,
        related_company=configured_entity or None,
        asset=asset,
        indication=indication,
        region=_infer_region(source_name, source_group, title, item_url, configured_entity),
        stage_status=None,
        document_id=_extract_document_id(item_url, title) if item_type == "regulatory_notice" else None,
        document_type="regulatory_notice" if item_type == "regulatory_notice" else None,
        filing_type=_normalize_filing_type(title) if item_type == "regulatory_notice" else None,
        trial_id=trial_id,
        sponsor=sponsor,
        target_moa=None,
        phase=_extract_phase(title, *context_lines),
        recruitment_status=_extract_status(context_lines),
        regulator=_regulator_label(source_name),
        published_at=published_at,
        updated_at=updated_at,
        scheduled_for=scheduled_for,
        event_status=(
            classify_event_freshness(
                scheduled_for_kst=scheduled_for.astimezone(now_kst().tzinfo).strftime("%Y-%m-%d %H:%M KST"),
                fact=title,
                status_note="",
            )
            if scheduled_for is not None
            else None
        ),
        detection_method="html_anchor_and_visible_line",
        raw_snapshot={
            "context_lines": context_lines[:4],
            "time_inferred": bool(time_inferred),
        },
    )
    item.item_identity_key, item.source_specific_identity_json = build_page_item_identity(item)
    item.content_fingerprint = build_page_item_fingerprint(item)
    return item


def _parse_calendar_items(
    *,
    source_name: str,
    source_group: str,
    page_name: str,
    page_url: str,
    configured_entity: str,
    html_text: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    max_items: int,
) -> list[OfficialPageItem]:
    visible_lines = _extract_visible_lines(html_text)
    items: list[OfficialPageItem] = []

    if source_name in {"hanall_ir", "hanall_events"}:
        for index, line in enumerate(visible_lines):
            if _normalize_key(line) != "upcoming":
                continue
            raw_date_line = visible_lines[index - 1] if index > 0 else ""
            scheduled_for = parse_known_event_kst(raw_date_line)
            title = _safe_text(visible_lines[index + 1] if index + 1 < len(visible_lines) else "")
            if not scheduled_for or not title:
                continue
            items.append(
                _build_page_item(
                    source_name=source_name,
                    source_group=source_group,
                    page_name=page_name,
                    page_url=page_url,
                    title=title,
                    item_url=page_url,
                    entity=configured_entity or "HanAll Biopharma",
                    item_type="investor_event",
                    visible_lines=visible_lines,
                    scheduled_for=scheduled_for,
                    time_inferred=scheduled_for is not None and not bool(re.search(r"\d{1,2}:\d{2}", raw_date_line)),
                    configured_entity=configured_entity,
                )
            )
            if len(items) >= max_items:
                break
        return items

    for label, href in _best_anchor_candidates(
        html_text=html_text,
        base_url=page_url,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        source_name=source_name,
        limit=max(max_items, 3),
    ):
        title = _safe_text(label)
        scheduled_for, time_inferred = _parse_datetime_from_lines(title, visible_lines)
        if scheduled_for is None:
            scheduled_for = parse_known_event_kst(title)
            time_inferred = scheduled_for is not None and not bool(re.search(r"\d{1,2}:\d{2}", title))
        if scheduled_for is None:
            continue
        items.append(
            _build_page_item(
                source_name=source_name,
                source_group=source_group,
                page_name=page_name,
                page_url=page_url,
                title=title,
                item_url=href,
                entity=configured_entity or "Immunovant",
                item_type=_infer_item_type(source_name=source_name, page_name=page_name, title=title, url=href),
                visible_lines=visible_lines,
                scheduled_for=scheduled_for,
                time_inferred=time_inferred,
                configured_entity=configured_entity,
            )
        )
        if len(items) >= max_items:
            break
    return items


def _parse_regulatory_items(
    *,
    source_name: str,
    source_group: str,
    page_name: str,
    page_url: str,
    configured_entity: str,
    html_text: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    max_items: int,
) -> list[OfficialPageItem]:
    visible_lines = _extract_visible_lines(html_text)
    items: list[OfficialPageItem] = []
    for label, href in _best_anchor_candidates(
        html_text=html_text,
        base_url=page_url,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        source_name=source_name,
        limit=max(max_items, 4),
    ):
        match = re.match(r"^(?P<no>\d+)\s+(?P<date>\d{4}[./-]\d{2}[./-]\d{2}(?:\s+\d{2}:\d{2})?)\s+(?P<title>.+?)(?:\s+VIEW MORE)?$", label)
        if match:
            published_at = parse_known_event_kst(match.group("date"))
            title = _safe_text(match.group("title"))
            item = _build_page_item(
                source_name=source_name,
                source_group=source_group,
                page_name=page_name,
                page_url=page_url,
                title=title,
                item_url=href,
                entity=configured_entity or "HanAll Biopharma",
                item_type="regulatory_notice",
                visible_lines=visible_lines,
                published_at=published_at,
                configured_entity=configured_entity,
            )
            item.document_id = _safe_text(match.group("no")) or item.document_id
            item.filing_type = _normalize_filing_type(title) or item.filing_type
            item.item_identity_key, item.source_specific_identity_json = build_page_item_identity(item)
            item.content_fingerprint = build_page_item_fingerprint(item)
            items.append(item)
            if len(items) >= max_items:
                break
            continue
        published_at, _ = _parse_datetime_from_lines(label, visible_lines)
        if published_at is None:
            published_at = parse_known_event_kst(label)
        if published_at is None:
            continue
        item = _build_page_item(
            source_name=source_name,
            source_group=source_group,
            page_name=page_name,
            page_url=page_url,
            title=_safe_text(label),
            item_url=href,
            entity=configured_entity or "HanAll Biopharma",
            item_type="regulatory_notice",
            visible_lines=visible_lines,
            published_at=published_at,
            configured_entity=configured_entity,
        )
        item.item_identity_key, item.source_specific_identity_json = build_page_item_identity(item)
        item.content_fingerprint = build_page_item_fingerprint(item)
        items.append(item)
        if len(items) >= max_items:
            break
    return items


def _parse_registry_items(
    *,
    source_name: str,
    source_group: str,
    page_name: str,
    page_url: str,
    configured_entity: str,
    html_text: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    max_items: int,
) -> list[OfficialPageItem]:
    visible_lines = _extract_visible_lines(html_text)
    items: list[OfficialPageItem] = []
    for label, href in _best_anchor_candidates(
        html_text=html_text,
        base_url=page_url,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        source_name=source_name,
        limit=max(max_items, 4),
    ):
        context_lines = _search_lines_for_title_context(label, visible_lines)
        trial_id = _extract_trial_id(label, href, *context_lines)
        if not trial_id:
            continue
        updated_at, _ = _parse_datetime_from_lines(label, context_lines)
        if updated_at is None:
            for line in context_lines:
                updated_at = parse_known_event_kst(line)
                if updated_at is not None:
                    break
        item = _build_page_item(
            source_name=source_name,
            source_group=source_group,
            page_name=page_name,
            page_url=page_url,
            title=_safe_text(label),
            item_url=href,
            entity=_infer_entity(
                source_name=source_name,
                source_group=source_group,
                configured_entity=configured_entity,
                title=label,
                url=href,
                visible_lines=context_lines,
            ),
            item_type="trial_registry_update",
            visible_lines=context_lines,
            updated_at=updated_at,
            configured_entity=configured_entity,
        )
        item.trial_id = trial_id
        item.item_identity_key, item.source_specific_identity_json = build_page_item_identity(item)
        item.content_fingerprint = build_page_item_fingerprint(item)
        items.append(item)
        if len(items) >= max_items:
            break
    return items


def _parse_news_items(
    *,
    source_name: str,
    source_group: str,
    page_name: str,
    page_url: str,
    configured_entity: str,
    html_text: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    max_items: int,
) -> list[OfficialPageItem]:
    visible_lines = _extract_visible_lines(html_text)
    items: list[OfficialPageItem] = []
    for label, href in _best_anchor_candidates(
        html_text=html_text,
        base_url=page_url,
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        source_name=source_name,
        limit=max(max_items, 4),
    ):
        title = _safe_text(label)
        published_at, _ = _parse_datetime_from_lines(title, visible_lines)
        if published_at is None:
            published_at = parse_known_event_kst(title)
        entity = _infer_entity(
            source_name=source_name,
            source_group=source_group,
            configured_entity=configured_entity,
            title=title,
            url=href,
            visible_lines=visible_lines,
        )
        item = _build_page_item(
            source_name=source_name,
            source_group=source_group,
            page_name=page_name,
            page_url=page_url,
            title=title,
            item_url=href,
            entity=entity,
            item_type=_infer_item_type(source_name=source_name, page_name=page_name, title=title, url=href),
            visible_lines=visible_lines,
            published_at=published_at,
            configured_entity=configured_entity,
        )
        if item.item_type == "page_update":
            item.item_type = "press_release" if source_name in _COMPANY_PAGE_SOURCES else item.item_type
        item.item_identity_key, item.source_specific_identity_json = build_page_item_identity(item)
        item.content_fingerprint = build_page_item_fingerprint(item)
        items.append(item)
        if len(items) >= max_items:
            break
    return items


def _sort_page_items(items: list[OfficialPageItem]) -> list[OfficialPageItem]:
    return sorted(
        items,
        key=lambda item: (
            item.scheduled_for or item.updated_at or item.published_at or datetime.min.replace(tzinfo=now_kst().tzinfo),
            item.item_title,
        ),
        reverse=True,
    )


def parse_official_page_items(
    *,
    source_config: dict[str, Any],
    html_text: str,
) -> tuple[list[OfficialPageItem], list[tuple[str, str]]]:
    source_name = _safe_text(source_config.get("name") or source_config.get("source_name")) or "page_check"
    source_group = _safe_text(source_config.get("source_group")) or "discovery_only"
    page_name = _source_page_name(source_config, source_name)
    page_url = _safe_text(source_config.get("url")) or "-"
    configured_entity = _safe_text(source_config.get("entity"))
    include_keywords = _matching_keywords(source_config, "include_keywords")
    exclude_keywords = _matching_keywords(source_config, "exclude_keywords")
    max_items = _max_items_per_page(source_config, default=1)
    access_text = html_text.lower()
    warnings: list[tuple[str, str]] = []

    if "robots" in access_text and "disallow" in access_text:
        warnings.append(("robots_blocked", "page contains robots/disallow notice"))
    if "sign in" in access_text or "log in" in access_text or "login" in access_text:
        warnings.append(("login_wall", "page appears to require login"))

    if source_name in _EVENT_PAGE_SOURCES:
        items = _parse_calendar_items(
            source_name=source_name,
            source_group=source_group,
            page_name=page_name,
            page_url=page_url,
            configured_entity=configured_entity,
            html_text=html_text,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            max_items=max_items,
        )
    elif source_name in {"krx", "kind"}:
        items = _parse_regulatory_items(
            source_name=source_name,
            source_group=source_group,
            page_name=page_name,
            page_url=page_url,
            configured_entity=configured_entity,
            html_text=html_text,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            max_items=max_items,
        )
    elif source_name in {"ctis", "jrct", "chictr", "who_ictrp"}:
        items = _parse_registry_items(
            source_name=source_name,
            source_group=source_group,
            page_name=page_name,
            page_url=page_url,
            configured_entity=configured_entity,
            html_text=html_text,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            max_items=max_items,
        )
    else:
        items = _parse_news_items(
            source_name=source_name,
            source_group=source_group,
            page_name=page_name,
            page_url=page_url,
            configured_entity=configured_entity,
            html_text=html_text,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            max_items=max_items,
        )

    if not items:
        warnings.append(("no_latest_item_found", "no stable latest item could be extracted from the page"))
        return [], warnings
    items = _sort_page_items(items)

    if source_name in _EVENT_PAGE_SOURCES and not items[0].scheduled_for_kst:
        warnings.append(("date_parse_failed", "event page item extracted but scheduled date/time could not be parsed"))
    if source_name in {"krx", "kind"} and not items[0].published_at_kst:
        warnings.append(("date_parse_failed", "regulatory page item extracted but publication date/time could not be parsed"))
    if source_name in {"ctis", "jrct", "chictr", "who_ictrp"} and not items[0].updated_at_kst:
        warnings.append(("date_parse_failed", "registry page item extracted but updated/post date could not be parsed"))
    return items, warnings


def promote_official_page_item_to_finding(
    *,
    page_item: OfficialPageItem,
    checked_at: datetime,
    discovery_only: bool,
) -> RawFinding | None:
    if page_item.login_wall or page_item.robots_blocked or page_item.access_restriction:
        return None
    if page_item.freshness_state not in {"new_item", "substantive_update"}:
        return None
    if page_item.item_type in {"investor_event", "earnings", "presentation"} and page_item.scheduled_for_kst:
        return None

    if discovery_only:
        confidence = 0.55
    elif page_item.item_type in {"career_posting", "analyst_coverage_update"}:
        confidence = 0.62
    else:
        confidence = 0.82

    title_prefix = page_item.page_name if page_item.page_name and page_item.page_name != page_item.source_name else page_item.entity
    source_note_bits = [
        f"page_name={page_item.page_name}",
        f"item_type={page_item.item_type}",
        f"freshness={page_item.freshness_state or '-'}",
        f"detection={page_item.detection_method or '-'}",
    ]
    if page_item.first_seen_at_kst:
        source_note_bits.append(f"first_seen={page_item.first_seen_at_kst}")
    if page_item.last_seen_at_kst:
        source_note_bits.append(f"last_seen={page_item.last_seen_at_kst}")
    title = page_item.item_title
    if page_item.item_type == "trial_registry_update" and page_item.trial_id:
        title = f"{title} ({page_item.trial_id})"
    summary = f"{title_prefix} latest page item promoted from official page parser"
    if page_item.updated_at_kst:
        summary = f"{summary} | updated_at={page_item.updated_at_kst}"
    elif page_item.published_at_kst:
        summary = f"{summary} | published_at={page_item.published_at_kst}"

    direct_text = " ".join(
        bit for bit in (page_item.entity, page_item.asset, page_item.item_title, page_item.related_company) if _safe_text(bit)
    ).lower()
    category = (
        "competitor_relevant"
        if page_item.source_group == "competitor_official"
        else "company_direct"
        if any(term in direct_text for term in DIRECT_TERMS) or page_item.source_group == "company_official"
        else "competitor_relevant"
    )

    return RawFinding(
        source_family=page_item.source_group,
        source_name=page_item.source_name,
        source_group=page_item.source_group,
        source_tier="page_check_promoted",
        entity=page_item.entity,
        entity_type="company",
        category=category,
        title=title,
        summary=summary,
        published_at=page_item.published_at or page_item.updated_at or checked_at,
        updated_at=page_item.updated_at,
        document_type=page_item.document_type,
        document_id=page_item.document_id,
        filing_type=page_item.filing_type,
        trial_id=page_item.trial_id,
        asset=page_item.asset,
        indication=page_item.indication,
        region=page_item.region,
        target_moa=page_item.target_moa,
        phase=page_item.phase,
        recruitment_status=page_item.recruitment_status,
        enrollment=page_item.enrollment,
        primary_completion_date=page_item.primary_completion_date,
        sponsor=page_item.sponsor,
        last_update_posted=page_item.last_update_posted or (page_item.updated_at_kst[:10] if page_item.updated_at_kst else None),
        site_countries=page_item.site_countries,
        changed_fields=page_item.changed_fields,
        regulator=page_item.regulator or _regulator_label(page_item.source_name),
        exchange=page_item.exchange,
        filed_at=page_item.filed_at,
        accepted_at=page_item.accepted_at,
        event_action=page_item.event_action or ("updated" if page_item.freshness_state == "substantive_update" else "published"),
        key_numbers=page_item.key_numbers,
        regulatory_phrase=page_item.regulatory_phrase,
        primary_source_url=page_item.item_url or page_item.page_url,
        source_note=" | ".join(source_note_bits),
        confidence=confidence,
        raw_payload={"page_item": page_item.model_dump(mode="json")},
    )
