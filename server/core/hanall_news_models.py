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


def _normalize_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        normalized: list[str] = []
        for item in value:
            text = _strip_or_none(item)
            if text and text not in normalized:
                normalized.append(text)
        return normalized
    text = _strip_or_none(value)
    return [text] if text else []


class RawFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_family: str
    source_name: str
    source_group: str = "regulator_disclosure"
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
    aliases: list[str] = Field(default_factory=list)
    sponsor: str | None = None
    target_moa: str | None = None
    indication: str | None = None
    region: str | None = None
    stage_status: str | None = None
    phase: str | None = None
    recruitment_status: str | None = None
    enrollment: str | None = None
    primary_completion_date: str | None = None
    last_update_posted: str | None = None
    site_countries: list[str] = Field(default_factory=list)
    changed_fields: list[str] = Field(default_factory=list)
    regulator: str | None = None
    exchange: str | None = None
    filed_at: str | None = None
    accepted_at: str | None = None
    event_action: str | None = None
    key_numbers: list[str] = Field(default_factory=list)
    regulatory_phrase: str | None = None
    insider_person: str | None = None
    insider_role: str | None = None
    insider_quantity: str | None = None
    insider_price: str | None = None
    trade_date: str | None = None
    primary_source_url: str | None = None
    secondary_source_url: str | None = None
    source_note: str | None = None
    confidence: float = 0.5
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_fields(self) -> "RawFinding":
        self.source_family = _strip_or_none(self.source_family) or "-"
        self.source_name = _strip_or_none(self.source_name) or "-"
        self.source_group = _strip_or_none(self.source_group) or "regulator_disclosure"
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
        self.aliases = _normalize_str_list(self.aliases)
        self.sponsor = _strip_or_none(self.sponsor)
        self.target_moa = _strip_or_none(self.target_moa)
        self.indication = _strip_or_none(self.indication)
        self.region = _strip_or_none(self.region)
        self.stage_status = _strip_or_none(self.stage_status)
        self.phase = _strip_or_none(self.phase)
        self.recruitment_status = _strip_or_none(self.recruitment_status)
        self.enrollment = _strip_or_none(self.enrollment)
        self.primary_completion_date = _strip_or_none(self.primary_completion_date)
        self.last_update_posted = _strip_or_none(self.last_update_posted)
        self.site_countries = _normalize_str_list(self.site_countries)
        self.changed_fields = _normalize_str_list(self.changed_fields)
        self.regulator = _strip_or_none(self.regulator)
        self.exchange = _strip_or_none(self.exchange)
        self.filed_at = _strip_or_none(self.filed_at)
        self.accepted_at = _strip_or_none(self.accepted_at)
        self.event_action = _strip_or_none(self.event_action)
        self.key_numbers = _normalize_str_list(self.key_numbers)
        self.regulatory_phrase = _strip_or_none(self.regulatory_phrase)
        self.insider_person = _strip_or_none(self.insider_person)
        self.insider_role = _strip_or_none(self.insider_role)
        self.insider_quantity = _strip_or_none(self.insider_quantity)
        self.insider_price = _strip_or_none(self.insider_price)
        self.trade_date = _strip_or_none(self.trade_date)
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


class SearchGapTarget(BaseModel):
    model_config = ConfigDict(extra="ignore")

    priority: str = "P1"
    candidate_id: str | None = None
    finding_identity: str | None = None
    gap_type: str
    field_targets: list[str] = Field(default_factory=list)
    preferred_queries: list[str] = Field(default_factory=list)
    preferred_source_types: list[str] = Field(default_factory=list)
    preferred_domains: list[str] = Field(default_factory=list)
    entity: str | None = None
    asset: str | None = None
    indication: str | None = None
    region: str | None = None

    @model_validator(mode="after")
    def _normalize_fields(self) -> "SearchGapTarget":
        self.priority = _strip_or_none(self.priority) or "P1"
        self.candidate_id = _strip_or_none(self.candidate_id)
        self.finding_identity = _strip_or_none(self.finding_identity)
        self.gap_type = _strip_or_none(self.gap_type) or "missing_structured_field"
        self.field_targets = _normalize_str_list(self.field_targets)
        self.preferred_queries = _normalize_str_list(self.preferred_queries)
        self.preferred_source_types = _normalize_str_list(self.preferred_source_types)
        self.preferred_domains = _normalize_str_list(self.preferred_domains)
        self.entity = _strip_or_none(self.entity)
        self.asset = _strip_or_none(self.asset)
        self.indication = _strip_or_none(self.indication)
        self.region = _strip_or_none(self.region)
        return self


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
    source_group: str | None = None
    source_tier: str | None = None
    primary_source_url: str | None = None
    secondary_source_url: str | None = None
    document_type: str | None = None
    document_id: str | None = None
    filing_type: str | None = None
    trial_id: str | None = None
    asset: str | None = None
    aliases: list[str] = Field(default_factory=list)
    sponsor: str | None = None
    target_moa: str | None = None
    indication: str | None = None
    region: str | None = None
    stage_status: str | None = None
    phase: str | None = None
    recruitment_status: str | None = None
    enrollment: str | None = None
    primary_completion_date: str | None = None
    last_update_posted: str | None = None
    site_countries: list[str] = Field(default_factory=list)
    changed_fields: list[str] = Field(default_factory=list)
    regulator: str | None = None
    exchange: str | None = None
    filed_at: str | None = None
    accepted_at: str | None = None
    event_action: str | None = None
    key_numbers: list[str] = Field(default_factory=list)
    regulatory_phrase: str | None = None
    insider_person: str | None = None
    insider_role: str | None = None
    insider_quantity: str | None = None
    insider_price: str | None = None
    trade_date: str | None = None
    discovered_at_kst: str | None = None
    reason_unverified: str | None = None
    missing_verification_target: str | None = None
    suggested_official_followup_queries: list[str] = Field(default_factory=list)
    likely_category: str | None = None
    related_asset: str | None = None
    related_indication: str | None = None
    field_provenance: dict[str, list["FieldProvenance"]] = Field(default_factory=dict)
    source_note: str | None = None
    confidence: float | None = None

    @model_validator(mode="after")
    def _normalize_fields(self) -> "StageFinding":
        self.candidate_id = _strip_or_none(self.candidate_id)
        self.entity = _strip_or_none(self.entity) or "-"
        self.entity_type = _strip_or_none(self.entity_type) or "company"
        self.category = _strip_or_none(self.category) or "general_update"
        self.title = _strip_or_none(self.title) or "-"
        self.summary = _strip_or_none(self.summary) or ""
        self.published_at_kst = _strip_or_none(self.published_at_kst)
        self.updated_at_kst = _strip_or_none(self.updated_at_kst)
        self.source_family = _strip_or_none(self.source_family)
        self.source_name = _strip_or_none(self.source_name)
        self.source_group = _strip_or_none(self.source_group)
        self.source_tier = _strip_or_none(self.source_tier)
        self.primary_source_url = _strip_or_none(self.primary_source_url)
        self.secondary_source_url = _strip_or_none(self.secondary_source_url)
        self.document_type = _strip_or_none(self.document_type)
        self.document_id = _strip_or_none(self.document_id)
        self.filing_type = _strip_or_none(self.filing_type)
        self.trial_id = _strip_or_none(self.trial_id)
        self.asset = _strip_or_none(self.asset)
        self.aliases = _normalize_str_list(self.aliases)
        self.sponsor = _strip_or_none(self.sponsor)
        self.target_moa = _strip_or_none(self.target_moa)
        self.indication = _strip_or_none(self.indication)
        self.region = _strip_or_none(self.region)
        self.stage_status = _strip_or_none(self.stage_status)
        self.phase = _strip_or_none(self.phase)
        self.recruitment_status = _strip_or_none(self.recruitment_status)
        self.enrollment = _strip_or_none(self.enrollment)
        self.primary_completion_date = _strip_or_none(self.primary_completion_date)
        self.last_update_posted = _strip_or_none(self.last_update_posted)
        self.site_countries = _normalize_str_list(self.site_countries)
        self.changed_fields = _normalize_str_list(self.changed_fields)
        self.regulator = _strip_or_none(self.regulator)
        self.exchange = _strip_or_none(self.exchange)
        self.filed_at = _strip_or_none(self.filed_at)
        self.accepted_at = _strip_or_none(self.accepted_at)
        self.event_action = _strip_or_none(self.event_action)
        self.key_numbers = _normalize_str_list(self.key_numbers)
        self.regulatory_phrase = _strip_or_none(self.regulatory_phrase)
        self.insider_person = _strip_or_none(self.insider_person)
        self.insider_role = _strip_or_none(self.insider_role)
        self.insider_quantity = _strip_or_none(self.insider_quantity)
        self.insider_price = _strip_or_none(self.insider_price)
        self.trade_date = _strip_or_none(self.trade_date)
        self.discovered_at_kst = _strip_or_none(self.discovered_at_kst)
        self.reason_unverified = _strip_or_none(self.reason_unverified)
        self.missing_verification_target = _strip_or_none(self.missing_verification_target)
        self.suggested_official_followup_queries = _normalize_str_list(self.suggested_official_followup_queries)
        self.likely_category = _strip_or_none(self.likely_category)
        self.related_asset = _strip_or_none(self.related_asset)
        self.related_indication = _strip_or_none(self.related_indication)
        if not isinstance(self.field_provenance, dict):
            self.field_provenance = {}
        else:
            normalized_provenance: dict[str, list[FieldProvenance]] = {}
            for field_name, values in self.field_provenance.items():
                key = _strip_or_none(field_name)
                if not key:
                    continue
                normalized_values: list[FieldProvenance] = []
                for value in values or []:
                    if isinstance(value, FieldProvenance):
                        normalized_values.append(value)
                    elif isinstance(value, dict):
                        normalized_values.append(FieldProvenance.model_validate(value))
                normalized_provenance[key] = normalized_values
            self.field_provenance = normalized_provenance
        self.source_note = _strip_or_none(self.source_note)
        return self


class CompetitorMapEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    competitor: str
    asset: str | None = None
    aliases: list[str] = Field(default_factory=list)
    target_moa: str | None = None
    indication: str | None = None
    stage_status: str | None = None
    region: str | None = None
    primary_source_url: str | None = None
    source_label: str | None = None
    source_type: str | None = None
    last_verified_at_kst: str | None = None
    provenance_score: float | None = None
    layer: str | None = None
    relevance: str = ""
    evidence_titles: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_fields(self) -> "CompetitorMapEntry":
        self.competitor = _strip_or_none(self.competitor) or "-"
        self.asset = _strip_or_none(self.asset)
        self.aliases = _normalize_str_list(self.aliases)
        self.target_moa = _strip_or_none(self.target_moa)
        self.indication = _strip_or_none(self.indication)
        self.stage_status = _strip_or_none(self.stage_status)
        self.region = _strip_or_none(self.region)
        self.primary_source_url = _strip_or_none(self.primary_source_url)
        self.source_label = _strip_or_none(self.source_label)
        self.source_type = _strip_or_none(self.source_type)
        self.last_verified_at_kst = _strip_or_none(self.last_verified_at_kst)
        self.provenance_score = None if self.provenance_score is None else max(0.0, min(1.0, float(self.provenance_score)))
        self.layer = _strip_or_none(self.layer)
        self.relevance = _strip_or_none(self.relevance) or ""
        self.evidence_titles = _normalize_str_list(self.evidence_titles)
        return self


class OfficialPageItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_name: str
    source_group: str
    page_name: str
    page_url: str
    item_title: str
    item_url: str | None = None
    item_type: str = "page_update"
    entity: str
    related_company: str | None = None
    asset: str | None = None
    indication: str | None = None
    region: str | None = None
    stage_status: str | None = None
    document_id: str | None = None
    document_type: str | None = None
    filing_type: str | None = None
    trial_id: str | None = None
    sponsor: str | None = None
    target_moa: str | None = None
    phase: str | None = None
    recruitment_status: str | None = None
    enrollment: str | None = None
    primary_completion_date: str | None = None
    last_update_posted: str | None = None
    site_countries: list[str] = Field(default_factory=list)
    changed_fields: list[str] = Field(default_factory=list)
    regulator: str | None = None
    exchange: str | None = None
    filed_at: str | None = None
    accepted_at: str | None = None
    event_action: str | None = None
    key_numbers: list[str] = Field(default_factory=list)
    regulatory_phrase: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    scheduled_for: datetime | None = None
    published_at_kst: str | None = None
    updated_at_kst: str | None = None
    scheduled_for_kst: str | None = None
    event_status: str | None = None
    access_restriction: str | None = None
    login_wall: bool = False
    robots_blocked: bool = False
    detection_method: str | None = None
    content_fingerprint: str | None = None
    item_identity_key: str | None = None
    source_specific_identity_json: dict[str, Any] = Field(default_factory=dict)
    freshness_state: str | None = None
    first_seen_at_kst: str | None = None
    last_seen_at_kst: str | None = None
    raw_snapshot: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_fields(self) -> "OfficialPageItem":
        self.source_name = _strip_or_none(self.source_name) or "-"
        self.source_group = _strip_or_none(self.source_group) or "discovery_only"
        self.page_name = _strip_or_none(self.page_name) or self.source_name
        self.page_url = _strip_or_none(self.page_url) or "-"
        self.item_title = _strip_or_none(self.item_title) or "-"
        self.item_url = _strip_or_none(self.item_url)
        self.item_type = _strip_or_none(self.item_type) or "page_update"
        self.entity = _strip_or_none(self.entity) or "-"
        self.related_company = _strip_or_none(self.related_company)
        self.asset = _strip_or_none(self.asset)
        self.indication = _strip_or_none(self.indication)
        self.region = _strip_or_none(self.region)
        self.stage_status = _strip_or_none(self.stage_status)
        self.document_id = _strip_or_none(self.document_id)
        self.document_type = _strip_or_none(self.document_type)
        self.filing_type = _strip_or_none(self.filing_type)
        self.trial_id = _strip_or_none(self.trial_id)
        self.sponsor = _strip_or_none(self.sponsor)
        self.target_moa = _strip_or_none(self.target_moa)
        self.phase = _strip_or_none(self.phase)
        self.recruitment_status = _strip_or_none(self.recruitment_status)
        self.enrollment = _strip_or_none(self.enrollment)
        self.primary_completion_date = _strip_or_none(self.primary_completion_date)
        self.last_update_posted = _strip_or_none(self.last_update_posted)
        self.site_countries = _normalize_str_list(self.site_countries)
        self.changed_fields = _normalize_str_list(self.changed_fields)
        self.regulator = _strip_or_none(self.regulator)
        self.exchange = _strip_or_none(self.exchange)
        self.filed_at = _strip_or_none(self.filed_at)
        self.accepted_at = _strip_or_none(self.accepted_at)
        self.event_action = _strip_or_none(self.event_action)
        self.key_numbers = _normalize_str_list(self.key_numbers)
        self.regulatory_phrase = _strip_or_none(self.regulatory_phrase)
        self.published_at_kst = _strip_or_none(self.published_at_kst) or _format_kst(self.published_at)
        self.updated_at_kst = _strip_or_none(self.updated_at_kst) or _format_kst(self.updated_at)
        self.scheduled_for_kst = _strip_or_none(self.scheduled_for_kst) or _format_kst(self.scheduled_for)
        self.event_status = _strip_or_none(self.event_status)
        self.access_restriction = _strip_or_none(self.access_restriction)
        self.login_wall = bool(self.login_wall)
        self.robots_blocked = bool(self.robots_blocked)
        self.detection_method = _strip_or_none(self.detection_method)
        self.content_fingerprint = _strip_or_none(self.content_fingerprint)
        self.item_identity_key = _strip_or_none(self.item_identity_key)
        if not isinstance(self.source_specific_identity_json, dict):
            self.source_specific_identity_json = {}
        self.freshness_state = _strip_or_none(self.freshness_state)
        self.first_seen_at_kst = _strip_or_none(self.first_seen_at_kst)
        self.last_seen_at_kst = _strip_or_none(self.last_seen_at_kst)
        if not isinstance(self.raw_snapshot, dict):
            self.raw_snapshot = {}
        return self


class GeneratedKnownEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: str
    entity: str
    category: str
    scheduled_for_kst: str
    fact: str
    basis: str
    primary_source: str
    status_note: str
    event_source_type: str = "generated_official"
    stale: bool = False
    freshness_status: str = "upcoming"
    event_identity_key: str | None = None
    source_name: str | None = None
    source_url: str | None = None

    @model_validator(mode="after")
    def _normalize_fields(self) -> "GeneratedKnownEvent":
        self.event_id = _strip_or_none(self.event_id) or "-"
        self.entity = _strip_or_none(self.entity) or "-"
        self.category = _strip_or_none(self.category) or "investor_event"
        self.scheduled_for_kst = _strip_or_none(self.scheduled_for_kst) or "-"
        self.fact = _strip_or_none(self.fact) or "-"
        self.basis = _strip_or_none(self.basis) or "-"
        self.primary_source = _strip_or_none(self.primary_source) or "-"
        self.status_note = _strip_or_none(self.status_note) or "-"
        self.event_source_type = _strip_or_none(self.event_source_type) or "generated_official"
        self.stale = bool(self.stale)
        self.freshness_status = _strip_or_none(self.freshness_status) or ("stale" if self.stale else "upcoming")
        self.event_identity_key = _strip_or_none(self.event_identity_key)
        self.source_name = _strip_or_none(self.source_name)
        self.source_url = _strip_or_none(self.source_url)
        return self


class CheckedSourceLogEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_family: str
    source_name: str
    source_group: str | None = None
    status: str
    checked_at_kst: str
    note: str = ""
    endpoint: str | None = None
    http_status: int | None = None
    latest_item_title: str | None = None
    latest_item_url: str | None = None
    access_restriction: str | None = None
    discovery_only: bool = False

    @model_validator(mode="after")
    def _normalize_fields(self) -> "CheckedSourceLogEntry":
        self.source_family = _strip_or_none(self.source_family) or "-"
        self.source_name = _strip_or_none(self.source_name) or "-"
        self.source_group = _strip_or_none(self.source_group)
        self.status = _strip_or_none(self.status) or "-"
        self.checked_at_kst = _strip_or_none(self.checked_at_kst) or "-"
        self.note = _strip_or_none(self.note) or ""
        self.endpoint = _strip_or_none(self.endpoint)
        self.latest_item_title = _strip_or_none(self.latest_item_title)
        self.latest_item_url = _strip_or_none(self.latest_item_url)
        self.access_restriction = _strip_or_none(self.access_restriction)
        self.discovery_only = bool(self.discovery_only)
        return self


class CoverageGap(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_family: str
    source_name: str
    source_group: str | None = None
    gap_type: str
    detail: str
    severity: str = "medium"
    endpoint: str | None = None
    http_status: int | None = None
    indication: str | None = None
    region: str | None = None
    discovery_only: bool = False

    @model_validator(mode="after")
    def _normalize_fields(self) -> "CoverageGap":
        self.source_family = _strip_or_none(self.source_family) or "-"
        self.source_name = _strip_or_none(self.source_name) or "-"
        self.source_group = _strip_or_none(self.source_group)
        self.gap_type = _strip_or_none(self.gap_type) or "-"
        self.detail = _strip_or_none(self.detail) or "-"
        self.severity = _strip_or_none(self.severity) or "medium"
        self.endpoint = _strip_or_none(self.endpoint)
        self.indication = _strip_or_none(self.indication)
        self.region = _strip_or_none(self.region)
        self.discovery_only = bool(self.discovery_only)
        return self


class OmissionAuditEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str
    detail: str
    axis: str = "general"
    status: str = "open"
    source_group: str | None = None
    indication: str | None = None
    region: str | None = None

    @model_validator(mode="after")
    def _normalize_fields(self) -> "OmissionAuditEntry":
        self.topic = _strip_or_none(self.topic) or "-"
        self.detail = _strip_or_none(self.detail) or "-"
        self.axis = _strip_or_none(self.axis) or "general"
        self.status = _strip_or_none(self.status) or "open"
        self.source_group = _strip_or_none(self.source_group)
        self.indication = _strip_or_none(self.indication)
        self.region = _strip_or_none(self.region)
        return self


class FieldProvenance(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field_name: str
    field_value: str | None = None
    action_type: str
    source_type: str
    source_name: str
    source_url: str | None = None
    evidence_id: str | None = None
    provenance_strength: str = "unknown"
    note: str = ""

    @model_validator(mode="after")
    def _normalize_fields(self) -> "FieldProvenance":
        self.field_name = _strip_or_none(self.field_name) or "-"
        self.field_value = _strip_or_none(self.field_value)
        self.action_type = _strip_or_none(self.action_type) or "filled_blank"
        self.source_type = _strip_or_none(self.source_type) or "official"
        self.source_name = _strip_or_none(self.source_name) or "-"
        self.source_url = _strip_or_none(self.source_url)
        self.evidence_id = _strip_or_none(self.evidence_id)
        self.provenance_strength = _strip_or_none(self.provenance_strength) or "unknown"
        self.note = _strip_or_none(self.note) or ""
        return self


class SearchEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    evidence_id: str
    topic: str
    source_type: str
    source_name: str
    source_url: str
    title: str
    published_at_kst: str | None = None
    updated_at_kst: str | None = None
    confidence: float = 0.5
    excerpt: str = ""
    evidence_kind: str = "field_confirmation"
    confirms_fields: list[str] = Field(default_factory=list)
    entity: str | None = None
    asset: str | None = None
    indication: str | None = None
    region: str | None = None

    @model_validator(mode="after")
    def _normalize_fields(self) -> "SearchEvidence":
        self.evidence_id = _strip_or_none(self.evidence_id) or "-"
        self.topic = _strip_or_none(self.topic) or "-"
        self.source_type = _strip_or_none(self.source_type) or "official"
        self.source_name = _strip_or_none(self.source_name) or "-"
        self.source_url = _strip_or_none(self.source_url) or "-"
        self.title = _strip_or_none(self.title) or "-"
        self.published_at_kst = _strip_or_none(self.published_at_kst)
        self.updated_at_kst = _strip_or_none(self.updated_at_kst)
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.excerpt = _strip_or_none(self.excerpt) or ""
        self.evidence_kind = _strip_or_none(self.evidence_kind) or "field_confirmation"
        self.confirms_fields = _normalize_str_list(self.confirms_fields)
        self.entity = _strip_or_none(self.entity)
        self.asset = _strip_or_none(self.asset)
        self.indication = _strip_or_none(self.indication)
        self.region = _strip_or_none(self.region)
        return self


class Stage2Backfill(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidate_id: str | None = None
    finding_identity: str | None = None
    filled_fields: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_fields(self) -> "Stage2Backfill":
        self.candidate_id = _strip_or_none(self.candidate_id)
        self.finding_identity = _strip_or_none(self.finding_identity)
        if not isinstance(self.filled_fields, dict):
            self.filled_fields = {}
        self.evidence_ids = _normalize_str_list(self.evidence_ids)
        return self


class Stage2DiscoveredFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: str
    entity: str
    title: str
    source_type: str
    source_name: str
    source_url: str
    published_at_kst: str | None = None
    updated_at_kst: str | None = None
    discovered_at_kst: str | None = None
    structured_fields: dict[str, Any] = Field(default_factory=dict)
    why_discovered: str = ""
    reason_unverified: str | None = None
    missing_verification_target: str | None = None
    suggested_official_followup_queries: list[str] = Field(default_factory=list)
    likely_category: str | None = None
    related_asset: str | None = None
    related_indication: str | None = None

    @model_validator(mode="after")
    def _normalize_fields(self) -> "Stage2DiscoveredFinding":
        self.category = _strip_or_none(self.category) or "unverified_lead"
        self.entity = _strip_or_none(self.entity) or "-"
        self.title = _strip_or_none(self.title) or "-"
        self.source_type = _strip_or_none(self.source_type) or "official"
        self.source_name = _strip_or_none(self.source_name) or "-"
        self.source_url = _strip_or_none(self.source_url) or "-"
        self.published_at_kst = _strip_or_none(self.published_at_kst)
        self.updated_at_kst = _strip_or_none(self.updated_at_kst)
        self.discovered_at_kst = _strip_or_none(self.discovered_at_kst)
        if not isinstance(self.structured_fields, dict):
            self.structured_fields = {}
        self.why_discovered = _strip_or_none(self.why_discovered) or ""
        self.reason_unverified = _strip_or_none(self.reason_unverified)
        self.missing_verification_target = _strip_or_none(self.missing_verification_target)
        self.suggested_official_followup_queries = _normalize_str_list(self.suggested_official_followup_queries)
        self.likely_category = _strip_or_none(self.likely_category)
        self.related_asset = _strip_or_none(self.related_asset)
        self.related_indication = _strip_or_none(self.related_indication)
        return self


class Stage2VerificationOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    evidence_catalog: list[SearchEvidence] = Field(default_factory=list)
    backfills: list[Stage2Backfill] = Field(default_factory=list)
    discovered_confirmed_findings: list[Stage2DiscoveredFinding] = Field(default_factory=list)
    discovered_unverified_leads: list[Stage2DiscoveredFinding] = Field(default_factory=list)
    updated_source_logs: list[CheckedSourceLogEntry] = Field(default_factory=list)
    updated_coverage_gaps: list[CoverageGap] = Field(default_factory=list)
    updated_omission_audit: list[OmissionAuditEntry] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)


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
    search_gap_targets: list[SearchGapTarget] = Field(default_factory=list)


class HanallStage1OverlayOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company_direct_confirmed_ids: list[str] = Field(default_factory=list)
    competitor_relevant_confirmed_ids: list[str] = Field(default_factory=list)
    unverified_lead_ids: list[str] = Field(default_factory=list)
    coverage: CoverageOverlay = Field(default_factory=CoverageOverlay)
    competitor_map_snapshot: list[CompetitorMapEntry] = Field(default_factory=list)
    omission_audit: list[OmissionAuditEntry] = Field(default_factory=list)
    search_tasks: list[SearchTask] = Field(default_factory=list)
    search_gap_targets: list[SearchGapTarget] = Field(default_factory=list)


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
    page_items: list[OfficialPageItem] = Field(default_factory=list)
    generated_known_events: list[GeneratedKnownEvent] = Field(default_factory=list)
    checked_source_log: list[CheckedSourceLogEntry] = Field(default_factory=list)
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)
