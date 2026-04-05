from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from time import perf_counter
from typing import Any
import random
from urllib.parse import urlparse

from pydantic import ValidationError
import requests

from server.application.hanall_competitor_universe import (
    MONITORED_INDICATIONS,
    MONITORED_REGIONS,
    MONITORED_SOURCE_GROUPS,
    build_competitor_universe_snapshot,
    normalize_monitor_indication,
    normalize_monitor_region,
)
from server.application.hanall_page_items import parse_known_event_kst
from server.application.hanall_reporting import (
    build_ranked_issue_list,
    build_search_plan_from_memory,
    build_stage2_search_memory,
)
from server.application.hanall_research import (
    HANALL_FINAL_SECTION_HEADINGS,
    HANALL_DIRECT_COMPANIES,
    build_hanall_base_prompt_replacements,
    build_hanall_known_events_context,
    get_hanall_known_events,
)
from server.application.prompting import run_prompt_by_key_raw
from server.config import get_hanall_sources_config
from server.core.hanall_news_models import (
    CheckedSourceLogEntry,
    CompetitorMapEntry,
    CoverageGap,
    CoverageOverlay,
    CoverageSummary,
    FieldProvenance,
    HanallStage1OverlayOutput,
    HanallStage1StructuredOutput,
    OmissionAuditEntry,
    OfficialCollectionResult,
    RSSCollectionResult,
    RawFinding,
    SearchEvidence,
    SearchGapTarget,
    SearchTask,
    StageFinding,
    Stage2Backfill,
    Stage2DiscoveredFinding,
    Stage2VerificationOutput,
)
from server.infra.hanall_news_collectors import collect_hanall_official_findings
from server.infra.hanall_rss import fetch_hanall_rss_results
from server.infra.sqlite_store import (
    finalize_stage2_verification_run,
    persist_hanall_run_snapshot,
    persist_stage2_verification_success,
    record_stage2_verification_run_start,
)
from server.utils import make_trace_id, now_kst, smart_truncate

logger = logging.getLogger(__name__)
REQUIRED_SECTION_HEADINGS = list(HANALL_FINAL_SECTION_HEADINGS)


@dataclass(frozen=True)
class HanallNewsPipelineResult:
    final_text: str
    raw_output_text: str
    stage1_output: HanallStage1StructuredOutput
    official_collection: OfficialCollectionResult
    rss_collection: RSSCollectionResult
    stage2_verification_output: Stage2VerificationOutput | None = None
    stage2_trace_id: str | None = None
    used_stage1_fallback: bool = False
    used_stage2_fallback: bool = False
    stage1_mode: str = "llm_overlay_merged"
    stage1_attempts: int = 0
    stage1_invalid_ref_count: int = 0
    stage1_candidate_count: int = 0
    render_mode: str = "stage2_llm"
    stage2_attempts: int = 0
    stage2_skipped_reason: str | None = None
    stage1_invalid_item_count: int = 0


@dataclass(frozen=True)
class Stage1DeterministicBase:
    output: HanallStage1StructuredOutput
    candidate_map: dict[str, StageFinding]
    candidate_order: list[str]
    candidate_bucket_map: dict[str, str]


STAGE1_ITEM_ALIAS_MAPS: dict[str, dict[str, tuple[str, ...]]] = {
    "today_scheduled_events": {
        "category": ("bucket", "event_type", "kind"),
        "title": ("fact", "headline", "event_title", "finding"),
    },
    "company_direct_confirmed": {
        "category": ("bucket", "event_type"),
        "title": ("fact", "headline", "finding"),
    },
    "competitor_relevant_confirmed": {
        "category": ("bucket", "event_type"),
        "title": ("fact", "headline", "finding"),
    },
    "unverified_leads": {
        "category": ("bucket", "event_type"),
        "title": ("fact", "headline", "finding"),
    },
    "checked_source_log": {
        "source_family": ("family", "source_group"),
    },
    "coverage_gaps": {
        "source_family": ("family", "source_group"),
        "detail": ("reason", "note", "message"),
    },
    "omission_audit": {
        "topic": ("check_point", "checkpoint"),
        "detail": ("reason", "note", "message", "observation"),
    },
    "search_tasks": {
        "topic": ("title", "query_topic"),
    },
}
SOURCE_FAMILY_FALLBACKS = {
    "sec_api": "sec",
    "sec_official": "sec",
    "fmp": "fmp",
    "opendart": "opendart",
    "openfda": "openfda",
    "clinicaltrials": "clinicaltrials",
    "cris": "cris",
    "mfds": "mfds",
    "ncbi": "ncbi",
    "europe_pmc": "europe_pmc",
    "crossref": "crossref",
    "biorxiv": "biorxiv",
    "local_known_events": "local_schedule",
}
SOURCE_GROUP_FALLBACKS = {
    "sec": "regulator_disclosure",
    "fmp": "regulator_disclosure",
    "opendart": "regulator_disclosure",
    "openfda": "regulator_disclosure",
    "mfds": "regulator_disclosure",
    "clinicaltrials": "trial_registry",
    "cris": "trial_registry",
    "ncbi": "discovery_only",
    "europe_pmc": "discovery_only",
    "crossref": "discovery_only",
    "biorxiv": "discovery_only",
    "local_schedule": "company_official",
    "company_official": "company_official",
    "competitor_official": "competitor_official",
    "regulator_disclosure": "regulator_disclosure",
    "trial_registry": "trial_registry",
    "discovery_only": "discovery_only",
}
STAGE2_RATE_LIMIT_TOTAL_ATTEMPTS = 2
STAGE2_RATE_LIMIT_COOLDOWN_SECONDS = 90.0
STAGE2_RETRY_BACKOFF_SECONDS = 0.35
STAGE1_TOTAL_ATTEMPTS = 2
STAGE1_RETRY_BACKOFF_SECONDS = 0.25
_stage2_rate_limit_state: dict[str, Any] = {
    "input_hash": None,
    "cooldown_until_monotonic": 0.0,
}


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _coerce_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _top_counter_summary(counter: Counter[str], *, limit: int = 5) -> str:
    return ",".join(f"{key}={count}" for key, count in counter.most_common(limit)) or "-"


def _summarize_source_log_statuses(entries: list[CheckedSourceLogEntry]) -> str:
    return _top_counter_summary(Counter(entry.status for entry in entries if entry.status))


def _summarize_gap_types(entries: list[CoverageGap]) -> str:
    return _top_counter_summary(Counter(entry.gap_type for entry in entries if entry.gap_type))


def _classify_stage2_fallback_reason(exc: Exception) -> str:
    message = str(exc)
    if "invalid or incomplete sectioned text" in message:
        return "invalid_final_text"
    if "structured JSON payload not found" in message or isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, ValidationError):
        return "validation_error"
    return exc.__class__.__name__


def _clean_json_text(raw_text: str) -> str:
    normalized = str(raw_text or "").strip()
    normalized = re.sub(r"^```(?:json|JSON|text)?\s*", "", normalized, count=1)
    normalized = re.sub(r"\s*```$", "", normalized, count=1)
    normalized = normalized.strip()
    if normalized.startswith("{") and normalized.endswith("}"):
        return normalized
    start = normalized.find("{")
    end = normalized.rfind("}")
    if start >= 0 and end > start:
        return normalized[start : end + 1]
    raise ValueError("structured JSON payload not found")


def _is_direct_company(finding: RawFinding) -> bool:
    normalized = finding.entity.lower()
    return any(company.lower() in normalized for company in HANALL_DIRECT_COMPANIES)


def _to_stage_finding(finding: RawFinding) -> StageFinding:
    return StageFinding(
        entity=finding.entity,
        entity_type=finding.entity_type,
        category=finding.category,
        title=finding.title,
        summary=finding.summary,
        published_at_kst=finding.published_at_kst,
        updated_at_kst=finding.updated_at_kst,
        source_family=finding.source_family,
        source_name=finding.source_name,
        source_group=finding.source_group,
        source_tier=finding.source_tier,
        primary_source_url=finding.primary_source_url,
        secondary_source_url=finding.secondary_source_url,
        document_type=finding.document_type,
        document_id=finding.document_id,
        filing_type=finding.filing_type,
        trial_id=finding.trial_id,
        asset=finding.asset,
        aliases=finding.aliases,
        sponsor=finding.sponsor,
        target_moa=finding.target_moa,
        indication=finding.indication,
        region=finding.region,
        stage_status=finding.stage_status,
        phase=finding.phase,
        recruitment_status=finding.recruitment_status,
        enrollment=finding.enrollment,
        primary_completion_date=finding.primary_completion_date,
        last_update_posted=finding.last_update_posted,
        site_countries=finding.site_countries,
        changed_fields=finding.changed_fields,
        regulator=finding.regulator,
        exchange=finding.exchange,
        filed_at=finding.filed_at,
        accepted_at=finding.accepted_at,
        event_action=finding.event_action,
        key_numbers=finding.key_numbers,
        regulatory_phrase=finding.regulatory_phrase,
        insider_person=finding.insider_person,
        insider_role=finding.insider_role,
        insider_quantity=finding.insider_quantity,
        insider_price=finding.insider_price,
        trade_date=finding.trade_date,
        source_note=finding.source_note,
        confidence=finding.confidence,
    )


def _candidate_identity_parts(item: StageFinding) -> list[str]:
    return [
        item.source_name or "-",
        item.document_id or "-",
        item.trial_id or "-",
        item.primary_source_url or "-",
        item.title,
        item.published_at_kst or "-",
        item.entity,
    ]


def _build_candidate_id(item: StageFinding) -> str:
    raw = "|".join(_candidate_identity_parts(item))
    return f"cand_{sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _with_candidate_id(item: StageFinding) -> StageFinding:
    if item.candidate_id:
        return item
    cloned = StageFinding.model_validate(item.model_dump(mode="json"))
    cloned.candidate_id = _build_candidate_id(cloned)
    return cloned


def _sort_stage_findings(items: list[StageFinding]) -> list[StageFinding]:
    return sorted(
        items,
        key=lambda item: (
            item.published_at_kst or "",
            item.source_name or "",
            item.title,
            item.entity,
        ),
        reverse=True,
    )


def _dedupe_stage_findings(items: list[StageFinding]) -> list[StageFinding]:
    deduped: list[StageFinding] = []
    seen: set[str] = set()
    for item in items:
        normalized = _with_candidate_id(item)
        if normalized.candidate_id in seen:
            continue
        seen.add(normalized.candidate_id or "-")
        deduped.append(normalized)
    return _sort_stage_findings(deduped)


def _build_known_event_findings(known_events: list[dict[str, Any]], current_now: datetime) -> list[StageFinding]:
    findings: list[StageFinding] = []
    for event in known_events:
        freshness = str(event.get("aging_status") or event.get("freshness_status") or "").strip()
        if freshness != "due_today":
            continue
        findings.append(
            StageFinding(
                entity=event["entity"],
                entity_type="company",
                category=event["category"],
                title=event["fact"],
                summary=f"basis={event['basis']} | source={event['primary_source']} | status={event['status_note']}",
                published_at_kst=event["scheduled_for_kst"],
                primary_source_url=event["primary_source"],
                source_name="local_known_events",
                source_family="local_schedule",
                source_tier="local_context",
                source_note=event["status_note"],
            )
        )
    return findings


def _build_known_event_stale_audit_entries(known_events: list[dict[str, Any]]) -> list[OmissionAuditEntry]:
    entries: list[OmissionAuditEntry] = []
    for event in known_events:
        freshness = str(event.get("aging_status") or event.get("freshness_status") or "").strip()
        if freshness != "past_due_without_followup" and freshness != "stale":
            continue
        entries.append(
            OmissionAuditEntry(
                topic=f"known_event:{event.get('entity', '-')}",
                axis="known_event",
                status="open",
                detail=(
                    f"scheduled_for_kst={event.get('scheduled_for_kst', '-')} "
                    f"status_note={event.get('status_note', '-')} "
                    f"basis={event.get('basis', '-')}"
                ),
            )
        )
    return entries


def _resolve_source_group(*, source_group: str | None, source_family: str | None, source_name: str | None) -> str:
    normalized_group = str(source_group or "").strip()
    if normalized_group:
        return normalized_group
    normalized_family = str(source_family or "").strip()
    normalized_name = str(source_name or "").strip()
    return (
        SOURCE_GROUP_FALLBACKS.get(normalized_family)
        or SOURCE_GROUP_FALLBACKS.get(normalized_name)
        or "discovery_only"
    )


def _build_competitor_snapshot(
    *,
    findings: list[RawFinding],
    checked_source_log: list[CheckedSourceLogEntry],
    page_items: list[Any] | None = None,
    current_now: datetime | None = None,
) -> list[CompetitorMapEntry]:
    return build_competitor_universe_snapshot(
        findings=findings,
        checked_source_log=checked_source_log,
        page_items=page_items,
        current_now=current_now,
    )


def _finding_indication_axis(finding: RawFinding) -> str | None:
    return normalize_monitor_indication(" ".join(bit for bit in (finding.indication, finding.title, finding.summary) if bit))


def _finding_region_axis(finding: RawFinding) -> str | None:
    values = [
        finding.region,
        finding.regulator,
        finding.exchange,
        finding.primary_source_url,
        finding.source_name,
        finding.source_family,
        " ".join(finding.site_countries or []),
    ]
    return normalize_monitor_region(" ".join(bit for bit in values if bit))


def _build_omission_audit(
    *,
    findings: list[RawFinding],
    checked_source_log: list[CheckedSourceLogEntry],
    coverage_gaps: list[CoverageGap],
    competitor_snapshot: list[CompetitorMapEntry],
    page_items: list[Any] | None,
    known_events: list[dict[str, Any]],
    current_now: datetime,
) -> list[OmissionAuditEntry]:
    entries: list[OmissionAuditEntry] = []

    for source_group in MONITORED_SOURCE_GROUPS:
        group_logs = [
            entry
            for entry in checked_source_log
            if _resolve_source_group(
                source_group=entry.source_group,
                source_family=entry.source_family,
                source_name=entry.source_name,
            )
            == source_group
        ]
        group_gaps = [
            gap
            for gap in coverage_gaps
            if _resolve_source_group(
                source_group=gap.source_group,
                source_family=gap.source_family,
                source_name=gap.source_name,
            )
            == source_group
        ]
        status = "completed" if group_logs and not group_gaps else "open" if group_gaps else "monitoring"
        detail_bits = [
            f"checked_sources={len(group_logs)}",
            f"coverage_gaps={len(group_gaps)}",
        ]
        latest_titles = [entry.latest_item_title for entry in group_logs if entry.latest_item_title][:2]
        if latest_titles:
            detail_bits.append(f"latest_examples={', '.join(latest_titles)}")
        entries.append(
            OmissionAuditEntry(
                topic=f"source-group:{source_group}",
                axis="source_group",
                source_group=source_group,
                status=status,
                detail=" | ".join(detail_bits),
            )
        )

    for indication in MONITORED_INDICATIONS:
        related_findings = [finding for finding in findings if _finding_indication_axis(finding) == indication]
        related_snapshot = [
            entry
            for entry in competitor_snapshot
            if normalize_monitor_indication(entry.indication) == indication
        ]
        status = "completed" if related_findings else "monitoring" if related_snapshot else "open"
        entries.append(
            OmissionAuditEntry(
                topic=f"indication:{indication}",
                axis="indication",
                indication=indication,
                status=status,
                detail=(
                    f"official_findings={len(related_findings)} | "
                    f"universe_entries={len(related_snapshot)}"
                ),
            )
        )

    for region in MONITORED_REGIONS:
        related_findings = [finding for finding in findings if _finding_region_axis(finding) == region]
        related_snapshot = [entry for entry in competitor_snapshot if normalize_monitor_region(entry.region) == region]
        status = "completed" if related_findings else "monitoring" if related_snapshot else "open"
        entries.append(
            OmissionAuditEntry(
                topic=f"region:{region}",
                axis="region",
                region=region,
                status=status,
                detail=(
                    f"official_findings={len(related_findings)} | "
                    f"universe_entries={len(related_snapshot)}"
                ),
            )
        )

    auto_synced_entries = [entry for entry in competitor_snapshot if (entry.source_type or "") not in {"", "curated_seed"}]
    multi_item_sources = Counter(_resolve_source_group(source_group=getattr(item, "source_group", None), source_family=getattr(item, "source_group", None), source_name=getattr(item, "source_name", None)) for item in list(page_items or []))
    parser_coverage = sum(1 for item in list(page_items or []) if getattr(item, "item_identity_key", None))
    entries.append(
        OmissionAuditEntry(
            topic="competitor_universe_auto_sync",
            axis="source_group",
            source_group="competitor_official",
            status="completed" if auto_synced_entries else "monitoring",
            detail=(
                f"auto_synced_entries={len(auto_synced_entries)} | "
                f"persisted_snapshot_entries={len(competitor_snapshot)}"
            ),
        )
    )
    entries.append(
        OmissionAuditEntry(
            topic="multi_item_parse_coverage",
            axis="source_group",
            source_group="company_official",
            status="completed" if any(count > 1 for count in multi_item_sources.values()) else "monitoring",
            detail=" | ".join(
                [
                    f"page_items={len(list(page_items or []))}",
                    f"sources_with_multi_items={sum(1 for count in multi_item_sources.values() if count > 1)}",
                ]
            ),
        )
    )
    entries.append(
        OmissionAuditEntry(
            topic="source_specific_parser_coverage",
            axis="source_group",
            source_group="competitor_official",
            status="completed" if parser_coverage else "open",
            detail=f"identity_backed_page_items={parser_coverage}",
        )
    )

    entries.extend(_build_known_event_stale_audit_entries(known_events))
    return entries


def _build_coverage_summary(
    *,
    findings: list[RawFinding],
    checked_source_log: list[CheckedSourceLogEntry],
    coverage_gaps: list[CoverageGap],
) -> CoverageSummary:
    gap_count = len(coverage_gaps)
    finding_count = len(findings)
    if finding_count >= 5 and gap_count <= 2:
        level = "High"
        rationale = "official APIs returned multiple candidate findings with limited collection gaps"
    elif finding_count >= 2:
        level = "Medium"
        rationale = "official APIs returned some findings but coverage gaps remain"
    else:
        level = "Low"
        rationale = "official API findings are sparse or collectors reported multiple gaps"
    return CoverageSummary(
        level=level,
        rationale=rationale,
        official_findings_count=finding_count,
        source_log_count=len(checked_source_log),
        coverage_gap_count=gap_count,
    )


def _build_search_tasks(findings: list[RawFinding], coverage_gaps: list[CoverageGap], competitor_snapshot: list[CompetitorMapEntry]) -> list[SearchTask]:
    tasks: list[SearchTask] = []
    for finding in findings[:5]:
        if finding.confidence >= 0.75 and finding.primary_source_url:
            continue
        recommended_queries = [
            query
            for query in (
                " ".join(part for part in (finding.entity, finding.asset, finding.indication, finding.region) if part),
                " ".join(part for part in (finding.asset, finding.indication, finding.regulator or finding.exchange) if part),
                finding.title,
            )
            if query.strip()
        ]
        tasks.append(
            SearchTask(
                topic=finding.title,
                reason="needs official confirmation or stronger sourcing in stage2",
                priority="high" if finding.category == "company_direct" else "medium",
                recommended_queries=recommended_queries,
                preferred_source_types=["official_site", "regulator", "registry", "trusted_rss", "newswire"],
                entity=finding.entity,
                asset=finding.asset,
                indication=finding.indication,
            )
        )
    for gap in coverage_gaps[:5]:
        tasks.append(
            SearchTask(
                topic=f"{gap.source_name} coverage gap",
                reason=gap.detail,
                priority="medium",
                recommended_queries=[gap.source_name, gap.endpoint or gap.detail],
                preferred_source_types=["official_site", "regulator", "registry"],
                entity=gap.source_name,
            )
        )
    for entry in competitor_snapshot[:5]:
        if entry.source_type and entry.source_type != "curated_seed":
            continue
        tasks.append(
            SearchTask(
                topic=f"{entry.competitor} {entry.asset or ''} {entry.indication or ''}".strip(),
                reason="competitor universe still relies on curated fallback more than auto-synced official evidence",
                priority="medium",
                recommended_queries=[
                    query
                    for query in (
                        " ".join(part for part in (entry.competitor, entry.asset, entry.indication, entry.region) if part),
                        " ".join(part for part in (entry.competitor, entry.target_moa, entry.indication) if part),
                    )
                    if query.strip()
                ],
                preferred_source_types=["official_site", "regulator", "registry"],
                entity=entry.competitor,
                asset=entry.asset,
                indication=entry.indication,
            )
        )
    deduped: list[SearchTask] = []
    seen: set[str] = set()
    for task in tasks:
        key = "|".join([task.topic, task.entity or "-", task.asset or "-", task.indication or "-"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(task)
    return deduped


def _preferred_domains_from_sources(*, source_group: str | None = None, source_name: str | None = None) -> list[str]:
    config = get_hanall_sources_config()
    page_checks = config.get("page_checks", {}) if isinstance(config, dict) else {}
    sources = page_checks.get("sources", []) if isinstance(page_checks, dict) else []
    domains: list[str] = []
    for raw_source in sources:
        if not isinstance(raw_source, dict):
            continue
        if source_name and str(raw_source.get("name") or "").strip() == source_name:
            url = str(raw_source.get("url") or "").strip()
        elif source_group and str(raw_source.get("source_group") or "").strip() == source_group:
            url = str(raw_source.get("url") or "").strip()
        else:
            continue
        domain = urlparse(url).netloc.lower()
        if domain and domain not in domains:
            domains.append(domain)
    return domains[:5]


def _stage_finding_identity(item: StageFinding) -> str:
    return "|".join(
        [
            item.candidate_id or "-",
            item.source_name or "-",
            item.document_id or item.trial_id or "-",
            item.primary_source_url or "-",
            item.title,
        ]
    )


def _missing_structured_fields(item: StageFinding) -> list[str]:
    missing: list[str] = []
    if item.trial_id or item.source_group == "trial_registry":
        for field_name in (
            "phase",
            "recruitment_status",
            "enrollment",
            "primary_completion_date",
            "last_update_posted",
            "target_moa",
        ):
            if not str(getattr(item, field_name) or "").strip():
                missing.append(field_name)
        if not item.site_countries:
            missing.append("site_countries")
    if item.document_id or item.filing_type or item.regulator or item.source_group == "regulator_disclosure":
        for field_name in (
            "filing_type",
            "filed_at",
            "accepted_at",
            "regulator",
            "exchange",
            "event_action",
            "regulatory_phrase",
        ):
            if not str(getattr(item, field_name) or "").strip():
                missing.append(field_name)
        if not item.key_numbers:
            missing.append("key_numbers")
    if item.category == "competitor_relevant" or item.source_group == "competitor_official":
        for field_name in ("stage_status", "target_moa", "region"):
            if not str(getattr(item, field_name, None) or "").strip():
                missing.append(field_name)
    return list(dict.fromkeys(missing))


def _build_gap_target_queries(
    *,
    entity: str | None,
    asset: str | None,
    indication: str | None,
    region: str | None,
    field_targets: list[str],
    trial_id: str | None = None,
    document_id: str | None = None,
    source_name: str | None = None,
) -> list[str]:
    queries: list[str] = []
    if trial_id:
        queries.append(f"{trial_id} site:clinicaltrials.gov OR site:euclinicaltrials.eu OR site:jrct.niph.go.jp OR site:chictr.org.cn OR site:trialsearch.who.int")
    if document_id:
        queries.append(f"{document_id} site:krx.co.kr OR site:kind.krx.co.kr OR site:ema.europa.eu OR site:pmda.go.jp OR site:english.nmpa.gov.cn")
    base = " ".join(part for part in (entity, asset, indication, region) if part)
    if base:
        queries.append(base)
    if field_targets and base:
        queries.append(f"{base} {' '.join(field_targets[:2])}")
    if source_name and asset:
        queries.append(f"{source_name} {asset} official")
    return [query for query in list(dict.fromkeys(query.strip() for query in queries if query.strip()))][:4]


def build_stage2_gap_targets(
    *,
    stage1_output: HanallStage1StructuredOutput,
    current_now: datetime,
) -> list[SearchGapTarget]:
    targets: list[SearchGapTarget] = []

    for item in [*stage1_output.company_direct_confirmed, *stage1_output.competitor_relevant_confirmed]:
        missing_fields = _missing_structured_fields(item)
        if not missing_fields:
            continue
        source_group = _resolve_source_group(
            source_group=item.source_group,
            source_family=item.source_family,
            source_name=item.source_name,
        )
        targets.append(
            SearchGapTarget(
                priority="P0",
                candidate_id=item.candidate_id,
                finding_identity=_stage_finding_identity(item),
                gap_type="missing_structured_field",
                field_targets=missing_fields[:6],
                preferred_queries=_build_gap_target_queries(
                    entity=item.entity,
                    asset=item.asset,
                    indication=item.indication,
                    region=item.region,
                    field_targets=missing_fields,
                    trial_id=item.trial_id,
                    document_id=item.document_id,
                    source_name=item.source_name,
                ),
                preferred_source_types=["official", "regulator", "registry"],
                preferred_domains=_preferred_domains_from_sources(source_group=source_group, source_name=item.source_name),
                entity=item.entity,
                asset=item.asset,
                indication=item.indication,
                region=item.region,
            )
        )

    weak_source_groups = [
        entry for entry in stage1_output.omission_audit
        if entry.axis == "source_group" and entry.status in {"open", "monitoring"}
    ]
    for entry in weak_source_groups[:4]:
        targets.append(
            SearchGapTarget(
                priority="P1",
                gap_type="weak_coverage",
                field_targets=[],
                preferred_queries=[
                    query
                    for query in (
                        f"HanAll Biopharma Immunovant {entry.source_group} latest official",
                        f"Immunovant {entry.source_group} site:immunovant.com",
                    )
                    if query.strip()
                ],
                preferred_source_types=["official", "regulator", "registry"],
                preferred_domains=_preferred_domains_from_sources(source_group=entry.source_group),
                entity="HanAll/Immunovant watch",
                region=entry.region,
            )
        )

    weak_universe_entries = [
        entry
        for entry in stage1_output.competitor_map_snapshot
        if (entry.provenance_score is None or entry.provenance_score < 0.7 or (entry.source_type or "") == "curated_seed")
    ]
    for entry in weak_universe_entries[:4]:
        targets.append(
            SearchGapTarget(
                priority="P1",
                finding_identity="|".join(
                    [
                        entry.competitor,
                        entry.asset or "-",
                        entry.indication or "-",
                    ]
                ),
                gap_type="weak_universe",
                field_targets=[field for field in ("stage_status", "target_moa", "region") if not str(getattr(entry, field) or "").strip()],
                preferred_queries=_build_gap_target_queries(
                    entity=entry.competitor,
                    asset=entry.asset,
                    indication=entry.indication,
                    region=entry.region,
                    field_targets=["stage_status", "target_moa", "region"],
                    source_name=entry.source_label,
                ),
                preferred_source_types=["official", "regulator", "registry"],
                preferred_domains=[urlparse(entry.primary_source_url).netloc.lower()] if entry.primary_source_url else [],
                entity=entry.competitor,
                asset=entry.asset,
                indication=entry.indication,
                region=entry.region,
            )
        )

    if not stage1_output.company_direct_confirmed and not stage1_output.competitor_relevant_confirmed and stage1_output.coverage.level in {"Low", "Medium"}:
        missing_groups = [
            entry.source_group
            for entry in weak_source_groups
            if entry.source_group
        ]
        targets.append(
            SearchGapTarget(
                priority="P0",
                gap_type="missing_official_item",
                field_targets=[],
                preferred_queries=[
                    "HanAll Biopharma Immunovant latest official update",
                    "Immunovant latest press release investor presentation site:immunovant.com",
                ],
                preferred_source_types=["official", "regulator", "registry"],
                preferred_domains=_preferred_domains_from_sources(source_group=missing_groups[0]) if missing_groups else [],
                entity="HanAll/Immunovant watch",
            )
        )

    for task in stage1_output.search_tasks[:6]:
        targets.append(
            SearchGapTarget(
                priority="P1" if task.priority != "high" else "P0",
                gap_type="missing_official_item",
                field_targets=[],
                preferred_queries=task.recommended_queries,
                preferred_source_types=task.preferred_source_types,
                preferred_domains=[],
                entity=task.entity,
                asset=task.asset,
                indication=task.indication,
            )
        )

    deduped: list[SearchGapTarget] = []
    seen: set[str] = set()
    p0_count = 0
    p1_count = 0
    for target in targets:
        key = "|".join(
            [
                target.priority,
                target.candidate_id or "-",
                target.finding_identity or "-",
                target.gap_type,
                ",".join(target.field_targets),
                target.entity or "-",
                target.asset or "-",
                target.indication or "-",
            ]
        )
        if key in seen:
            continue
        if target.priority == "P0" and p0_count >= 8:
            continue
        if target.priority != "P0" and p1_count >= 10:
            continue
        seen.add(key)
        deduped.append(target)
        if target.priority == "P0":
            p0_count += 1
        else:
            p1_count += 1
    return deduped


def _normalize_stage1_item_aliases(raw_item: dict[str, Any], label: str) -> dict[str, Any]:
    normalized = dict(raw_item)
    alias_map = STAGE1_ITEM_ALIAS_MAPS.get(label, {})
    for canonical_key, aliases in alias_map.items():
        current_value = normalized.get(canonical_key)
        if str(current_value or "").strip():
            continue
        for alias_key in aliases:
            alias_value = normalized.get(alias_key)
            if str(alias_value or "").strip():
                normalized[canonical_key] = alias_value
                break
    if label == "today_scheduled_events" and not str(normalized.get("category") or "").strip():
        if str(normalized.get("scheduled_for_kst") or "").strip() and str(normalized.get("fact") or normalized.get("title") or "").strip():
            normalized["category"] = "scheduled_event"
    if label in {"checked_source_log", "coverage_gaps"} and not str(normalized.get("source_family") or "").strip():
        source_name = str(normalized.get("source_name") or "").strip()
        if source_name in SOURCE_FAMILY_FALLBACKS:
            normalized["source_family"] = SOURCE_FAMILY_FALLBACKS[source_name]
    return normalized


def _summarize_stage1_validation_failure(
    raw_item: dict[str, Any],
    normalized_item: dict[str, Any],
    exc: ValidationError,
) -> tuple[list[str], list[str]]:
    missing_fields: list[str] = []
    for error in exc.errors():
        if error.get("type") == "missing":
            location = error.get("loc") or ()
            if location:
                missing_fields.append(str(location[0]))
    raw_keys = sorted(str(key) for key in raw_item.keys())
    deduped_missing = list(dict.fromkeys(missing_fields))
    return deduped_missing, raw_keys


def _validate_list_items_with_diagnostics(raw_items: Any, model_type: Any, label: str) -> tuple[list[Any], int]:
    validated: list[Any] = []
    invalid_count = 0
    for index, raw_item in enumerate(_coerce_list(raw_items), start=1):
        if not isinstance(raw_item, dict):
            continue
        normalized_item = _normalize_stage1_item_aliases(raw_item, label)
        try:
            validated.append(model_type.model_validate(normalized_item))
        except ValidationError as exc:
            invalid_count += 1
            missing_fields, raw_keys = _summarize_stage1_validation_failure(raw_item, normalized_item, exc)
            logger.warning(
                "hanall stage1 item validation failed label=%s index=%s missing_required_fields=%s raw_keys=%s",
                label,
                index,
                ",".join(missing_fields) or "-",
                ",".join(raw_keys) or "-",
            )
    return validated, invalid_count


def _parse_stage1_output_with_diagnostics(raw_text: str) -> tuple[HanallStage1StructuredOutput, int]:
    payload = json.loads(_clean_json_text(raw_text))
    if not isinstance(payload, dict):
        raise ValueError("stage1 payload must be object")
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]

    invalid_item_count = 0
    coverage = CoverageSummary.model_validate(payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {})

    today_scheduled_events, invalid = _validate_list_items_with_diagnostics(payload.get("today_scheduled_events"), StageFinding, "today_scheduled_events")
    invalid_item_count += invalid
    company_direct_confirmed, invalid = _validate_list_items_with_diagnostics(payload.get("company_direct_confirmed"), StageFinding, "company_direct_confirmed")
    invalid_item_count += invalid
    competitor_relevant_confirmed, invalid = _validate_list_items_with_diagnostics(payload.get("competitor_relevant_confirmed"), StageFinding, "competitor_relevant_confirmed")
    invalid_item_count += invalid
    competitor_map_snapshot, invalid = _validate_list_items_with_diagnostics(payload.get("competitor_map_snapshot"), CompetitorMapEntry, "competitor_map_snapshot")
    invalid_item_count += invalid
    checked_source_log, invalid = _validate_list_items_with_diagnostics(payload.get("checked_source_log"), CheckedSourceLogEntry, "checked_source_log")
    invalid_item_count += invalid
    unverified_leads, invalid = _validate_list_items_with_diagnostics(payload.get("unverified_leads"), StageFinding, "unverified_leads")
    invalid_item_count += invalid
    coverage_gaps, invalid = _validate_list_items_with_diagnostics(payload.get("coverage_gaps"), CoverageGap, "coverage_gaps")
    invalid_item_count += invalid
    omission_audit, invalid = _validate_list_items_with_diagnostics(payload.get("omission_audit"), OmissionAuditEntry, "omission_audit")
    invalid_item_count += invalid
    search_tasks, invalid = _validate_list_items_with_diagnostics(payload.get("search_tasks"), SearchTask, "search_tasks")
    invalid_item_count += invalid
    search_gap_targets, invalid = _validate_list_items_with_diagnostics(payload.get("search_gap_targets"), SearchGapTarget, "search_gap_targets")
    invalid_item_count += invalid

    return (
        HanallStage1StructuredOutput(
            coverage=coverage,
            today_scheduled_events=today_scheduled_events,
            company_direct_confirmed=company_direct_confirmed,
            competitor_relevant_confirmed=competitor_relevant_confirmed,
            competitor_map_snapshot=competitor_map_snapshot,
            checked_source_log=checked_source_log,
            unverified_leads=unverified_leads,
            coverage_gaps=coverage_gaps,
            omission_audit=omission_audit,
            search_tasks=search_tasks,
            search_gap_targets=search_gap_targets,
        ),
        invalid_item_count,
    )


def _parse_stage1_output(raw_text: str) -> HanallStage1StructuredOutput:
    stage1_output, _ = _parse_stage1_output_with_diagnostics(raw_text)
    return stage1_output


def _stabilize_stage1_output(
    *,
    stage1_output: HanallStage1StructuredOutput,
    official_collection: OfficialCollectionResult,
    current_now: datetime,
) -> HanallStage1StructuredOutput:
    known_events = get_hanall_known_events(
        current_now.strftime("%Y-%m-%d"),
        current_now=current_now,
        generated_events=official_collection.generated_known_events,
    )
    deterministic_coverage = _build_coverage_summary(
        findings=official_collection.findings,
        checked_source_log=official_collection.checked_source_log,
        coverage_gaps=official_collection.coverage_gaps,
    )
    if not stage1_output.today_scheduled_events:
        stage1_output.today_scheduled_events = _build_known_event_findings(known_events, current_now)
    if not stage1_output.checked_source_log:
        stage1_output.checked_source_log = list(official_collection.checked_source_log)
    if not stage1_output.coverage_gaps:
        stage1_output.coverage_gaps = list(official_collection.coverage_gaps)
    if not stage1_output.search_tasks:
        stage1_output.search_tasks = _build_search_tasks(
            official_collection.findings,
            stage1_output.coverage_gaps,
            stage1_output.competitor_map_snapshot,
        )
    if not stage1_output.search_gap_targets:
        stage1_output.search_gap_targets = build_stage2_gap_targets(
            stage1_output=stage1_output,
            current_now=current_now,
        )
    if not stage1_output.omission_audit:
        stage1_output.omission_audit = _build_omission_audit(
            findings=official_collection.findings,
            checked_source_log=stage1_output.checked_source_log,
            coverage_gaps=stage1_output.coverage_gaps,
            competitor_snapshot=stage1_output.competitor_map_snapshot,
            page_items=official_collection.page_items,
            known_events=known_events,
            current_now=current_now,
        )
        stage1_output.omission_audit.insert(
            0,
            OmissionAuditEntry(
                topic="structured_output_defaults",
                detail="stage1 output omitted omission_audit; defaults were injected",
                status="defaulted",
            ),
        )
    stage1_output.coverage.official_findings_count = deterministic_coverage.official_findings_count
    stage1_output.coverage.source_log_count = deterministic_coverage.source_log_count
    stage1_output.coverage.coverage_gap_count = deterministic_coverage.coverage_gap_count
    if not stage1_output.coverage.level:
        stage1_output.coverage.level = deterministic_coverage.level
    if not stage1_output.coverage.rationale:
        stage1_output.coverage.rationale = deterministic_coverage.rationale
    logger.info(
        "hanall stage1 stabilized company=%s competitor=%s search_tasks=%s source_logs=%s gaps=%s",
        len(stage1_output.company_direct_confirmed),
        len(stage1_output.competitor_relevant_confirmed),
        len(stage1_output.search_tasks),
        len(stage1_output.checked_source_log),
        len(stage1_output.coverage_gaps),
    )
    return stage1_output


def _base_bucket_for_finding(finding: RawFinding) -> str:
    if finding.category in {"scheduled_event", "event_schedule"}:
        return "today_scheduled_events"
    is_confirmed = finding.confidence >= 0.75 and bool(finding.primary_source_url)
    if not is_confirmed:
        return "unverified_leads"
    if _is_direct_company(finding):
        return "company_direct_confirmed"
    return "competitor_relevant_confirmed"


def build_stage1_deterministic_base(
    *,
    official_collection: OfficialCollectionResult,
    current_now: datetime,
) -> Stage1DeterministicBase:
    findings = official_collection.findings
    known_events = get_hanall_known_events(
        current_now.strftime("%Y-%m-%d"),
        current_now=current_now,
        generated_events=official_collection.generated_known_events,
    )
    today_scheduled_events = _build_known_event_findings(known_events, current_now)
    competitor_snapshot = _build_competitor_snapshot(
        findings=findings,
        checked_source_log=official_collection.checked_source_log,
        page_items=official_collection.page_items,
        current_now=current_now,
    )
    company_direct: list[StageFinding] = []
    competitor: list[StageFinding] = []
    unverified: list[StageFinding] = []
    candidate_map: dict[str, StageFinding] = {}
    candidate_order: list[str] = []
    candidate_bucket_map: dict[str, str] = {}

    for raw_finding in findings:
        stage_finding = _with_candidate_id(_to_stage_finding(raw_finding))
        bucket = _base_bucket_for_finding(raw_finding)
        if bucket == "today_scheduled_events":
            today_scheduled_events.append(stage_finding)
            continue
        if not stage_finding.candidate_id:
            continue
        if bucket == "company_direct_confirmed":
            company_direct.append(stage_finding)
        elif bucket == "competitor_relevant_confirmed":
            competitor.append(stage_finding)
        else:
            unverified.append(stage_finding)
        if stage_finding.candidate_id not in candidate_map:
            candidate_order.append(stage_finding.candidate_id)
        candidate_map[stage_finding.candidate_id] = stage_finding
        candidate_bucket_map[stage_finding.candidate_id] = bucket

    deduped_schedule = _dedupe_stage_findings(today_scheduled_events)
    deduped_company = _dedupe_stage_findings(company_direct)
    deduped_competitor = _dedupe_stage_findings(competitor)
    deduped_unverified = _dedupe_stage_findings(unverified)

    omission_audit = [
        OmissionAuditEntry(
            topic="official_api_collection",
            detail=(
                "deterministic_base "
                f"official_findings={len(findings)} "
                f"candidates={len(candidate_map)} "
                f"source_logs={len(official_collection.checked_source_log)} "
                f"coverage_gaps={len(official_collection.coverage_gaps)}"
            ),
            status="deterministic_base",
        )
    ]
    if not candidate_map:
        omission_audit.append(
            OmissionAuditEntry(
                topic="sparse_candidates",
                detail="deterministic base has no overlay candidates in the 24-hour window",
                status="open",
            )
        )
    omission_audit.extend(
        _build_omission_audit(
            findings=findings,
            checked_source_log=official_collection.checked_source_log,
            coverage_gaps=official_collection.coverage_gaps,
            competitor_snapshot=competitor_snapshot,
            page_items=official_collection.page_items,
            known_events=known_events,
            current_now=current_now,
        )
    )

    base_output = HanallStage1StructuredOutput(
        coverage=_build_coverage_summary(
            findings=findings,
            checked_source_log=official_collection.checked_source_log,
            coverage_gaps=official_collection.coverage_gaps,
        ),
        today_scheduled_events=deduped_schedule,
        company_direct_confirmed=deduped_company,
        competitor_relevant_confirmed=deduped_competitor,
        competitor_map_snapshot=competitor_snapshot,
        checked_source_log=list(official_collection.checked_source_log),
        unverified_leads=deduped_unverified,
        coverage_gaps=list(official_collection.coverage_gaps),
        omission_audit=omission_audit,
        search_tasks=_build_search_tasks(findings, official_collection.coverage_gaps, competitor_snapshot),
        search_gap_targets=[],
    )
    base_output.search_gap_targets = build_stage2_gap_targets(
        stage1_output=base_output,
        current_now=current_now,
    )
    return Stage1DeterministicBase(
        output=base_output,
        candidate_map=candidate_map,
        candidate_order=candidate_order,
        candidate_bucket_map=candidate_bucket_map,
    )


def build_stage1_fallback_output(
    *,
    official_collection: OfficialCollectionResult,
    current_now: datetime,
) -> HanallStage1StructuredOutput:
    return build_stage1_deterministic_base(
        official_collection=official_collection,
        current_now=current_now,
    ).output

def _render_key_value_pairs(pairs: list[tuple[str, str | None]]) -> str:
    rendered = [f"{label}: {value}" for label, value in pairs if str(value or "").strip()]
    return " | ".join(rendered)


def _render_compact_provenance_summary(item: StageFinding) -> str | None:
    action_to_fields: dict[str, list[str]] = {}
    source_labels: list[str] = []
    for field_name, entries in item.field_provenance.items():
        for entry in entries:
            if entry.action_type not in {
                "filled_blank",
                "replaced_metadata_only",
                "retained_stage1",
                "conflict_kept_stage1",
                "discovered_finding",
            }:
                continue
            action_to_fields.setdefault(entry.action_type, [])
            if field_name not in action_to_fields[entry.action_type]:
                action_to_fields[entry.action_type].append(field_name)
            label = entry.provenance_strength or entry.source_type
            if label and label not in source_labels:
                source_labels.append(label)
    if not action_to_fields:
        return None
    if action_to_fields.get("filled_blank") or action_to_fields.get("replaced_metadata_only"):
        fields = action_to_fields.get("filled_blank", []) + action_to_fields.get("replaced_metadata_only", [])
        return f"보강근거: {', '.join(source_labels[:2])} ({', '.join(fields[:5])})"
    if action_to_fields.get("discovered_finding"):
        return f"보강근거: {', '.join(source_labels[:2])} (discovered_finding)"
    if action_to_fields.get("retained_stage1") or action_to_fields.get("conflict_kept_stage1"):
        fields = action_to_fields.get("retained_stage1", []) + action_to_fields.get("conflict_kept_stage1", [])
        return f"검증판정: stage1 값 유지 ({', '.join(fields[:5])})"
    return None


def _render_stage_finding(item: StageFinding) -> list[str]:
    lines = [
        f"- {item.entity} | {item.title}",
    ]
    header_pairs = [
        ("published", item.published_at_kst or item.updated_at_kst),
        ("source", _friendly_source_name(item.source_family, item.source_name)),
        ("source_group", _resolve_source_group(
            source_group=item.source_group,
            source_family=item.source_family,
            source_name=item.source_name,
        )),
    ]
    header_line = _render_key_value_pairs(header_pairs)
    if header_line:
        lines.append(header_line)
    reference_pairs = [
        ("Trial ID", item.trial_id),
        ("Document ID", item.document_id),
        ("filing type", item.filing_type),
        ("Asset", item.asset),
        ("Indication", item.indication),
        ("Region", item.region),
        ("Stage status", item.stage_status),
    ]
    reference_line = _render_key_value_pairs(reference_pairs)
    if reference_line:
        lines.append(reference_line)
    clinical_pairs = [
        ("Sponsor", item.sponsor),
        ("Target/MOA", item.target_moa),
        ("Phase", item.phase),
        ("Recruitment status", item.recruitment_status),
        ("Enrollment", item.enrollment),
        ("Primary completion date", item.primary_completion_date),
        ("Last update posted", item.last_update_posted),
    ]
    clinical_line = _render_key_value_pairs(clinical_pairs)
    if clinical_line:
        lines.append(clinical_line)
    if item.site_countries:
        lines.append(f"Site countries: {', '.join(item.site_countries)}")
    if item.changed_fields:
        lines.append(f"Changed fields: {', '.join(item.changed_fields)}")
    disclosure_pairs = [
        ("regulator", item.regulator),
        ("exchange", item.exchange),
        ("filed_at", item.filed_at),
        ("accepted_at", item.accepted_at),
        ("event_action", item.event_action),
    ]
    disclosure_line = _render_key_value_pairs(disclosure_pairs)
    if disclosure_line:
        lines.append(disclosure_line)
    if item.regulatory_phrase:
        lines.append(f"Regulatory phrase: {item.regulatory_phrase}")
    if item.key_numbers:
        lines.append(f"key numbers: {', '.join(item.key_numbers)}")
    provenance_line = _render_compact_provenance_summary(item)
    if provenance_line:
        lines.append(provenance_line)
    insider_pairs = [
        ("insider_person", item.insider_person),
        ("insider_role", item.insider_role),
        ("insider_quantity", item.insider_quantity),
        ("insider_price", item.insider_price),
        ("trade_date", item.trade_date),
    ]
    insider_line = _render_key_value_pairs(insider_pairs)
    if insider_line:
        lines.append(insider_line)
    if item.summary or item.source_note:
        lines.append(f"Summary: {smart_truncate(item.summary or item.source_note or '확인 불가', 320)}")
    if item.reason_unverified:
        unverified_pairs = [
            ("Unverified reason", item.reason_unverified),
            ("Missing verification", item.missing_verification_target),
            ("Likely category", item.likely_category),
        ]
        unverified_line = _render_key_value_pairs(unverified_pairs)
        if unverified_line:
            lines.append(unverified_line)
    if item.suggested_official_followup_queries:
        lines.append(
            "Follow-up queries: "
            + ", ".join(item.suggested_official_followup_queries[:2])
        )
    lines.append(f"Primary source: {item.primary_source_url or '-'}")
    return lines


def _render_stage_block(title: str, items: list[StageFinding], *, empty_text: str = "- 없음") -> list[str]:
    lines = [title]
    if not items:
        lines.append(empty_text)
        return lines
    for item in items:
        lines.extend(_render_stage_finding(item))
    return lines


def _render_competitor_map(entries: list[CompetitorMapEntry]) -> list[str]:
    lines = ["Competitor Map Snapshot"]
    if not entries:
        lines.append("- 없음")
        return lines
    for entry in entries:
        lines.append(
            f"- {entry.competitor} | {entry.asset or '확인 불가'} | {entry.indication or '확인 불가'} | {entry.stage_status or '확인 불가'}"
        )
        detail_pairs = [
            ("layer", entry.layer),
            ("region", entry.region),
            ("target_moa", entry.target_moa),
            ("source_label", entry.source_label),
        ]
        detail_line = _render_key_value_pairs(detail_pairs)
        if detail_line:
            lines.append(detail_line)
        if entry.aliases:
            lines.append(f"aliases: {', '.join(entry.aliases)}")
        if entry.evidence_titles:
            lines.append(f"evidence: {', '.join(entry.evidence_titles)}")
        lines.append(f"primary_source_url: {entry.primary_source_url or '-'}")
    return lines


def _friendly_source_name(source_family: str | None, source_name: str | None) -> str:
    normalized_name = str(source_name or "").strip()
    normalized_family = str(source_family or "").strip()
    mapping = {
        "clinicaltrials": "ClinicalTrials.gov",
        "sec_api": "SEC 공시",
        "sec_official": "SEC 공시",
        "sec": "SEC 공시",
        "fmp": "FMP SEC 공시",
        "opendart": "OpenDART",
        "openfda": "openFDA",
        "cris": "CRIS",
        "mfds": "식약처(MFDS)",
        "ncbi": "PubMed/NCBI",
        "europe_pmc": "Europe PMC",
        "crossref": "Crossref",
        "biorxiv": "bioRxiv/medRxiv",
        "local_known_events": "로컬 일정",
        "local_schedule": "로컬 일정",
        "rss": "RSS 보강",
        "company_official": "Company official page-check",
        "competitor_official": "Competitor official page-check",
        "trial_registry": "Trial registry page-check",
        "regulator_disclosure": "Regulator / disclosure page-check",
        "discovery_only": "Discovery-only page-check",
    }
    return mapping.get(normalized_name) or mapping.get(normalized_family) or normalized_name or normalized_family or "-"


def _friendly_source_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "checked":
        return "확인 완료"
    if normalized in {"disabled", "approval_gated_disabled"}:
        return "현재 미사용"
    if normalized in {"request_error", "fetch_error", "collector_exception"}:
        return "연결 오류"
    if normalized in {"invalid_config", "api_error"}:
        return "추가 확인 필요"
    if normalized.startswith("http_403"):
        return "접근 제한"
    if normalized.startswith("http_404"):
        return "조회 결과 없음"
    if normalized.startswith("http_429"):
        return "응답 제한"
    if normalized.startswith("http_"):
        return "응답 오류"
    return "추가 확인 필요"


def _friendly_source_note(entry: CheckedSourceLogEntry) -> str:
    note = str(entry.note or "").strip()
    if "stage2_search_verified" in note:
        base = "stage2 search verification으로 재확인 완료"
        match = re.search(r"items=(\d+)", note)
        if match and int(match.group(1)) <= 0:
            return f"{base} (신규 확인 항목 없음)"
        if match and int(match.group(1)) > 0:
            return f"{base} (관련 항목 점검 완료)"
        return base
    if entry.status == "checked":
        match = re.search(r"items=(\d+)", note)
        if match:
            item_count = int(match.group(1))
            if item_count <= 0:
                return "신규 확인 항목 없음"
            if "detail_checked=" in note:
                return "관련 항목과 세부 등록 정보까지 점검 완료"
            if "financial_items=" in note:
                return "관련 공시 및 재무 항목 점검 완료"
            return "관련 항목 점검 완료"
        if note:
            return "관련 자료 점검 완료"
        return "점검 완료"
    if entry.status in {"disabled", "approval_gated_disabled"}:
        return "현재 수집 대상에서 제외된 자료입니다."
    if entry.status.startswith("http_403"):
        return "외부 서비스 접근이 제한되어 일부 자료를 확인하지 못했습니다."
    if entry.status.startswith("http_404"):
        return "현재 조건으로는 조회 가능한 결과가 없었습니다."
    if entry.status.startswith("http_429"):
        return "외부 서비스 응답 제한으로 잠시 후 재확인이 필요합니다."
    if entry.status in {"request_error", "fetch_error", "collector_exception"}:
        return "외부 서비스 연결 문제로 확인이 지연되었습니다."
    return smart_truncate(note or "추가 확인이 필요합니다.", 180)


def _render_source_logs(entries: list[CheckedSourceLogEntry]) -> list[str]:
    lines = ["Checked Source Log"]
    if not entries:
        lines.append("- 없음")
        return lines
    for entry in entries:
        source_group = _resolve_source_group(
            source_group=entry.source_group,
            source_family=entry.source_family,
            source_name=entry.source_name,
        )
        lines.append(
            f"- [{source_group}] {_friendly_source_name(entry.source_family, entry.source_name)} | {_friendly_source_status(entry.status)} | {entry.checked_at_kst}"
        )
        detail_bits = [f"note={_friendly_source_note(entry)}"]
        if entry.latest_item_title:
            detail_bits.append(f"latest_title={entry.latest_item_title}")
        if entry.latest_item_url:
            detail_bits.append(f"latest_url={entry.latest_item_url}")
        if entry.access_restriction:
            detail_bits.append(f"access={entry.access_restriction}")
        if entry.discovery_only:
            detail_bits.append("discovery_only=true")
        lines.append("상세: " + " | ".join(detail_bits))
    return lines


def _render_coverage_gaps(entries: list[CoverageGap]) -> list[str]:
    lines = ["Coverage Gaps"]
    if not entries:
        lines.append("- 없음")
        return lines
    for entry in entries:
        source_group = _resolve_source_group(
            source_group=entry.source_group,
            source_family=entry.source_family,
            source_name=entry.source_name,
        )
        lines.append(f"- [{source_group}] {_friendly_source_name(entry.source_family, entry.source_name)} | {_friendly_gap_type(entry.gap_type)}")
        detail_bits = [f"detail={_friendly_gap_detail(entry)}"]
        if entry.indication:
            detail_bits.append(f"indication={entry.indication}")
        if entry.region:
            detail_bits.append(f"region={entry.region}")
        if entry.discovery_only:
            detail_bits.append("discovery_only=true")
        lines.append("상세: " + " | ".join(detail_bits))
    return lines


def _render_omission_audit(entries: list[OmissionAuditEntry]) -> list[str]:
    lines = ["Omission Audit"]
    if not entries:
        lines.append("- 없음")
        return lines
    for entry in entries:
        scope_bits = [entry.axis]
        if entry.source_group:
            scope_bits.append(entry.source_group)
        if entry.indication:
            scope_bits.append(entry.indication)
        if entry.region:
            scope_bits.append(entry.region)
        lines.append(f"- {_friendly_omission_topic(entry.topic)} | {_friendly_omission_status(entry.status)} | {' / '.join(bit for bit in scope_bits if bit)}")
        lines.append(f"상세: {smart_truncate(entry.detail, 240)}")
    return lines


def _render_search_tasks(entries: list[SearchTask], stage1_output: HanallStage1StructuredOutput) -> list[str]:
    lines = ["검증 메모"]
    provenance_counter: Counter[str] = Counter()
    for item in [
        *stage1_output.company_direct_confirmed,
        *stage1_output.competitor_relevant_confirmed,
        *stage1_output.unverified_leads,
    ]:
        for field_entries in item.field_provenance.values():
            for entry in field_entries:
                provenance_counter[entry.action_type] += 1
    lines.append(f"- 추가 확인 메모: {len(entries)}건")
    if provenance_counter:
        lines.append(
            "- stage2 provenance: "
            + ", ".join(f"{action}={count}" for action, count in provenance_counter.items())
        )
    if not entries:
        lines.append("- 세부 메모 없음")
        return lines
    for entry in entries[:5]:
        related_bits = [bit for bit in (entry.entity, entry.asset, entry.indication) if bit]
        lines.append(f"- {entry.topic}{f' | {', '.join(related_bits)}' if related_bits else ''}")
        lines.append(f"메모: {smart_truncate(entry.reason, 120)}")
    return lines


def _friendly_gap_type(gap_type: str) -> str:
    normalized = str(gap_type or "").strip().lower()
    if normalized in {"disabled", "approval_gated_disabled"}:
        return "현재 미사용 자료"
    if normalized.startswith("http_403"):
        return "접근 제한"
    if normalized.startswith("http_404"):
        return "조회 결과 없음"
    if normalized.startswith("http_429"):
        return "응답 제한"
    if normalized in {"request_error", "fetch_error"}:
        return "연결 오류"
    if normalized == "collector_exception":
        return "수집 예외"
    if normalized == "api_error":
        return "응답 형식 확인 필요"
    if normalized == "invalid_config":
        return "설정 확인 필요"
    return "추가 확인 필요"


def _friendly_gap_detail(entry: CoverageGap) -> str:
    normalized = str(entry.gap_type or "").strip().lower()
    if normalized in {"disabled", "approval_gated_disabled"}:
        return "현재 수집 대상에서 제외된 자료입니다."
    if normalized.startswith("http_403"):
        return "외부 서비스 접근이 제한되어 해당 자료를 확인하지 못했습니다."
    if normalized.startswith("http_404"):
        return "현재 조건으로는 조회 가능한 결과가 없었습니다."
    if normalized.startswith("http_429"):
        return "외부 서비스 응답 제한으로 잠시 후 재확인이 필요합니다."
    if normalized in {"request_error", "fetch_error"}:
        return "외부 서비스 연결 문제로 확인이 지연되었습니다."
    if normalized == "collector_exception":
        return "수집 과정에서 예외가 발생해 후속 확인이 필요합니다."
    return smart_truncate(entry.detail or "추가 확인이 필요합니다.", 200)


def _friendly_omission_topic(topic: str) -> str:
    mapping = {
        "official_api_collection": "공식 API 수집 범위 점검",
        "sparse_candidates": "오늘 범위 내 핵심 업데이트 점검",
        "structured_output_defaults": "브리핑 기본 구조 보정",
    }
    normalized = str(topic or "").strip()
    if normalized.startswith("known_event:"):
        return f"기등록 일정 stale 점검 ({normalized.split(':', 1)[1] or '-'})"
    return mapping.get(normalized, normalized or "누락 점검")


def _friendly_omission_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"deterministic_base", "defaulted", "completed", "done"}:
        return "점검 완료"
    if normalized in {"monitoring", "in_progress"}:
        return "모니터링 중"
    if normalized == "open":
        return "후속 확인 필요"
    return "점검 진행"


def _merge_source_logs(stage1_output: HanallStage1StructuredOutput, rss_collection: RSSCollectionResult) -> list[CheckedSourceLogEntry]:
    merged = [*stage1_output.checked_source_log, *rss_collection.checked_source_log]
    deduped: list[CheckedSourceLogEntry] = []
    seen: set[str] = set()
    for entry in merged:
        key = "|".join([entry.source_family, entry.source_name, entry.status, entry.endpoint or "-"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _merge_coverage_gaps(stage1_output: HanallStage1StructuredOutput, rss_collection: RSSCollectionResult) -> list[CoverageGap]:
    merged = [*stage1_output.coverage_gaps, *rss_collection.coverage_gaps]
    deduped: list[CoverageGap] = []
    seen: set[str] = set()
    for entry in merged:
        key = "|".join([entry.source_family, entry.source_name, entry.gap_type, entry.endpoint or "-", entry.detail])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


SECTION_HEADING_ALIASES = {
    "요약": "요약",
    "오늘 예정 이벤트": "오늘 예정 이벤트",
    "회사 업데이트": "Confirmed Updates — Company Direct",
    "회사 직접 업데이트": "Confirmed Updates — Company Direct",
    "Confirmed Updates — Company Direct": "Confirmed Updates — Company Direct",
    "Confirmed Updates - Company Direct": "Confirmed Updates — Company Direct",
    "경쟁사 업데이트": "Confirmed Updates — Competitor Relevant",
    "경쟁사 관련 업데이트": "Confirmed Updates — Competitor Relevant",
    "Confirmed Updates — Competitor Relevant": "Confirmed Updates — Competitor Relevant",
    "Confirmed Updates - Competitor Relevant": "Confirmed Updates — Competitor Relevant",
    "Competitor Map Snapshot": "Competitor Map Snapshot",
    "경쟁사 맵 스냅샷": "Competitor Map Snapshot",
    "경쟁사 동향 요약": "Competitor Map Snapshot",
    "Checked Source Log": "Checked Source Log",
    "확인 소스 로그": "Checked Source Log",
    "확인한 자료": "Checked Source Log",
    "Unverified Leads": "Unverified Leads",
    "미확인 단서": "Unverified Leads",
    "추가 확인 필요": "Unverified Leads",
    "Coverage Gaps": "Coverage Gaps",
    "커버리지 공백": "Coverage Gaps",
    "아직 확인이 필요한 부분": "Coverage Gaps",
    "Omission Audit": "Omission Audit",
    "누락 감사": "Omission Audit",
    "누락 점검": "Omission Audit",
    "검증 메모": "검증 메모",
    "참고 메모": "검증 메모",
}


def _normalize_heading_line(raw_line: str) -> str | None:
    stripped = str(raw_line or "").strip().replace("**", "").replace("__", "").replace("`", "")
    if not stripped:
        return None
    candidate = re.sub(r"^[#>\-\*\s]*(?:\d+[.)]\s*)?", "", stripped)
    candidate = candidate.strip().rstrip(":").strip()
    return SECTION_HEADING_ALIASES.get(candidate)


def _ensure_section_placeholders(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    section_indexes = [index for index, line in enumerate(lines) if line in REQUIRED_SECTION_HEADINGS]
    if not section_indexes:
        return lines
    enriched: list[str] = []
    for index, line in enumerate(lines):
        enriched.append(line)
        if line not in REQUIRED_SECTION_HEADINGS:
            continue
        next_index = next((item for item in section_indexes if item > index), len(lines))
        has_content = any(entry.strip() for entry in lines[index + 1 : next_index])
        if not has_content:
            enriched.append("- 없음")
    return enriched


def normalize_hanall_final_text(raw_text: str) -> str:
    normalized = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    normalized = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", normalized, count=1)
    normalized = re.sub(r"\s*```$", "", normalized, count=1)

    compact_lines: list[str] = []
    previous_blank = False
    for raw_line in normalized.split("\n"):
        heading = _normalize_heading_line(raw_line)
        cleaned = heading or raw_line.strip().replace("**", "").replace("__", "").replace("`", "")
        cleaned = re.sub(r"^[•·]\s+", "- ", cleaned)
        if not cleaned:
            if previous_blank:
                continue
            compact_lines.append("")
            previous_blank = True
            continue
        compact_lines.append(cleaned)
        previous_blank = False

    compact_lines = _ensure_section_placeholders(compact_lines)
    normalized = "\n".join(compact_lines).strip()
    if normalized and not normalized.startswith("[한올/Immunovant 24시간 브리핑]"):
        normalized = f"[한올/Immunovant 24시간 브리핑]\n{normalized}"
    return normalized.strip()


def _normalize_generated_final_text(raw_text: str) -> str:
    return normalize_hanall_final_text(raw_text)


def _final_text_has_required_sections(text: str) -> bool:
    normalized = _normalize_generated_final_text(text)
    if not normalized or normalized.lstrip().startswith("{"):
        return False
    indexes: list[int] = []
    for heading in REQUIRED_SECTION_HEADINGS:
        match = re.search(rf"(?m)^{re.escape(heading)}$", normalized)
        if match is None:
            return False
        indexes.append(match.start())
    return indexes == sorted(indexes)


def _extract_section_body_lines(raw_text: str, heading: str) -> list[str]:
    normalized = _normalize_generated_final_text(raw_text)
    if not normalized:
        return []
    lines = normalized.split("\n")
    section_indexes = {line: index for index, line in enumerate(lines) if line in REQUIRED_SECTION_HEADINGS}
    start_index = section_indexes.get(heading)
    if start_index is None:
        return []
    next_indexes = [index for index in section_indexes.values() if index > start_index]
    end_index = min(next_indexes) if next_indexes else len(lines)
    body_lines = [line for line in lines[start_index + 1 : end_index] if line.strip()]
    return body_lines


def _render_deterministic_final_text(
    *,
    stage1_output: HanallStage1StructuredOutput,
    official_collection: OfficialCollectionResult,
    rss_collection: RSSCollectionResult,
    current_now: datetime,
    summary_lines: list[str] | None = None,
) -> str:
    window_start = current_now - timedelta(hours=24)
    company_count = len(stage1_output.company_direct_confirmed)
    competitor_count = len(stage1_output.competitor_relevant_confirmed)
    total_count = company_count + competitor_count
    merged_source_logs = _merge_source_logs(stage1_output, rss_collection)
    merged_gaps = _merge_coverage_gaps(stage1_output, rss_collection)
    ranked_deterministic_lines = _build_deterministic_summary_lines(
        stage1_output=stage1_output,
        merged_source_logs=merged_source_logs,
        merged_gaps=merged_gaps,
        current_now=current_now,
    )
    resolved_summary_lines: list[str] = []
    seen_summary_lines: set[str] = set()
    for line in [*ranked_deterministic_lines, *(summary_lines or [])]:
        normalized = str(line or "").strip()
        if not normalized or normalized in seen_summary_lines:
            continue
        seen_summary_lines.add(normalized)
        resolved_summary_lines.append(normalized)
    lines = [
        "[한올/Immunovant 24시간 브리핑]",
        f"기준: {current_now.strftime('%Y-%m-%d %H:%M KST')}",
        f"범위: {window_start.strftime('%Y-%m-%d %H:%M KST')} ~ {current_now.strftime('%Y-%m-%d %H:%M KST')}",
        f"커버리지: {stage1_output.coverage.level} ({stage1_output.coverage.rationale})",
        f"확인 이벤트: 총 {total_count}건, 회사 {company_count}건, 경쟁사 {competitor_count}건",
        f"오늘 예정 이벤트: {len(stage1_output.today_scheduled_events)}건",
        "",
        "요약",
    ]
    lines.extend(resolved_summary_lines or ["- 없음"])
    lines.append("")
    lines.extend(_render_stage_block("오늘 예정 이벤트", stage1_output.today_scheduled_events, empty_text="- 예정 또는 후속 확인 필요 일정 없음"))
    lines.append("")
    lines.extend(
        _render_stage_block(
            "Confirmed Updates — Company Direct",
            stage1_output.company_direct_confirmed,
            empty_text="- 지난 24시간 내 확인된 회사 직접 업데이트 없음",
        )
    )
    lines.append("")
    lines.extend(
        _render_stage_block(
            "Confirmed Updates — Competitor Relevant",
            stage1_output.competitor_relevant_confirmed,
            empty_text="- 지난 24시간 내 확인된 경쟁사 중요 업데이트 없음",
        )
    )
    lines.append("")
    lines.extend(_render_competitor_map(stage1_output.competitor_map_snapshot))
    lines.append("")
    lines.extend(_render_source_logs(merged_source_logs))
    lines.append("")
    lines.extend(
        _render_stage_block(
            "Unverified Leads",
            stage1_output.unverified_leads,
            empty_text="- 현재 추가 확인이 필요한 항목 없음",
        )
    )
    lines.append("")
    lines.extend(_render_coverage_gaps(merged_gaps))
    lines.append("")
    lines.extend(_render_omission_audit(stage1_output.omission_audit))
    lines.append("")
    lines.extend(_render_search_tasks(stage1_output.search_tasks, stage1_output))
    lines.append(f"- 공식 API 확인 항목: {len(official_collection.findings)}건")
    lines.append(f"- RSS 보강 항목: {len(rss_collection.items)}건")
    lines.append(f"- Checked Source Log: {len(merged_source_logs)}건")
    lines.append(f"- Coverage Gaps: {len(merged_gaps)}건")
    return "\n".join(lines).strip()


def render_stage1_fallback_text(
    *,
    stage1_output: HanallStage1StructuredOutput,
    official_collection: OfficialCollectionResult,
    rss_collection: RSSCollectionResult,
    current_now: datetime,
) -> str:
    return _render_deterministic_final_text(
        stage1_output=stage1_output,
        official_collection=official_collection,
        rss_collection=rss_collection,
        current_now=current_now,
    )


def _compact_candidate_dict(item: StageFinding, base_bucket: str) -> dict[str, Any]:
    document_ref = item.document_id or item.trial_id or item.filing_type or item.asset or "-"
    return {
        "id": item.candidate_id,
        "base_bucket": base_bucket,
        "entity": item.entity,
        "entity_type": item.entity_type,
        "category": item.category,
        "title": item.title,
        "summary": smart_truncate(item.summary or item.source_note or "", 220),
        "published_at_kst": item.published_at_kst,
        "updated_at_kst": item.updated_at_kst,
        "source_family": item.source_family,
        "source_name": item.source_name,
        "document_ref": document_ref,
        "asset": item.asset,
        "indication": item.indication,
        "region": item.region,
        "stage_status": item.stage_status,
        "phase": item.phase,
        "recruitment_status": item.recruitment_status,
        "enrollment": item.enrollment,
        "primary_completion_date": item.primary_completion_date,
        "last_update_posted": item.last_update_posted,
        "regulator": item.regulator,
        "exchange": item.exchange,
        "filed_at": item.filed_at,
        "accepted_at": item.accepted_at,
        "event_action": item.event_action,
        "key_numbers": item.key_numbers,
    }


def _build_stage1_candidate_payload(base: Stage1DeterministicBase) -> dict[str, Any]:
    bucket_counts = Counter(base.candidate_bucket_map.values())
    source_group_status: dict[str, dict[str, int]] = {}
    for entry in base.output.checked_source_log:
        group = _resolve_source_group(
            source_group=entry.source_group,
            source_family=entry.source_family,
            source_name=entry.source_name,
        )
        group_bucket = source_group_status.setdefault(group, {"checked": 0, "non_checked": 0})
        if entry.status == "checked":
            group_bucket["checked"] += 1
        else:
            group_bucket["non_checked"] += 1
    return {
        "candidate_count": len(base.candidate_order),
        "base_bucket_counts": {
            "company_direct_confirmed": bucket_counts.get("company_direct_confirmed", 0),
            "competitor_relevant_confirmed": bucket_counts.get("competitor_relevant_confirmed", 0),
            "unverified_leads": bucket_counts.get("unverified_leads", 0),
        },
        "coverage": base.output.coverage.model_dump(mode="json"),
        "today_scheduled_event_count": len(base.output.today_scheduled_events),
        "checked_source_log_count": len(base.output.checked_source_log),
        "coverage_gap_count": len(base.output.coverage_gaps),
        "checked_source_status_summary": _summarize_source_log_statuses(base.output.checked_source_log),
        "coverage_gap_type_summary": _summarize_gap_types(base.output.coverage_gaps),
        "source_group_status": source_group_status,
        "competitor_universe_snapshot": [
            entry.model_dump(mode="json")
            for entry in base.output.competitor_map_snapshot
        ],
        "competitor_universe_provenance_summary": dict(Counter(entry.source_type or "unknown" for entry in base.output.competitor_map_snapshot)),
        "omission_audit_axes": [
            entry.model_dump(mode="json")
            for entry in base.output.omission_audit
        ],
        "search_gap_targets": [
            entry.model_dump(mode="json")
            for entry in base.output.search_gap_targets
        ],
        "candidates": [
            _compact_candidate_dict(base.candidate_map[candidate_id], base.candidate_bucket_map.get(candidate_id, "unverified_leads"))
            for candidate_id in base.candidate_order
            if candidate_id in base.candidate_map
        ],
    }


STAGE1_OVERLAY_ID_ALIAS_FIELDS = {
    "company_direct_confirmed_ids": ("company_direct_ids", "company_ids"),
    "competitor_relevant_confirmed_ids": ("competitor_ids", "competitor_relevant_ids"),
    "unverified_lead_ids": ("unverified_ids", "unverified_leads_ids"),
}


def _extract_overlay_id_list(payload: dict[str, Any], field_name: str) -> tuple[list[str], int]:
    raw_value = payload.get(field_name)
    if raw_value is None:
        for alias in STAGE1_OVERLAY_ID_ALIAS_FIELDS.get(field_name, ()):
            if payload.get(alias) is not None:
                raw_value = payload.get(alias)
                break
    valid_ids: list[str] = []
    invalid_count = 0
    for index, raw_item in enumerate(_coerce_list(raw_value), start=1):
        candidate_id: str | None = None
        raw_keys: list[str] = []
        if isinstance(raw_item, str):
            candidate_id = raw_item.strip() or None
        elif isinstance(raw_item, dict):
            raw_keys = sorted(str(key) for key in raw_item.keys())
            candidate_id = str(raw_item.get("candidate_id") or raw_item.get("id") or "").strip() or None
        if candidate_id:
            valid_ids.append(candidate_id)
            continue
        invalid_count += 1
        logger.warning(
            "hanall stage1 overlay invalid id ref field=%s index=%s missing_required_fields=id raw_keys=%s",
            field_name,
            index,
            ",".join(raw_keys) or "-",
        )
    return list(dict.fromkeys(valid_ids)), invalid_count


def _overlay_has_soft_content(overlay: HanallStage1OverlayOutput) -> bool:
    return bool(
        overlay.competitor_map_snapshot
        or overlay.omission_audit
        or overlay.search_tasks
        or overlay.coverage.level
        or overlay.coverage.rationale
    )


def _parse_stage1_overlay_with_diagnostics(raw_text: str) -> tuple[HanallStage1OverlayOutput, int]:
    payload = json.loads(_clean_json_text(raw_text))
    if not isinstance(payload, dict):
        raise ValueError("stage1 overlay payload must be object")
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]

    invalid_item_count = 0
    company_ids, invalid = _extract_overlay_id_list(payload, "company_direct_confirmed_ids")
    invalid_item_count += invalid
    competitor_ids, invalid = _extract_overlay_id_list(payload, "competitor_relevant_confirmed_ids")
    invalid_item_count += invalid
    unverified_ids, invalid = _extract_overlay_id_list(payload, "unverified_lead_ids")
    invalid_item_count += invalid
    competitor_map_snapshot, invalid = _validate_list_items_with_diagnostics(payload.get("competitor_map_snapshot"), CompetitorMapEntry, "competitor_map_snapshot")
    invalid_item_count += invalid
    omission_audit, invalid = _validate_list_items_with_diagnostics(payload.get("omission_audit"), OmissionAuditEntry, "omission_audit")
    invalid_item_count += invalid
    search_tasks, invalid = _validate_list_items_with_diagnostics(payload.get("search_tasks"), SearchTask, "search_tasks")
    invalid_item_count += invalid
    search_gap_targets, invalid = _validate_list_items_with_diagnostics(payload.get("search_gap_targets"), SearchGapTarget, "search_gap_targets")
    invalid_item_count += invalid

    coverage_payload = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    coverage = CoverageOverlay.model_validate(coverage_payload)
    return (
        HanallStage1OverlayOutput(
            company_direct_confirmed_ids=company_ids,
            competitor_relevant_confirmed_ids=competitor_ids,
            unverified_lead_ids=unverified_ids,
            coverage=coverage,
            competitor_map_snapshot=competitor_map_snapshot,
            omission_audit=omission_audit,
            search_tasks=search_tasks,
            search_gap_targets=search_gap_targets,
        ),
        invalid_item_count,
    )


def _merge_stage1_overlay(
    *,
    deterministic_base: Stage1DeterministicBase,
    overlay: HanallStage1OverlayOutput,
) -> tuple[HanallStage1StructuredOutput, int, bool]:
    merged = HanallStage1StructuredOutput.model_validate(deterministic_base.output.model_dump(mode="json"))
    overlay_bucket_sets = {
        "company_direct_confirmed": set(overlay.company_direct_confirmed_ids),
        "competitor_relevant_confirmed": set(overlay.competitor_relevant_confirmed_ids),
        "unverified_leads": set(overlay.unverified_lead_ids),
    }
    known_candidate_ids = set(deterministic_base.candidate_map.keys())
    unknown_ids = {
        candidate_id
        for candidate_ids in overlay_bucket_sets.values()
        for candidate_id in candidate_ids
        if candidate_id not in known_candidate_ids
    }

    prioritized_assignments: dict[str, str] = {}
    for bucket_name in ("company_direct_confirmed", "competitor_relevant_confirmed", "unverified_leads"):
        for candidate_id in overlay_bucket_sets[bucket_name]:
            if candidate_id in known_candidate_ids and candidate_id not in prioritized_assignments:
                prioritized_assignments[candidate_id] = bucket_name

    final_bucket_map = dict(deterministic_base.candidate_bucket_map)
    final_bucket_map.update(prioritized_assignments)

    bucketed_items = {
        "company_direct_confirmed": [],
        "competitor_relevant_confirmed": [],
        "unverified_leads": [],
    }
    for candidate_id in deterministic_base.candidate_order:
        candidate = deterministic_base.candidate_map.get(candidate_id)
        bucket = final_bucket_map.get(candidate_id)
        if candidate is None or bucket not in bucketed_items:
            continue
        bucketed_items[bucket].append(candidate)

    merged.company_direct_confirmed = _dedupe_stage_findings(bucketed_items["company_direct_confirmed"])
    merged.competitor_relevant_confirmed = _dedupe_stage_findings(bucketed_items["competitor_relevant_confirmed"])
    merged.unverified_leads = _dedupe_stage_findings(bucketed_items["unverified_leads"])
    if overlay.coverage.level:
        merged.coverage.level = overlay.coverage.level
    if overlay.coverage.rationale:
        merged.coverage.rationale = overlay.coverage.rationale
    if overlay.competitor_map_snapshot:
        merged.competitor_map_snapshot = overlay.competitor_map_snapshot
    if overlay.omission_audit:
        merged.omission_audit = overlay.omission_audit
    if overlay.search_tasks:
        merged.search_tasks = overlay.search_tasks
    if overlay.search_gap_targets:
        merged.search_gap_targets = overlay.search_gap_targets
    return merged, len(unknown_ids), _overlay_has_soft_content(overlay) or bool(prioritized_assignments)


STAGE2_CONFIRMED_SOURCE_TYPES = {"official", "regulator", "registry"}
STAGE2_BACKFILL_SOURCE_TYPES = {"official", "regulator", "registry"}
STAGE2_DISCOVERY_ONLY_SOURCE_TYPES = {"discovery_only"}
STAGE2_PRESS_SOURCE_TYPES = {"trusted_press", "trusted_rss", "newswire"}
STAGE2_LIST_FIELDS = {"aliases", "site_countries", "key_numbers", "changed_fields"}
STAGE2_BACKFILLABLE_FIELDS = {
    "aliases",
    "asset",
    "sponsor",
    "target_moa",
    "indication",
    "region",
    "phase",
    "recruitment_status",
    "enrollment",
    "primary_completion_date",
    "last_update_posted",
    "site_countries",
    "filing_type",
    "filed_at",
    "accepted_at",
    "regulator",
    "exchange",
    "event_action",
    "key_numbers",
    "regulatory_phrase",
    "stage_status",
}


def _normalize_stage2_source_type(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"official", "official_site", "company_official", "competitor_official", "investor_page"}:
        return "official"
    if normalized in {"regulator", "exchange", "regulator_disclosure"}:
        return "regulator"
    if normalized in {"registry", "trial_registry", "clinical_registry"}:
        return "registry"
    if normalized in {"trusted_press", "trusted_newswire", "newswire", "trusted_rss"}:
        return "trusted_press"
    if normalized in {"discovery_only", "discovery"}:
        return "discovery_only"
    return normalized or "discovery_only"


def _stage2_source_group(*, source_type: str, entity: str | None = None) -> str:
    normalized = _normalize_stage2_source_type(source_type)
    if normalized == "regulator":
        return "regulator_disclosure"
    if normalized == "registry":
        return "trial_registry"
    if normalized == "official":
        if entity and any(company.lower() in entity.lower() for company in HANALL_DIRECT_COMPANIES):
            return "company_official"
        return "competitor_official"
    return "discovery_only"


def _stage2_source_family(*, source_type: str, source_name: str | None = None) -> str:
    normalized = _normalize_stage2_source_type(source_type)
    if normalized == "official":
        return "company_official" if (source_name or "").strip() in {"hanall_official", "immunovant_ir"} else "competitor_official"
    if normalized == "regulator":
        return "regulator_disclosure"
    if normalized == "registry":
        return "trial_registry"
    return source_name or normalized or "discovery_only"


def _parse_kst_like_datetime(value: str | None) -> datetime | None:
    return parse_known_event_kst(value) if str(value or "").strip() else None


def _is_recent_stage2_datetime(
    *,
    published_at_kst: str | None,
    updated_at_kst: str | None,
    discovered_at_kst: str | None = None,
    current_now: datetime,
) -> bool:
    candidate = (
        _parse_kst_like_datetime(updated_at_kst)
        or _parse_kst_like_datetime(published_at_kst)
        or _parse_kst_like_datetime(discovered_at_kst)
    )
    if candidate is None:
        return False
    window_start = current_now - timedelta(hours=24)
    return window_start <= candidate <= current_now + timedelta(minutes=5)


def _is_blank_stage_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"-", "n/a", "unknown", "확인 불가", "metadata_only"}
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _normalize_stage2_field_value(field_name: str, value: Any) -> Any:
    if field_name in STAGE2_LIST_FIELDS:
        if isinstance(value, list):
            normalized: list[str] = []
            for item in value:
                text = str(item or "").strip()
                if text and text not in normalized:
                    normalized.append(text)
            return normalized
        text = str(value or "").strip()
        return [text] if text else []
    return str(value or "").strip() or None


def _stage_finding_lookup_maps(
    stage1_output: HanallStage1StructuredOutput,
) -> tuple[dict[str, StageFinding], dict[str, StageFinding]]:
    by_candidate_id: dict[str, StageFinding] = {}
    by_identity: dict[str, StageFinding] = {}
    for item in [
        *stage1_output.company_direct_confirmed,
        *stage1_output.competitor_relevant_confirmed,
        *stage1_output.unverified_leads,
    ]:
        if item.candidate_id:
            by_candidate_id[item.candidate_id] = item
        by_identity[_stage_finding_identity(item)] = item
    return by_candidate_id, by_identity


def _stage_finding_dedupe_key(item: StageFinding) -> str:
    return "|".join(
        [
            (item.source_name or "-").strip().lower(),
            (item.primary_source_url or "-").strip().lower(),
            (item.document_id or item.trial_id or "-").strip().lower(),
            (item.title or "-").strip().lower(),
            (item.published_at_kst or item.updated_at_kst or "-").strip().lower(),
            (item.entity or "-").strip().lower(),
        ]
    )


def _compact_detail(value: Any, *, limit: int = 80) -> str:
    return smart_truncate(str(value or "").strip() or "-", limit)


def _provenance_strength_for_source_type(source_type: str | None, *, stage1_classification: str | None = None) -> str:
    normalized = _normalize_stage2_source_type(source_type)
    if stage1_classification == "official_structured":
        return "stage1_official_structured"
    if stage1_classification == "metadata_only":
        return "stage1_metadata_only"
    if normalized in STAGE2_CONFIRMED_SOURCE_TYPES:
        return "stage2_official_search"
    if normalized in STAGE2_PRESS_SOURCE_TYPES:
        return "stage2_trusted_newswire"
    if normalized in STAGE2_DISCOVERY_ONLY_SOURCE_TYPES:
        return "discovery_only"
    return "unknown"


def _classify_stage1_field_state(item: StageFinding, field_name: str) -> str:
    value = getattr(item, field_name, None)
    if _is_blank_stage_value(value):
        return "empty"
    provenance_entries = item.field_provenance.get(field_name, [])
    if provenance_entries and any(
        entry.action_type in {"filled_blank", "replaced_metadata_only"} for entry in provenance_entries
    ):
        return "stage2_backfilled"
    source_group = _resolve_source_group(
        source_group=item.source_group,
        source_family=item.source_family,
        source_name=item.source_name,
    )
    if source_group in {"trial_registry", "regulator_disclosure"} and item.source_tier in {
        "official_api",
        "page_check_promoted",
        "stage2_search_verified",
    }:
        return "official_structured"
    if item.source_tier == "official_api":
        return "official_structured"
    if item.source_tier in {"page_check", "page_check_promoted"} or source_group in {"company_official", "competitor_official"}:
        return "metadata_only"
    return "unknown"


def _evidence_text_blob(evidence: SearchEvidence) -> str:
    return " ".join(
        bit
        for bit in (
            evidence.topic,
            evidence.title,
            evidence.source_url,
            evidence.excerpt,
            evidence.entity,
            evidence.asset,
            evidence.indication,
        )
        if str(bit or "").strip()
    ).lower()


def _same_source_identity_for_replacement(target: StageFinding, evidence: SearchEvidence) -> bool:
    target_group = _resolve_source_group(
        source_group=target.source_group,
        source_family=target.source_family,
        source_name=target.source_name,
    )
    evidence_group = _stage2_source_group(source_type=evidence.source_type, entity=target.entity)
    target_domain = urlparse(target.primary_source_url or "").netloc.lower()
    evidence_domain = urlparse(evidence.source_url or "").netloc.lower()
    if target.trial_id and target.trial_id.lower() in _evidence_text_blob(evidence):
        return True
    if target.document_id and target.document_id.lower() in _evidence_text_blob(evidence):
        return True
    if target_domain and evidence_domain and target_domain == evidence_domain:
        return True
    if (target.source_name or "").strip() and (target.source_name or "").strip() == (evidence.source_name or "").strip():
        return True
    if target_group == evidence_group == "trial_registry" and target.trial_id:
        return True
    if target_group == evidence_group == "regulator_disclosure" and target.document_id:
        return True
    return False


def _is_more_specific_value(field_name: str, current_value: Any, new_value: Any) -> bool:
    if _is_blank_stage_value(current_value):
        return True
    if isinstance(current_value, list) or isinstance(new_value, list):
        current_list = current_value if isinstance(current_value, list) else [str(current_value or "").strip()] if str(current_value or "").strip() else []
        new_list = new_value if isinstance(new_value, list) else [str(new_value or "").strip()] if str(new_value or "").strip() else []
        return len(new_list) > len(current_list) or ", ".join(new_list) != ", ".join(current_list) and len(", ".join(new_list)) > len(", ".join(current_list))
    current_text = str(current_value or "").strip()
    new_text = str(new_value or "").strip()
    if not current_text:
        return True
    if new_text == current_text:
        return False
    if field_name == "indication" and current_text.upper() in {"MG", "RA"} and len(new_text) > len(current_text):
        return True
    if new_text.lower().startswith(current_text.lower()) and len(new_text) > len(current_text):
        return True
    return len(new_text) > len(current_text) + 2


def _append_field_provenance(
    item: StageFinding,
    *,
    field_name: str,
    field_value: Any,
    action_type: str,
    source_type: str,
    source_name: str,
    source_url: str | None,
    evidence_id: str | None,
    provenance_strength: str,
    note: str,
) -> None:
    field_entries = list(item.field_provenance.get(field_name, []))
    field_entries.append(
        FieldProvenance(
            field_name=field_name,
            field_value=None if _is_blank_stage_value(field_value) else ", ".join(field_value) if isinstance(field_value, list) else str(field_value),
            action_type=action_type,
            source_type=source_type,
            source_name=source_name,
            source_url=source_url,
            evidence_id=evidence_id,
            provenance_strength=provenance_strength,
            note=note,
        )
    )
    item.field_provenance[field_name] = field_entries


def _append_provenance_row(
    provenance_rows: list[dict[str, Any]],
    *,
    finding_identity: str,
    candidate_id: str | None,
    field_name: str,
    field_value: Any,
    action_type: str,
    source_type: str,
    source_name: str,
    source_url: str | None,
    evidence_id: str | None,
    provenance_strength: str,
    note: str,
) -> None:
    provenance_rows.append(
        {
            "finding_identity": finding_identity,
            "candidate_id": candidate_id,
            "field_name": field_name,
            "field_value": None if _is_blank_stage_value(field_value) else ", ".join(field_value) if isinstance(field_value, list) else str(field_value),
            "action_type": action_type,
            "source_type": source_type,
            "source_name": source_name,
            "source_url": source_url,
            "evidence_id": evidence_id,
            "provenance_strength": provenance_strength,
            "note": note,
            "recorded_at_kst": now_kst().strftime("%Y-%m-%d %H:%M KST"),
        }
    )


def _apply_stage2_search_note(entry: CheckedSourceLogEntry) -> CheckedSourceLogEntry:
    if "stage2_search_verified" in (entry.note or ""):
        return entry
    cloned = CheckedSourceLogEntry.model_validate(entry.model_dump(mode="json"))
    note = str(cloned.note or "").strip()
    cloned.note = f"{note} | stage2_search_verified".strip(" |")
    return cloned


def _apply_stage2_gap_note(entry: CoverageGap) -> CoverageGap:
    if entry.gap_type.startswith("stage2_search_") or entry.gap_type == "stage2_field_conflict":
        return entry
    cloned = CoverageGap.model_validate(entry.model_dump(mode="json"))
    cloned.gap_type = "stage2_search_no_primary_confirmation"
    cloned.detail = smart_truncate(f"{entry.detail} | stage2_search_no_primary_confirmation", 220)
    return cloned


def _parse_stage2_verification_output_with_diagnostics(raw_text: str) -> tuple[Stage2VerificationOutput, int]:
    payload = json.loads(_clean_json_text(raw_text))
    if not isinstance(payload, dict):
        raise ValueError("stage2 verification payload must be object")
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]

    invalid_item_count = 0
    evidence_catalog, invalid = _validate_list_items_with_diagnostics(payload.get("evidence_catalog"), SearchEvidence, "evidence_catalog")
    invalid_item_count += invalid
    backfills, invalid = _validate_list_items_with_diagnostics(payload.get("backfills"), Stage2Backfill, "backfills")
    invalid_item_count += invalid
    discovered_confirmed_findings, invalid = _validate_list_items_with_diagnostics(
        payload.get("discovered_confirmed_findings"),
        Stage2DiscoveredFinding,
        "discovered_confirmed_findings",
    )
    invalid_item_count += invalid
    discovered_unverified_leads, invalid = _validate_list_items_with_diagnostics(
        payload.get("discovered_unverified_leads"),
        Stage2DiscoveredFinding,
        "discovered_unverified_leads",
    )
    invalid_item_count += invalid
    updated_source_logs, invalid = _validate_list_items_with_diagnostics(payload.get("updated_source_logs"), CheckedSourceLogEntry, "updated_source_logs")
    invalid_item_count += invalid
    updated_coverage_gaps, invalid = _validate_list_items_with_diagnostics(payload.get("updated_coverage_gaps"), CoverageGap, "updated_coverage_gaps")
    invalid_item_count += invalid
    updated_omission_audit, invalid = _validate_list_items_with_diagnostics(payload.get("updated_omission_audit"), OmissionAuditEntry, "updated_omission_audit")
    invalid_item_count += invalid
    summary_lines = [str(line or "").strip() for line in _coerce_list(payload.get("summary_lines")) if str(line or "").strip()]

    return (
        Stage2VerificationOutput(
            evidence_catalog=evidence_catalog,
            backfills=backfills,
            discovered_confirmed_findings=discovered_confirmed_findings,
            discovered_unverified_leads=discovered_unverified_leads,
            updated_source_logs=updated_source_logs,
            updated_coverage_gaps=updated_coverage_gaps,
            updated_omission_audit=updated_omission_audit,
            summary_lines=summary_lines,
        ),
        invalid_item_count,
    )


def _parse_stage2_verification_output(raw_text: str) -> Stage2VerificationOutput:
    parsed, _ = _parse_stage2_verification_output_with_diagnostics(raw_text)
    return parsed


def _build_search_verify_prompt_replacements(
    *,
    stage1_output: HanallStage1StructuredOutput,
    rss_collection: RSSCollectionResult,
    current_now: datetime,
    known_events_context: str | None = None,
    search_memory: dict[str, Any] | None = None,
) -> dict[str, str]:
    replacements = build_hanall_base_prompt_replacements(current_now)
    if known_events_context:
        replacements["__KNOWN_EVENTS_CONTEXT__"] = known_events_context
    replacements["__STAGE1_JSON__"] = _json_dumps(stage1_output.model_dump(mode="json"))
    replacements["__RSS_RESULTS_JSON__"] = _json_dumps(rss_collection.model_dump(mode="json"))
    replacements["__SEARCH_GAP_TARGETS_JSON__"] = _json_dumps(
        [entry.model_dump(mode="json") for entry in stage1_output.search_gap_targets]
    )
    replacements["__SEARCH_MEMORY_JSON__"] = _json_dumps(search_memory or {})
    return replacements


def run_stage2_search_verification(
    *,
    stage1_output: HanallStage1StructuredOutput,
    rss_collection: RSSCollectionResult,
    current_now: datetime,
    known_events_context: str | None = None,
    search_memory: dict[str, Any] | None = None,
) -> tuple[str, Stage2VerificationOutput]:
    raw_text = run_prompt_by_key_raw(
        "hanall_news_search_verify_prompt",
        replacements=_build_search_verify_prompt_replacements(
            stage1_output=stage1_output,
            rss_collection=rss_collection,
            current_now=current_now,
            known_events_context=known_events_context,
            search_memory=search_memory,
        ),
    )
    try:
        parsed = _parse_stage2_verification_output(raw_text)
    except Exception as exc:
        setattr(exc, "stage2_raw_text", raw_text)
        raise
    return raw_text, parsed


def merge_stage2_backfills(
    *,
    stage1_output: HanallStage1StructuredOutput,
    stage2_output: Stage2VerificationOutput,
    provenance_rows: list[dict[str, Any]] | None = None,
) -> HanallStage1StructuredOutput:
    provenance_rows = provenance_rows if provenance_rows is not None else []
    evidence_map = {entry.evidence_id: entry for entry in stage2_output.evidence_catalog}
    by_candidate_id, by_identity = _stage_finding_lookup_maps(stage1_output)

    for backfill in stage2_output.backfills:
        target = None
        if backfill.candidate_id:
            target = by_candidate_id.get(backfill.candidate_id)
        if target is None and backfill.finding_identity:
            target = by_identity.get(backfill.finding_identity)
        if target is None:
            continue

        linked_evidence = [evidence_map[evidence_id] for evidence_id in backfill.evidence_ids if evidence_id in evidence_map]
        if linked_evidence and not any(
            _normalize_stage2_source_type(evidence.source_type) in STAGE2_BACKFILL_SOURCE_TYPES
            for evidence in linked_evidence
        ):
            continue

        for field_name, raw_value in backfill.filled_fields.items():
            if field_name not in STAGE2_BACKFILLABLE_FIELDS:
                continue
            value = _normalize_stage2_field_value(field_name, raw_value)
            if _is_blank_stage_value(value):
                continue
            current_value = getattr(target, field_name, None)
            linked_evidence_for_field = [
                evidence
                for evidence in linked_evidence
                if not evidence.confirms_fields or field_name in evidence.confirms_fields
            ]
            evidence = linked_evidence_for_field[0] if linked_evidence_for_field else (linked_evidence[0] if linked_evidence else None)
            source_type = _normalize_stage2_source_type(evidence.source_type if evidence else "official")
            source_name = evidence.source_name if evidence else (target.source_name or "stage2_search_verify")
            source_url = evidence.source_url if evidence else target.primary_source_url
            note = f"field={field_name}"
            finding_identity = _stage_finding_identity(target)
            current_state = _classify_stage1_field_state(target, field_name)

            if _is_blank_stage_value(current_value):
                setattr(target, field_name, value)
                _append_field_provenance(
                    target,
                    field_name=field_name,
                    field_value=value,
                    action_type="filled_blank",
                    source_type=source_type,
                    source_name=source_name,
                    source_url=source_url,
                    evidence_id=evidence.evidence_id if evidence else None,
                    provenance_strength=_provenance_strength_for_source_type(source_type),
                    note=note,
                )
                _append_provenance_row(
                    provenance_rows,
                    finding_identity=finding_identity,
                    candidate_id=target.candidate_id,
                    field_name=field_name,
                    field_value=value,
                    action_type="filled_blank",
                    source_type=source_type,
                    source_name=source_name,
                    source_url=source_url,
                    evidence_id=evidence.evidence_id if evidence else None,
                    provenance_strength=_provenance_strength_for_source_type(source_type),
                    note=note,
                )
                continue
            if current_value == value:
                continue

            can_replace_metadata_only = (
                current_state == "metadata_only"
                and source_type in STAGE2_BACKFILL_SOURCE_TYPES
                and evidence is not None
                and _same_source_identity_for_replacement(target, evidence)
                and _is_more_specific_value(field_name, current_value, value)
            )
            if can_replace_metadata_only:
                setattr(target, field_name, value)
                _append_field_provenance(
                    target,
                    field_name=field_name,
                    field_value=value,
                    action_type="replaced_metadata_only",
                    source_type=source_type,
                    source_name=source_name,
                    source_url=source_url,
                    evidence_id=evidence.evidence_id if evidence else None,
                    provenance_strength=_provenance_strength_for_source_type(source_type),
                    note=f"{note} refined_metadata_only",
                )
                _append_provenance_row(
                    provenance_rows,
                    finding_identity=finding_identity,
                    candidate_id=target.candidate_id,
                    field_name=field_name,
                    field_value=value,
                    action_type="replaced_metadata_only",
                    source_type=source_type,
                    source_name=source_name,
                    source_url=source_url,
                    evidence_id=evidence.evidence_id if evidence else None,
                    provenance_strength=_provenance_strength_for_source_type(source_type),
                    note=f"{note} refined_metadata_only",
                )
                continue

            action_type = "retained_stage1" if current_state == "official_structured" else "conflict_kept_stage1"
            provenance_strength = _provenance_strength_for_source_type(
                source_type,
                stage1_classification=current_state if action_type == "retained_stage1" else None,
            )
            stage1_output.coverage_gaps.append(
                CoverageGap(
                    source_family=_stage2_source_family(
                        source_type=source_type,
                        source_name=source_name,
                    ),
                    source_name=source_name,
                    source_group=_stage2_source_group(
                        source_type=source_type,
                        entity=target.entity,
                    ),
                    gap_type="stage2_field_conflict",
                    detail=(
                        f"candidate_id={target.candidate_id or '-'} field={field_name} "
                        f"stage1={_compact_detail(current_value)} kept_over_stage2={_compact_detail(value)}"
                    ),
                    severity="low",
                )
            )
            _append_field_provenance(
                target,
                field_name=field_name,
                field_value=current_value,
                action_type=action_type,
                source_type=source_type,
                source_name=source_name,
                source_url=source_url,
                evidence_id=evidence.evidence_id if evidence else None,
                provenance_strength=provenance_strength,
                note=f"{note} stage1_kept",
            )
            _append_provenance_row(
                provenance_rows,
                finding_identity=finding_identity,
                candidate_id=target.candidate_id,
                field_name=field_name,
                field_value=current_value,
                action_type=action_type,
                source_type=source_type,
                source_name=source_name,
                source_url=source_url,
                evidence_id=evidence.evidence_id if evidence else None,
                provenance_strength=provenance_strength,
                note=f"{note} stage1_kept",
            )

    return stage1_output


def _stage2_discovered_to_stage_finding(discovered: Stage2DiscoveredFinding) -> StageFinding:
    source_type = _normalize_stage2_source_type(discovered.source_type)
    payload = dict(discovered.structured_fields)
    payload.update(
        {
            "entity": discovered.entity,
            "entity_type": payload.get("entity_type") or "company",
            "category": "company_direct" if discovered.category == "company_direct" else "competitor_relevant",
            "title": discovered.title,
            "summary": payload.get("summary") or discovered.why_discovered,
            "published_at_kst": discovered.published_at_kst,
            "updated_at_kst": discovered.updated_at_kst,
            "source_name": discovered.source_name,
            "source_family": payload.get("source_family")
            or _stage2_source_family(source_type=source_type, source_name=discovered.source_name),
            "source_group": payload.get("source_group")
            or _stage2_source_group(source_type=source_type, entity=discovered.entity),
            "source_tier": payload.get("source_tier") or "stage2_search_verified",
            "primary_source_url": discovered.source_url,
            "discovered_at_kst": discovered.discovered_at_kst or discovered.updated_at_kst or discovered.published_at_kst,
            "reason_unverified": discovered.reason_unverified,
            "missing_verification_target": discovered.missing_verification_target,
            "suggested_official_followup_queries": discovered.suggested_official_followup_queries,
            "likely_category": discovered.likely_category,
            "related_asset": discovered.related_asset or payload.get("asset"),
            "related_indication": discovered.related_indication or payload.get("indication"),
            "source_note": payload.get("source_note") or discovered.why_discovered,
            "confidence": payload.get("confidence") or 0.85,
        }
    )
    return _with_candidate_id(StageFinding.model_validate(payload))


def _stage2_discovered_allowed_as_confirmed(
    discovered: Stage2DiscoveredFinding,
    *,
    current_now: datetime,
) -> bool:
    source_type = _normalize_stage2_source_type(discovered.source_type)
    if source_type not in STAGE2_CONFIRMED_SOURCE_TYPES:
        return False
    return _is_recent_stage2_datetime(
        published_at_kst=discovered.published_at_kst,
        updated_at_kst=discovered.updated_at_kst,
        discovered_at_kst=discovered.discovered_at_kst,
        current_now=current_now,
    )


def merge_stage2_discovered_findings(
    *,
    stage1_output: HanallStage1StructuredOutput,
    stage2_output: Stage2VerificationOutput,
    current_now: datetime,
    provenance_rows: list[dict[str, Any]] | None = None,
) -> HanallStage1StructuredOutput:
    provenance_rows = provenance_rows if provenance_rows is not None else []
    existing_keys = {
        _stage_finding_dedupe_key(item)
        for item in [
            *stage1_output.company_direct_confirmed,
            *stage1_output.competitor_relevant_confirmed,
            *stage1_output.unverified_leads,
        ]
    }

    for discovered in stage2_output.discovered_confirmed_findings:
        if not _stage2_discovered_allowed_as_confirmed(discovered, current_now=current_now):
            continue
        item = _stage2_discovered_to_stage_finding(discovered)
        dedupe_key = _stage_finding_dedupe_key(item)
        if dedupe_key in existing_keys:
            continue
        existing_keys.add(dedupe_key)
        for field_name, field_value in {"title": discovered.title, **item.model_dump(mode="json")}.items():
            if field_name not in {"title", *STAGE2_BACKFILLABLE_FIELDS}:
                continue
            if _is_blank_stage_value(field_value):
                continue
            _append_field_provenance(
                item,
                field_name=field_name,
                field_value=field_value,
                action_type="discovered_finding",
                source_type=_normalize_stage2_source_type(discovered.source_type),
                source_name=discovered.source_name,
                source_url=discovered.source_url,
                evidence_id=None,
                provenance_strength=_provenance_strength_for_source_type(discovered.source_type),
                note="discovered_confirmed_finding",
            )
            _append_provenance_row(
                provenance_rows,
                finding_identity=_stage_finding_identity(item),
                candidate_id=item.candidate_id,
                field_name=field_name,
                field_value=field_value,
                action_type="discovered_finding",
                source_type=_normalize_stage2_source_type(discovered.source_type),
                source_name=discovered.source_name,
                source_url=discovered.source_url,
                evidence_id=None,
                provenance_strength=_provenance_strength_for_source_type(discovered.source_type),
                note="discovered_confirmed_finding",
            )
        if _is_direct_company(
            RawFinding(
                source_family=item.source_family or "-",
                source_name=item.source_name or "-",
                source_group=item.source_group or "company_official",
                source_tier=item.source_tier or "stage2_search_verified",
                entity=item.entity,
                entity_type=item.entity_type,
                category=item.category,
                title=item.title,
                summary=item.summary,
                primary_source_url=item.primary_source_url,
                raw_payload={},
            )
        ):
            stage1_output.company_direct_confirmed.append(item)
        else:
            stage1_output.competitor_relevant_confirmed.append(item)

    richer_unverified = [
        *stage2_output.discovered_unverified_leads,
        *[
            Stage2DiscoveredFinding.model_validate(
                {
                    **entry.model_dump(mode="json"),
                    "category": "unverified_lead",
                    "reason_unverified": entry.reason_unverified or "discovery_only source cannot promote confirmed finding",
                    "missing_verification_target": entry.missing_verification_target or "official primary confirmation",
                    "suggested_official_followup_queries": entry.suggested_official_followup_queries
                    or [entry.title, f"{entry.entity} official {entry.related_asset or entry.related_indication or ''}".strip()],
                    "likely_category": entry.likely_category or entry.category,
                    "related_asset": entry.related_asset or entry.structured_fields.get("asset"),
                    "related_indication": entry.related_indication or entry.structured_fields.get("indication"),
                }
            )
            for entry in stage2_output.discovered_confirmed_findings
            if _normalize_stage2_source_type(entry.source_type) in STAGE2_DISCOVERY_ONLY_SOURCE_TYPES
        ],
    ]

    for discovered in richer_unverified:
        if not _is_recent_stage2_datetime(
            published_at_kst=discovered.published_at_kst,
            updated_at_kst=discovered.updated_at_kst,
            discovered_at_kst=discovered.discovered_at_kst,
            current_now=current_now,
        ):
            continue
        item = _stage2_discovered_to_stage_finding(
            Stage2DiscoveredFinding.model_validate(
                {
                    **discovered.model_dump(mode="json"),
                    "category": "unverified_lead",
                }
            )
        )
        item.category = "unverified_lead"
        dedupe_key = _stage_finding_dedupe_key(item)
        if dedupe_key in existing_keys:
            continue
        existing_keys.add(dedupe_key)
        _append_field_provenance(
            item,
            field_name="title",
            field_value=item.title,
            action_type="discovered_finding",
            source_type=_normalize_stage2_source_type(discovered.source_type),
            source_name=discovered.source_name,
            source_url=discovered.source_url,
            evidence_id=None,
            provenance_strength=_provenance_strength_for_source_type(discovered.source_type),
            note=discovered.reason_unverified or "unverified lead retained for official follow-up",
        )
        _append_provenance_row(
            provenance_rows,
            finding_identity=_stage_finding_identity(item),
            candidate_id=item.candidate_id,
            field_name="title",
            field_value=item.title,
            action_type="discovered_finding",
            source_type=_normalize_stage2_source_type(discovered.source_type),
            source_name=discovered.source_name,
            source_url=discovered.source_url,
            evidence_id=None,
            provenance_strength=_provenance_strength_for_source_type(discovered.source_type),
            note=discovered.reason_unverified or "unverified lead retained for official follow-up",
        )
        stage1_output.unverified_leads.append(item)

    stage1_output.company_direct_confirmed = _dedupe_stage_findings(stage1_output.company_direct_confirmed)
    stage1_output.competitor_relevant_confirmed = _dedupe_stage_findings(stage1_output.competitor_relevant_confirmed)
    stage1_output.unverified_leads = _dedupe_stage_findings(stage1_output.unverified_leads)
    return stage1_output


def merge_stage2_source_log_updates(
    *,
    stage1_output: HanallStage1StructuredOutput,
    stage2_output: Stage2VerificationOutput,
) -> HanallStage1StructuredOutput:
    merged = [*stage1_output.checked_source_log, *[_apply_stage2_search_note(entry) for entry in stage2_output.updated_source_logs]]
    deduped: list[CheckedSourceLogEntry] = []
    seen: set[str] = set()
    for entry in merged:
        key = "|".join(
            [
                entry.source_family,
                entry.source_name,
                entry.status,
                entry.endpoint or "-",
                entry.latest_item_url or "-",
                entry.latest_item_title or "-",
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    stage1_output.checked_source_log = deduped
    return stage1_output


def merge_stage2_coverage_updates(
    *,
    stage1_output: HanallStage1StructuredOutput,
    stage2_output: Stage2VerificationOutput,
) -> HanallStage1StructuredOutput:
    merged = [*stage1_output.coverage_gaps, *[_apply_stage2_gap_note(entry) for entry in stage2_output.updated_coverage_gaps]]
    deduped: list[CoverageGap] = []
    seen: set[str] = set()
    for entry in merged:
        key = "|".join(
            [
                entry.source_family,
                entry.source_name,
                entry.gap_type,
                entry.endpoint or "-",
                entry.detail,
                entry.indication or "-",
                entry.region or "-",
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    stage1_output.coverage_gaps = deduped
    return stage1_output


def merge_stage2_omission_audit_updates(
    *,
    stage1_output: HanallStage1StructuredOutput,
    stage2_output: Stage2VerificationOutput,
) -> HanallStage1StructuredOutput:
    merged = [*stage1_output.omission_audit, *stage2_output.updated_omission_audit]
    deduped: list[OmissionAuditEntry] = []
    seen: set[str] = set()
    for entry in merged:
        key = "|".join(
            [
                entry.topic,
                entry.axis,
                entry.status,
                entry.source_group or "-",
                entry.indication or "-",
                entry.region or "-",
                entry.detail,
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    stage1_output.omission_audit = deduped
    return stage1_output


def _refresh_stage1_coverage_counts(stage1_output: HanallStage1StructuredOutput) -> HanallStage1StructuredOutput:
    stage1_output.coverage.official_findings_count = (
        len(stage1_output.company_direct_confirmed) + len(stage1_output.competitor_relevant_confirmed)
    )
    stage1_output.coverage.source_log_count = len(stage1_output.checked_source_log)
    stage1_output.coverage.coverage_gap_count = len(stage1_output.coverage_gaps)
    return stage1_output


def _is_stage1_transient_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code in {429, 503}:
        return True
    message = str(exc or "").lower()
    return any(token in message for token in ("429", "503", "rate limit", "timed out", "timeout", "temporarily unavailable"))


def _stage1_retry_sleep(attempt: int) -> None:
    jitter = random.uniform(0.0, 0.08)
    delay = STAGE1_RETRY_BACKOFF_SECONDS * (2 ** max(0, attempt - 1)) + jitter
    time.sleep(delay)


def _build_collect_prompt_replacements(
    *,
    deterministic_base: Stage1DeterministicBase,
    current_now: datetime,
    known_events_context: str | None = None,
) -> dict[str, str]:
    replacements = build_hanall_base_prompt_replacements(current_now)
    if known_events_context:
        replacements["__KNOWN_EVENTS_CONTEXT__"] = known_events_context
    replacements["__STAGE1_CANDIDATE_PAYLOAD_JSON__"] = _json_dumps(_build_stage1_candidate_payload(deterministic_base))
    return replacements


def _build_finalize_prompt_replacements(
    *,
    stage1_output: HanallStage1StructuredOutput,
    rss_collection: RSSCollectionResult,
    current_now: datetime,
    known_events_context: str | None = None,
) -> dict[str, str]:
    replacements = build_hanall_base_prompt_replacements(current_now)
    if known_events_context:
        replacements["__KNOWN_EVENTS_CONTEXT__"] = known_events_context
    replacements["__STAGE1_JSON__"] = _json_dumps(stage1_output.model_dump(mode="json"))
    replacements["__RSS_RESULTS_JSON__"] = _json_dumps(rss_collection.model_dump(mode="json"))
    return replacements


def _build_deterministic_summary_lines(
    *,
    stage1_output: HanallStage1StructuredOutput,
    merged_source_logs: list[CheckedSourceLogEntry],
    merged_gaps: list[CoverageGap],
    current_now: datetime,
) -> list[str]:
    company_count = len(stage1_output.company_direct_confirmed)
    competitor_count = len(stage1_output.competitor_relevant_confirmed)
    total_count = company_count + competitor_count
    lines: list[str] = []
    ranked_issues = build_ranked_issue_list(stage1_output=stage1_output, current_now=current_now, limit=5)
    lines.extend(
        entry["summary_line"]
        for entry in ranked_issues
        if str(entry.get("summary_line") or "").strip()
    )
    if total_count == 0 and not stage1_output.unverified_leads:
        lines.append("- 지난 24시간 내 확인된 핵심 업데이트 없음")
        if stage1_output.today_scheduled_events:
            lines.append(f"- 다만 예정 이벤트 또는 후속 확인 필요 항목 {len(stage1_output.today_scheduled_events)}건 반영")
        else:
            lines.append("- 예정 이벤트 또는 후속 확인 필요 일정도 확인되지 않음")
    else:
        lines.append(f"- 지난 24시간 내 공식 확인 업데이트 총수: {total_count}")
        lines.append(f"- 한올/Immunovant 직접 업데이트 수: {company_count}")
        lines.append(f"- 경쟁사 중요 업데이트 수: {competitor_count}")
        if stage1_output.unverified_leads:
            lines.append(f"- 추가 확인이 필요한 항목 수: {len(stage1_output.unverified_leads)}")
    lines.append(f"- 전반적 커버리지 수준: {stage1_output.coverage.level}")
    lines.append(f"- 이유: {stage1_output.coverage.rationale}")
    lines.append(f"- Checked Source Log {len(merged_source_logs)}건, Coverage Gaps {len(merged_gaps)}건")
    deduped_lines: list[str] = []
    seen: set[str] = set()
    for line in lines:
        normalized = str(line or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped_lines.append(normalized)
    return deduped_lines


def _is_sparse_stage1(stage1_output: HanallStage1StructuredOutput, official_collection: OfficialCollectionResult) -> bool:
    return (
        len(official_collection.findings) == 0
        and len(stage1_output.unverified_leads) == 0
        and len(stage1_output.company_direct_confirmed) == 0
        and len(stage1_output.competitor_relevant_confirmed) == 0
        and len(stage1_output.search_gap_targets) == 0
    )


def _compute_stage2_input_hash(
    *,
    stage1_output: HanallStage1StructuredOutput,
    rss_collection: RSSCollectionResult,
) -> str:
    payload = {
        "stage1": stage1_output.model_dump(mode="json"),
        "rss": rss_collection.model_dump(mode="json"),
    }
    return sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 429:
        return True
    message = str(exc or "").lower()
    return "too many requests" in message or "rate limit" in message


def _stage2_retry_sleep(attempt: int) -> None:
    jitter = random.uniform(0.0, 0.12)
    delay = STAGE2_RETRY_BACKOFF_SECONDS * (2 ** max(0, attempt - 1)) + jitter
    time.sleep(delay)


def _stage2_rate_limit_cooldown_active(stage2_input_hash: str) -> bool:
    return (
        _stage2_rate_limit_state.get("input_hash") == stage2_input_hash
        and float(_stage2_rate_limit_state.get("cooldown_until_monotonic", 0.0)) > time.monotonic()
    )


def _record_stage2_rate_limit(stage2_input_hash: str) -> None:
    _stage2_rate_limit_state["input_hash"] = stage2_input_hash
    _stage2_rate_limit_state["cooldown_until_monotonic"] = time.monotonic() + STAGE2_RATE_LIMIT_COOLDOWN_SECONDS


def _clear_stage2_rate_limit_state() -> None:
    _stage2_rate_limit_state["input_hash"] = None
    _stage2_rate_limit_state["cooldown_until_monotonic"] = 0.0


def _reset_stage2_rate_limit_state() -> None:
    _clear_stage2_rate_limit_state()


def run_hanall_news_pipeline(*, current_now: datetime | None = None, room_key: str | None = None) -> HanallNewsPipelineResult:
    pipeline_start = perf_counter()
    now = current_now or now_kst()
    official_collection = collect_hanall_official_findings(current_now=now)
    known_events_context = build_hanall_known_events_context(
        now.strftime("%Y-%m-%d"),
        current_now=now,
        generated_events=official_collection.generated_known_events,
    )

    deterministic_base = build_stage1_deterministic_base(
        official_collection=official_collection,
        current_now=now,
    )
    stage1_output = HanallStage1StructuredOutput.model_validate(deterministic_base.output.model_dump(mode="json"))
    stage1_raw = _json_dumps(stage1_output.model_dump(mode="json"))
    used_stage1_fallback = False
    stage1_mode = "llm_overlay_merged"
    stage1_attempts = 0
    stage1_invalid_item_count = 0
    stage1_invalid_ref_count = 0
    stage1_candidate_count = len(deterministic_base.candidate_order)
    stage1_start = perf_counter()
    if stage1_candidate_count == 0:
        used_stage1_fallback = True
        stage1_mode = "deterministic_base_only_sparse"
    else:
        for attempt in range(1, STAGE1_TOTAL_ATTEMPTS + 1):
            stage1_attempts = attempt
            try:
                stage1_raw = run_prompt_by_key_raw(
                    "hanall_news_collect_prompt",
                    replacements=_build_collect_prompt_replacements(
                        deterministic_base=deterministic_base,
                        current_now=now,
                        known_events_context=known_events_context,
                    ),
                )
                overlay_output, stage1_invalid_item_count = _parse_stage1_overlay_with_diagnostics(stage1_raw)
                merged_output, stage1_invalid_ref_count, had_overlay_effect = _merge_stage1_overlay(
                    deterministic_base=deterministic_base,
                    overlay=overlay_output,
                )
                if stage1_invalid_ref_count:
                    logger.warning(
                        "hanall stage1 overlay ignored unknown ids unknown_ids_count=%s",
                        stage1_invalid_ref_count,
                    )
                if had_overlay_effect:
                    stage1_output = merged_output
                    stage1_mode = "llm_overlay_merged"
                else:
                    stage1_output = HanallStage1StructuredOutput.model_validate(deterministic_base.output.model_dump(mode="json"))
                    stage1_mode = "deterministic_base_due_to_invalid_overlay"
                    used_stage1_fallback = True
                break
            except Exception as exc:
                if _is_stage1_transient_error(exc) and attempt < STAGE1_TOTAL_ATTEMPTS:
                    logger.warning(
                        "hanall stage1 transient error; retrying attempt=%s max_attempts=%s error=%s",
                        attempt,
                        STAGE1_TOTAL_ATTEMPTS,
                        exc.__class__.__name__,
                    )
                    _stage1_retry_sleep(attempt)
                    continue
                stage1_output = HanallStage1StructuredOutput.model_validate(deterministic_base.output.model_dump(mode="json"))
                used_stage1_fallback = True
                stage1_mode = "deterministic_base_due_to_llm_error" if _is_stage1_transient_error(exc) else "deterministic_base_due_to_invalid_overlay"
                logger.warning(
                    "hanall stage1 overlay failed; using deterministic base mode=%s attempts=%s error=%s",
                    stage1_mode,
                    attempt,
                    exc.__class__.__name__,
                )
                break
    stage1_output = _stabilize_stage1_output(
        stage1_output=stage1_output,
        official_collection=official_collection,
        current_now=now,
    )
    logger.info(
        "hanall stage1 completed mode=%s attempts=%s candidate_count=%s invalid_items=%s invalid_refs=%s used_fallback=%s search_tasks=%s elapsed_ms=%.1f",
        stage1_mode,
        stage1_attempts,
        stage1_candidate_count,
        stage1_invalid_item_count,
        stage1_invalid_ref_count,
        used_stage1_fallback,
        len(stage1_output.search_tasks),
        (perf_counter() - stage1_start) * 1000,
    )

    rss_start = perf_counter()
    rss_collection = fetch_hanall_rss_results(current_now=now)
    logger.info(
        "hanall rss completed feed_count=%s items=%s coverage_gaps=%s elapsed_ms=%.1f",
        rss_collection.checked_feed_count,
        len(rss_collection.items),
        len(rss_collection.coverage_gaps),
        (perf_counter() - rss_start) * 1000,
    )
    stage1_output_before_stage2 = HanallStage1StructuredOutput.model_validate(stage1_output.model_dump(mode="json"))
    stage2_search_memory = build_stage2_search_memory(
        stage1_output=stage1_output_before_stage2,
        current_now=now,
        room_key=room_key,
    )
    stage2_search_plan = build_search_plan_from_memory(
        stage1_output=stage1_output_before_stage2,
        search_memory=stage2_search_memory,
        current_now=now,
    )

    used_stage2_fallback = False
    stage2_start = perf_counter()
    stage2_fallback_reason = "none"
    render_mode = "stage2_search_verify"
    stage2_attempts = 0
    stage2_skipped_reason: str | None = None
    stage2_input_hash = _compute_stage2_input_hash(stage1_output=stage1_output, rss_collection=rss_collection)
    stage2_verification_output: Stage2VerificationOutput | None = None
    stage2_trace_id = make_trace_id()
    stage2_run_started_at_kst = now_kst().strftime("%Y-%m-%d %H:%M KST")
    stage2_provenance_rows: list[dict[str, Any]] = []
    stage2_used_search_verify = not _is_sparse_stage1(stage1_output, official_collection) and not _stage2_rate_limit_cooldown_active(stage2_input_hash)
    record_stage2_verification_run_start(
        trace_id=stage2_trace_id,
        room_key=room_key,
        run_started_at_kst=stage2_run_started_at_kst,
        used_search_verify=stage2_used_search_verify,
        gap_target_count=len(stage1_output.search_gap_targets),
    )

    def _render_deterministic() -> tuple[str, str]:
        raw_text = render_stage1_fallback_text(
            stage1_output=stage1_output,
            official_collection=official_collection,
            rss_collection=rss_collection,
            current_now=now,
        )
        return raw_text, _normalize_generated_final_text(raw_text)

    if _is_sparse_stage1(stage1_output, official_collection):
        stage2_fallback_reason = "sparse_stage1"
        render_mode = "deterministic_due_to_sparse_stage1"
        stage2_skipped_reason = "sparse_stage1_no_updates"
        final_raw_text, final_text = _render_deterministic()
        used_stage2_fallback = True
        finalize_stage2_verification_run(
            trace_id=stage2_trace_id,
            run_finished_at_kst=now_kst().strftime("%Y-%m-%d %H:%M KST"),
            stage2_status="skipped_sparse_stage1",
            reused_evidence_count=int(stage2_search_plan.get("reused_evidence_count") or 0),
        )
    elif _stage2_rate_limit_cooldown_active(stage2_input_hash):
        stage2_fallback_reason = "rate_limit"
        render_mode = "deterministic_due_to_rate_limit"
        stage2_skipped_reason = "rate_limit_cooldown_active_same_input"
        final_raw_text, final_text = _render_deterministic()
        used_stage2_fallback = True
        finalize_stage2_verification_run(
            trace_id=stage2_trace_id,
            run_finished_at_kst=now_kst().strftime("%Y-%m-%d %H:%M KST"),
            stage2_status="rate_limit_cooldown",
            reused_evidence_count=int(stage2_search_plan.get("reused_evidence_count") or 0),
        )
    else:
        last_stage2_exc: Exception | None = None
        for attempt in range(1, STAGE2_RATE_LIMIT_TOTAL_ATTEMPTS + 1):
            stage2_attempts = attempt
            try:
                final_raw_text, stage2_verification_output = run_stage2_search_verification(
                    stage1_output=stage1_output,
                    rss_collection=rss_collection,
                    current_now=now,
                    known_events_context=known_events_context,
                    search_memory=stage2_search_plan,
                )
                merged_stage1_output = HanallStage1StructuredOutput.model_validate(stage1_output.model_dump(mode="json"))
                merged_stage1_output = merge_stage2_backfills(
                    stage1_output=merged_stage1_output,
                    stage2_output=stage2_verification_output,
                    provenance_rows=stage2_provenance_rows,
                )
                merged_stage1_output = merge_stage2_discovered_findings(
                    stage1_output=merged_stage1_output,
                    stage2_output=stage2_verification_output,
                    current_now=now,
                    provenance_rows=stage2_provenance_rows,
                )
                merged_stage1_output = merge_stage2_source_log_updates(
                    stage1_output=merged_stage1_output,
                    stage2_output=stage2_verification_output,
                )
                merged_stage1_output = merge_stage2_coverage_updates(
                    stage1_output=merged_stage1_output,
                    stage2_output=stage2_verification_output,
                )
                merged_stage1_output = merge_stage2_omission_audit_updates(
                    stage1_output=merged_stage1_output,
                    stage2_output=stage2_verification_output,
                )
                merged_stage1_output = _refresh_stage1_coverage_counts(merged_stage1_output)
                final_text = _normalize_generated_final_text(
                    _render_deterministic_final_text(
                        stage1_output=merged_stage1_output,
                        official_collection=official_collection,
                        rss_collection=rss_collection,
                        current_now=now,
                        summary_lines=stage2_verification_output.summary_lines,
                    )
                )
                stage2_status = "success"
                discovered_unverified_count = len(stage2_verification_output.discovered_unverified_leads) + sum(
                    1
                    for entry in stage2_verification_output.discovered_confirmed_findings
                    if _normalize_stage2_source_type(entry.source_type) in STAGE2_DISCOVERY_ONLY_SOURCE_TYPES
                )
                if (
                    len(stage2_verification_output.evidence_catalog) == 0
                    and len(stage2_verification_output.backfills) == 0
                    and len(stage2_verification_output.discovered_confirmed_findings) == 0
                    and discovered_unverified_count == 0
                    and len(stage2_verification_output.updated_source_logs) == 0
                    and len(stage2_verification_output.updated_coverage_gaps) == 0
                    and len(stage2_verification_output.updated_omission_audit) == 0
                ):
                    stage2_status = "no_result"
                persist_stage2_verification_success(
                    trace_id=stage2_trace_id,
                    run_finished_at_kst=now_kst().strftime("%Y-%m-%d %H:%M KST"),
                    stage2_status=stage2_status,
                    evidence_catalog=[entry.model_dump(mode="json") for entry in stage2_verification_output.evidence_catalog],
                    provenance_rows=stage2_provenance_rows,
                    backfill_count=len(stage2_verification_output.backfills),
                    discovered_confirmed_count=len(stage2_verification_output.discovered_confirmed_findings),
                    discovered_unverified_count=discovered_unverified_count,
                    coverage_upgrade_count=(
                        len(stage2_verification_output.updated_source_logs)
                        + len(stage2_verification_output.updated_coverage_gaps)
                        + len(stage2_verification_output.updated_omission_audit)
                    ),
                    reused_evidence_count=int(stage2_search_plan.get("reused_evidence_count") or 0),
                )
                stage1_output = merged_stage1_output
                render_mode = "stage2_search_verify_plus_deterministic"
                _clear_stage2_rate_limit_state()
                break
            except Exception as exc:
                last_stage2_exc = exc
                if _is_rate_limit_error(exc):
                    stage2_fallback_reason = "rate_limit"
                    if attempt < STAGE2_RATE_LIMIT_TOTAL_ATTEMPTS:
                        logger.warning(
                            "hanall stage2 rate limited; retrying attempt=%s max_attempts=%s",
                            attempt,
                            STAGE2_RATE_LIMIT_TOTAL_ATTEMPTS,
                        )
                        _stage2_retry_sleep(attempt)
                        continue
                    _record_stage2_rate_limit(stage2_input_hash)
                    render_mode = "deterministic_due_to_rate_limit"
                    stage2_skipped_reason = "rate_limit_after_retry"
                    final_raw_text, final_text = _render_deterministic()
                    used_stage2_fallback = True
                    finalize_stage2_verification_run(
                        trace_id=stage2_trace_id,
                        run_finished_at_kst=now_kst().strftime("%Y-%m-%d %H:%M KST"),
                        stage2_status="rate_limit_after_retry",
                        reused_evidence_count=int(stage2_search_plan.get("reused_evidence_count") or 0),
                        error_detail="stage2 google_search rate limited after retry",
                    )
                    break

                stage2_fallback_reason = _classify_stage2_fallback_reason(exc)
                logger.warning(
                    "hanall stage2 prompt failed; using fallback renderer reason=%s error=%s official_findings=%s rss_items=%s company=%s competitor=%s coverage_gaps=%s source_statuses=%s gap_types=%s",
                    stage2_fallback_reason,
                    exc,
                    len(official_collection.findings),
                    len(rss_collection.items),
                    len(stage1_output.company_direct_confirmed),
                    len(stage1_output.competitor_relevant_confirmed),
                    len(stage1_output.coverage_gaps),
                    _summarize_source_log_statuses(stage1_output.checked_source_log),
                    _summarize_gap_types(stage1_output.coverage_gaps),
                )
                render_mode = "deterministic_fallback"
                final_raw_text, final_text = _render_deterministic()
                used_stage2_fallback = True
                stage2_verification_output = None
                finalize_stage2_verification_run(
                    trace_id=stage2_trace_id,
                    run_finished_at_kst=now_kst().strftime("%Y-%m-%d %H:%M KST"),
                    stage2_status=stage2_fallback_reason,
                    reused_evidence_count=int(stage2_search_plan.get("reused_evidence_count") or 0),
                    error_detail=smart_truncate(
                        str(getattr(exc, "stage2_raw_text", "")).strip() or str(exc),
                        320,
                    ),
                )
                break
        else:
            last_stage2_exc = RuntimeError("stage2 execution loop exhausted")
            finalize_stage2_verification_run(
                trace_id=stage2_trace_id,
                run_finished_at_kst=now_kst().strftime("%Y-%m-%d %H:%M KST"),
                stage2_status="failed",
                reused_evidence_count=int(stage2_search_plan.get("reused_evidence_count") or 0),
                error_detail="stage2 execution loop exhausted",
            )

        if last_stage2_exc is not None and not used_stage2_fallback and render_mode != "stage2_llm":
            used_stage2_fallback = True
    logger.info(
        "hanall stage2 completed used_fallback=%s reason=%s render_mode=%s attempts=%s skipped_reason=%s rss_items=%s source_statuses=%s gap_types=%s elapsed_ms=%.1f",
        used_stage2_fallback,
        stage2_fallback_reason,
        render_mode,
        stage2_attempts,
        stage2_skipped_reason or "-",
        len(rss_collection.items),
        _summarize_source_log_statuses(stage1_output.checked_source_log),
        _summarize_gap_types(stage1_output.coverage_gaps),
        (perf_counter() - stage2_start) * 1000,
    )
    logger.info(
        "hanall pipeline completed stage1_mode=%s stage1_attempts=%s stage1_candidate_count=%s stage1_invalid_items=%s stage1_invalid_refs=%s stage1_fallback=%s stage2_fallback=%s stage2_reason=%s render_mode=%s stage2_attempts=%s stage2_skipped_reason=%s official_findings=%s unverified=%s search_tasks=%s elapsed_ms=%.1f",
        stage1_mode,
        stage1_attempts,
        stage1_candidate_count,
        stage1_invalid_item_count,
        stage1_invalid_ref_count,
        used_stage1_fallback,
        used_stage2_fallback,
        stage2_fallback_reason,
        render_mode,
        stage2_attempts,
        stage2_skipped_reason or "-",
        len(official_collection.findings),
        len(stage1_output.unverified_leads),
        len(stage1_output.search_tasks),
        (perf_counter() - pipeline_start) * 1000,
    )
    logger.info(
        "hanall metric stage_fallback_counts stage1=%s stage2=%s stage1_mode=%s stage1_attempts=%s stage1_candidate_count=%s stage1_invalid_items=%s stage1_invalid_refs=%s stage2_reason=%s render_mode=%s stage2_attempts=%s official_findings=%s rss_items=%s coverage_gaps=%s checked_source_logs=%s",
        1 if used_stage1_fallback else 0,
        1 if used_stage2_fallback else 0,
        stage1_mode,
        stage1_attempts,
        stage1_candidate_count,
        stage1_invalid_item_count,
        stage1_invalid_ref_count,
        stage2_fallback_reason,
        render_mode,
        stage2_attempts,
        len(official_collection.findings),
        len(rss_collection.items),
        len(stage1_output.coverage_gaps),
        len(stage1_output.checked_source_log),
    )
    logger.info(
        "hanall metric pipeline_summary stage1_mode=%s stage1_attempts=%s stage1_candidate_count=%s stage1_invalid_item_count=%s stage1_invalid_ref_count=%s official_findings_count=%s unverified_leads_count=%s render_mode=%s stage2_attempts=%s coverage_gap_count=%s",
        stage1_mode,
        stage1_attempts,
        stage1_candidate_count,
        stage1_invalid_item_count,
        stage1_invalid_ref_count,
        len(official_collection.findings),
        len(stage1_output.unverified_leads),
        render_mode,
        stage2_attempts,
        len(stage1_output.coverage_gaps),
    )
    logger.info(
        "hanall metric stage2_fallback used=%s count=%s reason=%s render_mode=%s skipped_reason=%s official_findings=%s rss_items=%s coverage_gaps=%s checked_source_logs=%s",
        used_stage2_fallback,
        1 if used_stage2_fallback else 0,
        stage2_fallback_reason,
        render_mode,
        stage2_skipped_reason or "-",
        len(official_collection.findings),
        len(rss_collection.items),
        len(stage1_output.coverage_gaps),
        len(stage1_output.checked_source_log),
    )

    ranked_issues = build_ranked_issue_list(stage1_output=stage1_output, current_now=now, limit=10)
    try:
        persist_hanall_run_snapshot(
            trace_id=stage2_trace_id,
            room_key=room_key,
            created_at_kst=now_kst().strftime("%Y-%m-%d %H:%M KST"),
            stage1_candidate_summary=_build_stage1_candidate_payload(deterministic_base),
            stage1_output=stage1_output_before_stage2.model_dump(mode="json"),
            merged_stage1_output=stage1_output.model_dump(mode="json"),
            stage2_output=stage2_verification_output.model_dump(mode="json") if stage2_verification_output is not None else None,
            search_memory={
                "memory": stage2_search_memory,
                "plan": stage2_search_plan,
            },
            ranked_issues=ranked_issues,
            summary_lines=_extract_section_body_lines(final_text, "요약"),
            final_text=final_text,
            debug_meta={
                "stage1_mode": stage1_mode,
                "stage1_attempts": stage1_attempts,
                "stage1_candidate_count": stage1_candidate_count,
                "stage1_invalid_item_count": stage1_invalid_item_count,
                "stage1_invalid_ref_count": stage1_invalid_ref_count,
                "stage2_attempts": stage2_attempts,
                "stage2_skipped_reason": stage2_skipped_reason,
                "stage2_used_search_verify": stage2_used_search_verify,
                "stage2_reused_evidence_count": int(stage2_search_plan.get("reused_evidence_count") or 0),
                "used_stage1_fallback": used_stage1_fallback,
                "used_stage2_fallback": used_stage2_fallback,
                "stage2_fallback_reason": stage2_fallback_reason,
                "render_mode": render_mode,
            },
        )
    except Exception as exc:
        logger.warning("failed to persist hanall run snapshot trace_id=%s error=%s", stage2_trace_id, exc)

    return HanallNewsPipelineResult(
        final_text=final_text,
        raw_output_text=final_raw_text,
        stage1_output=stage1_output,
        official_collection=official_collection,
        rss_collection=rss_collection,
        stage2_verification_output=stage2_verification_output,
        stage2_trace_id=stage2_trace_id,
        used_stage1_fallback=used_stage1_fallback,
        used_stage2_fallback=used_stage2_fallback,
        stage1_mode=stage1_mode,
        stage1_attempts=stage1_attempts,
        stage1_invalid_ref_count=stage1_invalid_ref_count,
        stage1_candidate_count=stage1_candidate_count,
        render_mode=render_mode,
        stage2_attempts=stage2_attempts,
        stage2_skipped_reason=stage2_skipped_reason,
        stage1_invalid_item_count=stage1_invalid_item_count,
    )
