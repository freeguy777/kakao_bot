from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.utils import now_kst


def _strip_or_none(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _format_kst(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(now_kst().tzinfo).strftime("%Y-%m-%d %H:%M KST")


class RawFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_family: str
    source_name: str
    source_tier: str = "official_api"
    entity: str
    entity_type: str = "company"
    category: str
    title: str
    summary: str = ""
    published_at: datetime | None = None
    updated_at: datetime | None = None
    published_at_kst: str | None = None
    updated_at_kst: str | None = None
    document_type: str | None = None
    document_id: str | None = None
    filing_type: str | None = None
    trial_id: str | None = None
    asset: str | None = None
    indication: str | None = None
    region: str | None = None
    primary_source_url: str | None = None
    secondary_source_url: str | None = None
    source_note: str | None = None
    confidence: float = 0.5
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_fields(self) -> "RawFinding":
        self.source_family = _strip_or_none(self.source_family) or "-"
        self.source_name = _strip_or_none(self.source_name) or "-"
        self.source_tier = _strip_or_none(self.source_tier) or "official_api"
        self.entity = _strip_or_none(self.entity) or "-"
        self.entity_type = _strip_or_none(self.entity_type) or "company"
        self.category = _strip_or_none(self.category) or "general_update"
        self.title = _strip_or_none(self.title) or "-"
        self.summary = _strip_or_none(self.summary) or ""
        self.document_type = _strip_or_none(self.document_type)
        self.document_id = _strip_or_none(self.document_id)
        self.filing_type = _strip_or_none(self.filing_type)
        self.trial_id = _strip_or_none(self.trial_id)
        self.asset = _strip_or_none(self.asset)
        self.indication = _strip_or_none(self.indication)
        self.region = _strip_or_none(self.region)
        self.primary_source_url = _strip_or_none(self.primary_source_url)
        self.secondary_source_url = _strip_or_none(self.secondary_source_url)
        self.source_note = _strip_or_none(self.source_note)
        self.published_at_kst = _strip_or_none(self.published_at_kst) or _format_kst(self.published_at)
        self.updated_at_kst = _strip_or_none(self.updated_at_kst) or _format_kst(self.updated_at)
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if not isinstance(self.raw_payload, dict):
            self.raw_payload = {}
        return self


class SearchTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str
    reason: str
    priority: str = "medium"
    recommended_queries: list[str] = Field(default_factory=list)
    preferred_source_types: list[str] = Field(default_factory=list)
    entity: str | None = None
    asset: str | None = None
    indication: str | None = None


class CoverageSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    level: str = "Low"
    rationale: str = ""
    official_findings_count: int = 0
    source_log_count: int = 0
    coverage_gap_count: int = 0


class CoverageOverlay(BaseModel):
    model_config = ConfigDict(extra="ignore")

    level: str | None = None
    rationale: str | None = None


class StageFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidate_id: str | None = None
    entity: str
    entity_type: str = "company"
    category: str
    title: str
    summary: str = ""
    published_at_kst: str | None = None
    updated_at_kst: str | None = None
    source_family: str | None = None
    source_name: str | None = None
    source_tier: str | None = None
    primary_source_url: str | None = None
    secondary_source_url: str | None = None
    document_type: str | None = None
    document_id: str | None = None
    filing_type: str | None = None
    trial_id: str | None = None
    asset: str | None = None
    indication: str | None = None
    region: str | None = None
    source_note: str | None = None
    confidence: float | None = None


class CompetitorMapEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    competitor: str
    asset: str | None = None
    indication: str | None = None
    relevance: str = ""
    evidence_titles: list[str] = Field(default_factory=list)


class CheckedSourceLogEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_family: str
    source_name: str
    status: str
    checked_at_kst: str
    note: str = ""
    endpoint: str | None = None
    http_status: int | None = None


class CoverageGap(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_family: str
    source_name: str
    gap_type: str
    detail: str
    severity: str = "medium"
    endpoint: str | None = None
    http_status: int | None = None


class OmissionAuditEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str
    detail: str
    status: str = "open"


class HanallStage1StructuredOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    coverage: CoverageSummary = Field(default_factory=CoverageSummary)
    today_scheduled_events: list[StageFinding] = Field(default_factory=list)
    company_direct_confirmed: list[StageFinding] = Field(default_factory=list)
    competitor_relevant_confirmed: list[StageFinding] = Field(default_factory=list)
    competitor_map_snapshot: list[CompetitorMapEntry] = Field(default_factory=list)
    checked_source_log: list[CheckedSourceLogEntry] = Field(default_factory=list)
    unverified_leads: list[StageFinding] = Field(default_factory=list)
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)
    omission_audit: list[OmissionAuditEntry] = Field(default_factory=list)
    search_tasks: list[SearchTask] = Field(default_factory=list)


class HanallStage1OverlayOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company_direct_confirmed_ids: list[str] = Field(default_factory=list)
    competitor_relevant_confirmed_ids: list[str] = Field(default_factory=list)
    unverified_lead_ids: list[str] = Field(default_factory=list)
    coverage: CoverageOverlay = Field(default_factory=CoverageOverlay)
    competitor_map_snapshot: list[CompetitorMapEntry] = Field(default_factory=list)
    omission_audit: list[OmissionAuditEntry] = Field(default_factory=list)
    search_tasks: list[SearchTask] = Field(default_factory=list)


class RSSItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    feed_name: str
    category: str
    title: str
    summary: str = ""
    published_at: datetime | None = None
    published_at_kst: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _normalize_fields(self) -> "RSSItem":
        self.feed_name = _strip_or_none(self.feed_name) or "-"
        self.category = _strip_or_none(self.category) or "rss"
        self.title = _strip_or_none(self.title) or "-"
        self.summary = _strip_or_none(self.summary) or ""
        self.url = _strip_or_none(self.url)
        self.published_at_kst = _strip_or_none(self.published_at_kst) or _format_kst(self.published_at)
        return self


class RSSCollectionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    checked_feed_count: int = 0
    items: list[RSSItem] = Field(default_factory=list)
    checked_source_log: list[CheckedSourceLogEntry] = Field(default_factory=list)
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)


class OfficialCollectionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    findings: list[RawFinding] = Field(default_factory=list)
    checked_source_log: list[CheckedSourceLogEntry] = Field(default_factory=list)
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)
