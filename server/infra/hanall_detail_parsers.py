from __future__ import annotations

import re
from typing import Any

from server.application.hanall_competitor_universe import normalize_monitor_indication, normalize_monitor_region
from server.application.hanall_page_items import parse_known_event_kst
from server.application.hanall_research import _extract_visible_lines
from server.core.hanall_news_models import OfficialPageItem

_PHASE_PATTERN = re.compile(r"\b(phase\s*[1234](?:/[1234])?[ab]?)\b", re.IGNORECASE)
_TRIAL_ID_PATTERNS = (
    re.compile(r"\b(NCT\d{8})\b", re.IGNORECASE),
    re.compile(r"\b(jRCT\d{10,})\b", re.IGNORECASE),
    re.compile(r"\b(ChiCTR\d{6,})\b", re.IGNORECASE),
    re.compile(r"\b(CTIS[-\s]?\d{4}[-\d]+)\b", re.IGNORECASE),
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
_DETAIL_FOLLOWUP_SOURCES = {
    "krx",
    "kind",
    "ctis",
    "jrct",
    "chictr",
    "who_ictrp",
    "ema",
    "pmda_mhlw",
    "nmpa",
    "argenx_official",
    "ucb_official",
    "jnj_official",
    "amgen_official",
    "roivant_official",
    "roivant_investors",
    "harbour_biomed_pipeline",
    "harbour_biomed_news",
    "daewoong_official",
    "immunovant_press_releases",
    "immunovant_presentations",
    "hanall_newsroom",
}
_TARGET_MOA_BY_ASSET = {
    "batoclimab": "FcRn antagonist",
    "HBM9161": "FcRn antagonist",
    "IMVT-1402": "FcRn antagonist",
    "tanfanercept": "TNFR1 fusion protein / anti-inflammatory biologic",
    "efgartigimod": "FcRn antagonist",
    "rozanolixizumab": "FcRn antagonist",
    "nipocalimab": "FcRn antagonist",
    "teprotumumab": "IGF-1R inhibitor",
}


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", _safe_text(value)).strip()


def _extract_first_matching_line(lines: list[str], *patterns: str) -> str | None:
    for line in lines:
        lowered = line.lower()
        if any(pattern in lowered for pattern in patterns):
            return line
    return None


def _extract_labeled_value(lines: list[str], *labels: str) -> str | None:
    for line in lines:
        normalized_line = _normalize_space(line)
        lowered = normalized_line.lower()
        for label in labels:
            lowered_label = label.lower()
            if lowered.startswith(f"{lowered_label}:"):
                return _safe_text(normalized_line.split(":", 1)[1])
            if lowered.startswith(f"{lowered_label} -"):
                return _safe_text(normalized_line.split("-", 1)[1])
    return None


def _extract_datetime_label(lines: list[str], *labels: str) -> str | None:
    value = _extract_labeled_value(lines, *labels)
    if not value:
        return None
    parsed = parse_known_event_kst(value)
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M KST")


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
        match = _PHASE_PATTERN.search(text)
        if match:
            return match.group(1)
    return None


def _extract_asset(*values: Any) -> str | None:
    lowered = " ".join(_safe_text(value).lower() for value in values if _safe_text(value))
    if not lowered:
        return None
    for asset, aliases in _ASSET_RULES:
        if any(alias in lowered for alias in aliases):
            return asset
    return None


def _extract_declared_asset(lines: list[str]) -> str | None:
    labeled = _extract_labeled_value(lines, "asset", "drug", "program", "product")
    if not labeled:
        return None
    return _extract_asset(labeled)


def _extract_target_moa(asset: str | None, *values: Any) -> str | None:
    normalized_asset = _safe_text(asset)
    if normalized_asset and normalized_asset in _TARGET_MOA_BY_ASSET:
        return _TARGET_MOA_BY_ASSET[normalized_asset]
    lowered = " ".join(_safe_text(value).lower() for value in values if _safe_text(value))
    if "fcrn" in lowered:
        return "FcRn antagonist"
    if "igf-1r" in lowered:
        return "IGF-1R inhibitor"
    if "tnfr" in lowered:
        return "TNFR1 fusion protein / anti-inflammatory biologic"
    return None


def _extract_site_countries(lines: list[str]) -> list[str]:
    value = _extract_labeled_value(lines, "site countries", "countries", "sites")
    if not value:
        return []
    return [country for country in (_safe_text(item) for item in re.split(r"[,;/]", value)) if country]


def _extract_key_numbers(lines: list[str]) -> list[str]:
    labeled = _extract_labeled_value(lines, "key numbers", "key metrics")
    if labeled:
        return [item for item in (_safe_text(token) for token in re.split(r"[,;/]", labeled)) if item]
    extracted: list[str] = []
    for line in lines:
        for match in re.findall(r"(?:\$[\d,.]+(?:\s*(?:million|billion))?|[\d,.]+\s*(?:shares|patients|subjects|sites|countries))", line, re.IGNORECASE):
            normalized = _safe_text(match)
            if normalized and normalized not in extracted:
                extracted.append(normalized)
    return extracted


def _infer_event_action(title: str, lines: list[str], *, item_type: str, source_name: str) -> str | None:
    explicit = _extract_labeled_value(lines, "event action", "action", "status")
    if explicit:
        return explicit
    lowered = f"{title} {' '.join(lines[:10])}".lower()
    if item_type == "trial_registry_update":
        if "updated" in lowered or "update posted" in lowered or "last update posted" in lowered:
            return "updated"
        return "posted"
    if any(keyword in lowered for keyword in ("accepted", "receipt accepted")):
        return "accepted"
    if any(keyword in lowered for keyword in ("approval", "approved", "authorised", "authorized", "designation granted")):
        return "approved"
    if "updated" in lowered or "amended" in lowered:
        return "updated"
    if item_type == "career_posting" or any(keyword in lowered for keyword in ("career", "hiring", "recruit")):
        return "posted"
    if item_type == "presentation" or "presentation" in lowered:
        return "presented"
    if source_name in {"krx", "kind", "ema", "pmda_mhlw", "nmpa"}:
        return "published"
    return None


def _parse_regulatory_detail(page_item: OfficialPageItem, lines: list[str]) -> dict[str, Any]:
    published_at = _extract_datetime_label(lines, "published at", "publication datetime", "public disclosure datetime")
    accepted_at = _extract_datetime_label(lines, "accepted at", "receipt accepted at", "accepted datetime")
    filed_at = _extract_datetime_label(lines, "filed at", "received at", "filed datetime")
    regulatory_phrase = _extract_labeled_value(lines, "regulatory phrase", "exact phrase", "decision")
    asset = _extract_declared_asset(lines) or _extract_asset(page_item.item_title, *lines, page_item.asset)
    indication = normalize_monitor_indication(" ".join([page_item.item_title, *lines]))
    return {
        "document_id": _extract_labeled_value(lines, "notice id", "document id", "report id") or page_item.document_id,
        "filing_type": _extract_labeled_value(lines, "filing type", "report type") or page_item.filing_type,
        "document_type": "regulatory_notice",
        "regulator": page_item.regulator or _safe_text(page_item.source_name).upper(),
        "exchange": _extract_labeled_value(lines, "exchange") or ("KRX" if page_item.source_name in {"krx", "kind"} else page_item.exchange),
        "published_at_kst": published_at,
        "accepted_at": accepted_at,
        "filed_at": filed_at,
        "event_action": _infer_event_action(page_item.item_title, lines, item_type=page_item.item_type, source_name=page_item.source_name),
        "key_numbers": _extract_key_numbers(lines),
        "asset": asset or page_item.asset,
        "indication": indication or page_item.indication,
        "regulatory_phrase": regulatory_phrase,
    }


def _parse_registry_detail(page_item: OfficialPageItem, lines: list[str]) -> dict[str, Any]:
    updated_at = _extract_datetime_label(lines, "updated", "last updated", "posted at", "update posted")
    last_update_posted = _extract_labeled_value(lines, "last update posted", "update posted")
    if not last_update_posted and updated_at:
        last_update_posted = updated_at[:10]
    sponsor = _extract_labeled_value(lines, "sponsor", "company", "organization", "기관명")
    asset = _extract_declared_asset(lines) or _extract_asset(page_item.item_title, *lines, page_item.asset)
    return {
        "trial_id": _extract_labeled_value(lines, "trial id", "registry id") or _extract_trial_id(page_item.trial_id, page_item.item_title, *lines),
        "sponsor": sponsor or page_item.sponsor,
        "asset": asset or page_item.asset,
        "target_moa": _extract_target_moa(asset or page_item.asset, page_item.item_title, *lines) or page_item.target_moa,
        "indication": normalize_monitor_indication(" ".join([page_item.item_title, *lines])) or page_item.indication,
        "phase": _extract_labeled_value(lines, "phase") or _extract_phase(page_item.phase, page_item.item_title, *lines),
        "recruitment_status": _extract_labeled_value(lines, "recruitment status", "status", "enrollment status") or page_item.recruitment_status,
        "enrollment": _extract_labeled_value(lines, "enrollment", "planned enrollment", "target enrollment"),
        "primary_completion_date": _extract_labeled_value(lines, "primary completion date", "primary completion"),
        "last_update_posted": last_update_posted or page_item.last_update_posted,
        "site_countries": _extract_site_countries(lines) or page_item.site_countries,
        "updated_at_kst": updated_at or page_item.updated_at_kst,
        "event_action": _infer_event_action(page_item.item_title, lines, item_type=page_item.item_type, source_name=page_item.source_name),
    }


def _parse_competitor_detail(page_item: OfficialPageItem, lines: list[str]) -> dict[str, Any]:
    asset = _extract_declared_asset(lines) or _extract_asset(page_item.item_title, *lines, page_item.asset)
    indication = normalize_monitor_indication(" ".join([page_item.item_title, *lines])) or page_item.indication
    published_at = _extract_datetime_label(lines, "published at", "release date", "posted at", "date")
    updated_at = _extract_datetime_label(lines, "updated at", "last updated")
    stage_status = _extract_labeled_value(lines, "stage status", "stage", "development stage", "program stage")
    document_type = {
        "press_release": "official_pr",
        "presentation": "official_presentation",
        "career_posting": "official_careers",
        "investor_event": "official_ir",
        "pipeline_program": "official_pipeline",
    }.get(page_item.item_type, "official_page")
    return {
        "document_type": document_type,
        "asset": asset or page_item.asset,
        "target_moa": _extract_target_moa(asset or page_item.asset, page_item.item_title, *lines) or page_item.target_moa,
        "indication": indication,
        "region": normalize_monitor_region(" ".join(lines[:10])) or page_item.region,
        "stage_status": stage_status or page_item.stage_status,
        "published_at_kst": published_at or page_item.published_at_kst,
        "updated_at_kst": updated_at or page_item.updated_at_kst,
        "event_action": _infer_event_action(page_item.item_title, lines, item_type=page_item.item_type, source_name=page_item.source_name),
    }


def parse_detail_page(
    *,
    page_item: OfficialPageItem,
    html_text: str,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    lines = _extract_visible_lines(html_text)
    warnings: list[tuple[str, str]] = []
    if not lines:
        return {}, [("detail_parse_failed", "detail page had no visible lines")]

    normalized_lines = [_normalize_space(line) for line in lines if _normalize_space(line)]
    detail_fields: dict[str, Any] = {}

    if page_item.source_name in {"krx", "kind", "ema", "pmda_mhlw", "nmpa"}:
        detail_fields.update(_parse_regulatory_detail(page_item, normalized_lines))
    if page_item.source_name in {"ctis", "jrct", "chictr", "who_ictrp"}:
        detail_fields.update(_parse_registry_detail(page_item, normalized_lines))
    if page_item.source_group == "competitor_official" or page_item.source_name in {"ema", "pmda_mhlw", "nmpa"}:
        detail_fields.update(_parse_competitor_detail(page_item, normalized_lines))

    if page_item.source_group == "company_official" and page_item.source_name not in {"krx", "kind"}:
        detail_fields.setdefault(
            "published_at_kst",
            _extract_datetime_label(normalized_lines, "published at", "release date", "posted at", "date") or page_item.published_at_kst,
        )
        detail_fields.setdefault(
            "updated_at_kst",
            _extract_datetime_label(normalized_lines, "updated at", "last updated") or page_item.updated_at_kst,
        )
        asset = _extract_declared_asset(normalized_lines) or _extract_asset(page_item.item_title, *normalized_lines, page_item.asset)
        detail_fields.setdefault("asset", asset or page_item.asset)
        detail_fields.setdefault(
            "indication",
            normalize_monitor_indication(" ".join([page_item.item_title, *normalized_lines])) or page_item.indication,
        )

    if not any(_safe_text(value) for value in detail_fields.values()):
        warnings.append(("detail_parse_failed", "detail page follow-up did not yield structured fields"))
    return detail_fields, warnings


def should_follow_detail(page_item: OfficialPageItem, source_config: dict[str, Any]) -> bool:
    if bool(source_config.get("detail_followup", False)):
        return bool(page_item.item_url) and page_item.item_url != page_item.page_url
    return bool(page_item.item_url) and page_item.item_url != page_item.page_url and page_item.source_name in _DETAIL_FOLLOWUP_SOURCES
