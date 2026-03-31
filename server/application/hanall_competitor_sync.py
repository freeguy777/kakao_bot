from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from server.core.hanall_news_models import CompetitorMapEntry, OfficialPageItem, RawFinding
from server.infra.sqlite_store import load_competitor_universe_observations, record_competitor_universe_observation
from server.utils import now_kst

GENERIC_ENTITY_MARKERS = {
    "",
    "-",
    "hanall biopharma",
    "hanall/immunovant watch",
    "registry watch",
    "regional regulator",
    "eu clinical trials",
    "japan clinical trials",
    "china clinical trials",
    "who ictrp",
    "eu regulators",
    "japan regulators",
    "china regulators",
}
DIRECT_CLASS_ASSETS = {"batoclimab", "imvt-1402", "nipocalimab", "tanfanercept", "hbm9161"}
COMMERCIAL_BENCHMARK_ASSETS = {"efgartigimod", "rozanolixizumab", "teprotumumab"}
SOURCE_TYPE_PROVENANCE = {
    "pipeline_program": 0.96,
    "trial_registry": 0.92,
    "regional_regulator": 0.9,
    "investor_presentation": 0.84,
    "press_release": 0.82,
    "investor_event": 0.8,
    "official_careers": 0.58,
    "page_update": 0.62,
    "curated_seed": 0.35,
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_list(values: Any) -> list[str]:
    if isinstance(values, list):
        items = values
    else:
        items = [values]
    normalized: list[str] = []
    for item in items:
        text = _normalize_text(item)
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _entry_key(entry: CompetitorMapEntry) -> str:
    return "|".join(
        [
            _normalize_text(entry.competitor).lower(),
            _normalize_text(entry.asset).lower(),
            _normalize_text(entry.indication).lower(),
        ]
    )


def _derive_source_type_from_page_item(page_item: OfficialPageItem) -> str:
    source_name = _normalize_text(page_item.source_name).lower()
    if page_item.item_type == "pipeline_program" or "pipeline" in source_name or "program" in source_name:
        return "pipeline_program"
    if page_item.item_type == "trial_registry_update":
        return "trial_registry"
    if page_item.item_type == "regulatory_notice":
        return "regional_regulator"
    if page_item.item_type == "presentation":
        return "investor_presentation"
    if page_item.item_type == "press_release":
        return "press_release"
    if page_item.item_type == "career_posting":
        return "official_careers"
    if page_item.item_type in {"investor_event", "earnings"}:
        return "investor_event"
    return "page_update"


def _derive_source_type_from_finding(finding: RawFinding) -> str:
    if finding.source_group == "trial_registry":
        return "trial_registry"
    if finding.source_group == "regulator_disclosure":
        return "regional_regulator"
    if finding.document_type == "official_presentation":
        return "investor_presentation"
    if finding.document_type == "official_pr":
        return "press_release"
    if finding.document_type == "official_careers":
        return "official_careers"
    return "page_update"


def _candidate_competitor_name(*values: Any) -> str | None:
    for value in values:
        text = _normalize_text(value)
        if text and text.lower() not in GENERIC_ENTITY_MARKERS:
            return text
    return None


def _infer_stage_status(*values: Any) -> str | None:
    lowered = " ".join(_normalize_text(value).lower() for value in values if _normalize_text(value))
    if not lowered:
        return None
    if any(keyword in lowered for keyword in ("commercial", "marketed", "approved", "authorized", "authorised")):
        return "commercial / marketed benchmark"
    if any(keyword in lowered for keyword in ("phase 3", "pivotal", "late-stage")):
        return "late clinical-stage"
    if any(keyword in lowered for keyword in ("phase 2", "phase 1", "clinical", "recruiting", "active")):
        return "clinical-stage"
    if any(keyword in lowered for keyword in ("preclinical", "discovery")):
        return "preclinical"
    return None


def _infer_layer(*, asset: str | None, indication: str | None, stage_status: str | None, region: str | None, target_moa: str | None) -> str | None:
    normalized_asset = _normalize_text(asset).lower()
    normalized_stage = _normalize_text(stage_status).lower()
    normalized_region = _normalize_text(region).upper()
    normalized_moa = _normalize_text(target_moa).lower()
    if normalized_asset in COMMERCIAL_BENCHMARK_ASSETS or any(keyword in normalized_stage for keyword in ("commercial", "marketed", "approved")):
        return "Standard-of-care / commercial"
    if normalized_region in {"CN", "KR", "JP"} and normalized_asset in {"hbm9161", "tanfanercept"}:
        return "Regional"
    if normalized_asset in DIRECT_CLASS_ASSETS or "fcrn antagonist" in normalized_moa:
        return "Direct class"
    if _normalize_text(indication):
        return "Indication"
    return "Indication"


def _entry_fingerprint(entry: CompetitorMapEntry) -> str:
    source = "|".join(
        [
            _normalize_text(entry.competitor).lower(),
            _normalize_text(entry.asset).lower(),
            _normalize_text(entry.indication).lower(),
            _normalize_text(entry.stage_status).lower(),
            _normalize_text(entry.region).lower(),
            _normalize_text(entry.primary_source_url).lower(),
            _normalize_text(entry.source_label).lower(),
            _normalize_text(entry.source_type).lower(),
        ]
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _merge_entries(preferred: CompetitorMapEntry, fallback: CompetitorMapEntry) -> CompetitorMapEntry:
    preferred_payload = preferred.model_dump(mode="json")
    fallback_payload = fallback.model_dump(mode="json")
    for field_name, field_value in fallback_payload.items():
        if field_name in {"aliases", "evidence_titles"}:
            continue
        if not _normalize_text(preferred_payload.get(field_name)) and _normalize_text(field_value):
            preferred_payload[field_name] = field_value
    preferred_payload["aliases"] = _normalize_list(preferred.aliases) + [
        alias for alias in _normalize_list(fallback.aliases) if alias not in _normalize_list(preferred.aliases)
    ]
    preferred_payload["evidence_titles"] = _normalize_list(preferred.evidence_titles) + [
        title for title in _normalize_list(fallback.evidence_titles) if title not in _normalize_list(preferred.evidence_titles)
    ]
    return CompetitorMapEntry.model_validate(preferred_payload)


def _select_preferred_entry(current: CompetitorMapEntry, candidate: CompetitorMapEntry) -> CompetitorMapEntry:
    current_score = current.provenance_score if current.provenance_score is not None else SOURCE_TYPE_PROVENANCE.get(current.source_type or "", 0.35)
    candidate_score = candidate.provenance_score if candidate.provenance_score is not None else SOURCE_TYPE_PROVENANCE.get(candidate.source_type or "", 0.35)
    if candidate_score >= current_score:
        return _merge_entries(candidate, current)
    return _merge_entries(current, candidate)


def _candidate_from_page_item(page_item: OfficialPageItem, *, current_now: datetime) -> CompetitorMapEntry | None:
    competitor = _candidate_competitor_name(page_item.related_company, page_item.entity, page_item.sponsor)
    if not competitor:
        return None
    if not (_normalize_text(page_item.asset) and _normalize_text(page_item.indication)):
        return None
    source_type = _derive_source_type_from_page_item(page_item)
    stage_status = _normalize_text(page_item.stage_status) or _infer_stage_status(
        page_item.stage_status,
        page_item.event_action,
        page_item.item_title,
        page_item.regulatory_phrase,
    )
    aliases = _normalize_list([page_item.asset, *page_item.raw_snapshot.get("aliases", [])])
    entry = CompetitorMapEntry(
        competitor=competitor,
        asset=page_item.asset,
        aliases=aliases,
        target_moa=page_item.target_moa,
        indication=page_item.indication,
        stage_status=stage_status,
        region=page_item.region,
        primary_source_url=page_item.item_url or page_item.page_url,
        source_label=page_item.page_name or page_item.source_name,
        source_type=source_type,
        last_verified_at_kst=page_item.updated_at_kst or page_item.published_at_kst or current_now.strftime("%Y-%m-%d %H:%M KST"),
        provenance_score=SOURCE_TYPE_PROVENANCE.get(source_type, 0.6),
        layer=_infer_layer(
            asset=page_item.asset,
            indication=page_item.indication,
            stage_status=stage_status,
            region=page_item.region,
            target_moa=page_item.target_moa,
        ),
        evidence_titles=[page_item.item_title],
    )
    return entry


def _candidate_from_finding(finding: RawFinding, *, current_now: datetime) -> CompetitorMapEntry | None:
    competitor = _candidate_competitor_name(finding.entity, finding.sponsor)
    if not competitor:
        return None
    if not (_normalize_text(finding.asset) and _normalize_text(finding.indication)):
        return None
    source_type = _derive_source_type_from_finding(finding)
    stage_status = _infer_stage_status(
        finding.event_action,
        finding.phase,
        finding.summary,
        finding.title,
        " ".join(finding.key_numbers or []),
    )
    aliases = _normalize_list([finding.asset, *finding.aliases])
    return CompetitorMapEntry(
        competitor=competitor,
        asset=finding.asset,
        aliases=aliases,
        target_moa=finding.target_moa,
        indication=finding.indication,
        stage_status=stage_status,
        region=finding.region,
        primary_source_url=finding.primary_source_url,
        source_label=finding.source_name,
        source_type=source_type,
        last_verified_at_kst=finding.updated_at_kst or finding.published_at_kst or current_now.strftime("%Y-%m-%d %H:%M KST"),
        provenance_score=SOURCE_TYPE_PROVENANCE.get(source_type, 0.6),
        layer=_infer_layer(
            asset=finding.asset,
            indication=finding.indication,
            stage_status=stage_status,
            region=finding.region,
            target_moa=finding.target_moa,
        ),
        evidence_titles=[finding.title],
    )


def build_auto_competitor_candidates(
    *,
    findings: list[RawFinding],
    page_items: list[OfficialPageItem],
    current_now: datetime | None = None,
) -> list[CompetitorMapEntry]:
    observed_at = current_now or now_kst()
    merged: dict[str, CompetitorMapEntry] = {}
    for page_item in page_items:
        if page_item.source_group not in {"competitor_official", "trial_registry", "regulator_disclosure", "company_official"}:
            continue
        if page_item.login_wall or page_item.robots_blocked or page_item.access_restriction:
            continue
        candidate = _candidate_from_page_item(page_item, current_now=observed_at)
        if candidate is None:
            continue
        key = _entry_key(candidate)
        merged[key] = _select_preferred_entry(merged[key], candidate) if key in merged else candidate
    for finding in findings:
        if finding.source_group not in {"competitor_official", "trial_registry", "regulator_disclosure", "company_official"}:
            continue
        candidate = _candidate_from_finding(finding, current_now=observed_at)
        if candidate is None:
            continue
        key = _entry_key(candidate)
        merged[key] = _select_preferred_entry(merged[key], candidate) if key in merged else candidate
    return list(merged.values())


def persist_auto_competitor_candidates(entries: list[CompetitorMapEntry], *, observed_at_kst: str) -> None:
    for entry in entries:
        if not (_normalize_text(entry.competitor) and _normalize_text(entry.asset) and _normalize_text(entry.indication)):
            continue
        record_competitor_universe_observation(
            competitor=entry.competitor,
            asset=entry.asset or "-",
            indication=entry.indication or "-",
            stage_status=entry.stage_status,
            region=entry.region,
            primary_source_url=entry.primary_source_url or "-",
            source_label=entry.source_label,
            source_type=entry.source_type,
            aliases=entry.aliases,
            target_moa=entry.target_moa,
            layer=entry.layer,
            provenance_score=entry.provenance_score,
            content_fingerprint=_entry_fingerprint(entry),
            observed_at_kst=observed_at_kst,
        )


def load_persisted_auto_candidates() -> list[CompetitorMapEntry]:
    entries: list[CompetitorMapEntry] = []
    for row in load_competitor_universe_observations():
        entries.append(CompetitorMapEntry.model_validate(row))
    return entries


def merge_competitor_entries(
    *,
    curated_entries: list[CompetitorMapEntry],
    auto_entries: list[CompetitorMapEntry],
) -> list[CompetitorMapEntry]:
    merged: dict[str, CompetitorMapEntry] = {}
    for entry in curated_entries:
        payload = entry.model_dump(mode="json")
        payload["source_type"] = entry.source_type or "curated_seed"
        payload["provenance_score"] = entry.provenance_score if entry.provenance_score is not None else SOURCE_TYPE_PROVENANCE["curated_seed"]
        normalized = CompetitorMapEntry.model_validate(payload)
        merged[_entry_key(normalized)] = normalized
    for entry in auto_entries:
        key = _entry_key(entry)
        merged[key] = _select_preferred_entry(merged[key], entry) if key in merged else entry
    return sorted(
        merged.values(),
        key=lambda item: (
            _normalize_text(item.layer),
            _normalize_text(item.competitor),
            _normalize_text(item.asset),
            _normalize_text(item.indication),
        ),
    )
