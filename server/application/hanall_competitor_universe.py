from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from server.application.hanall_competitor_sync import (
    build_auto_competitor_candidates,
    load_persisted_auto_candidates,
    merge_competitor_entries,
    persist_auto_competitor_candidates,
)
from server.core.hanall_news_models import CheckedSourceLogEntry, CompetitorMapEntry, RawFinding
from server.settings import HANALL_COMPETITORS_PATH
from server.utils import now_kst

MONITORED_SOURCE_GROUPS = (
    "company_official",
    "regulator_disclosure",
    "trial_registry",
    "competitor_official",
    "discovery_only",
)
MONITORED_INDICATIONS = ("MG", "TED", "CIDP", "GD", "RA", "SjD", "CLE", "DED")
MONITORED_REGIONS = ("US", "EU", "JP", "CN", "KR")

_INDICATION_ALIASES: dict[str, tuple[str, ...]] = {
    "MG": ("mg", "gmg", "generalized myasthenia gravis", "myasthenia gravis"),
    "TED": ("ted", "thyroid eye", "thyroid eye disease"),
    "CIDP": ("cidp",),
    "GD": ("gd", "graves", "graves disease"),
    "RA": ("ra", "d2t ra", "rheumatoid arthritis", "rheumatoid"),
    "SjD": ("sjd", "sjogren", "sjogren's", "sjögren"),
    "CLE": ("cle", "cutaneous lupus"),
    "DED": ("ded", "dry eye", "dry eye disease"),
}
_REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "US": ("us", "usa", "united states", "nasdaq", "sec", "fda"),
    "EU": ("eu", "europe", "ema", "ctis"),
    "JP": ("jp", "japan", "pmda", "mhlw", "jrct"),
    "CN": ("cn", "china", "nmpa", "chictr"),
    "KR": ("kr", "korea", "krx", "kind", "mfds", "dart", "opendart"),
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for text in (_normalize_text(item) for item in value) if text]
    text = _normalize_text(value)
    return [text] if text else []


def _contains_alias(text: str, alias: str) -> bool:
    normalized_alias = _normalize_text(alias).lower()
    if not normalized_alias:
        return False
    if re.fullmatch(r"[a-z0-9]+", normalized_alias):
        return re.search(rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])", text) is not None
    return normalized_alias in text


def normalize_monitor_indication(value: str | None) -> str | None:
    lowered = _normalize_text(value).lower()
    if not lowered:
        return None
    for label, aliases in _INDICATION_ALIASES.items():
        if any(_contains_alias(lowered, alias) for alias in aliases):
            return label
    return None


def normalize_monitor_region(value: str | None) -> str | None:
    lowered = _normalize_text(value).lower()
    if not lowered:
        return None
    for label, aliases in _REGION_ALIASES.items():
        if any(_contains_alias(lowered, alias) for alias in aliases):
            return label
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    return loaded if isinstance(loaded, dict) else {}


def load_hanall_competitor_universe(path: Path = HANALL_COMPETITORS_PATH) -> list[CompetitorMapEntry]:
    payload = _load_yaml(path)
    universe = payload.get("universe", [])
    if not isinstance(universe, list):
        return []
    entries: list[CompetitorMapEntry] = []
    for item in universe:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        normalized["aliases"] = _normalize_list(item.get("aliases"))
        entries.append(CompetitorMapEntry.model_validate(normalized))
    return entries


def _finding_tokens(finding: RawFinding) -> str:
    bits = [
        finding.entity,
        finding.title,
        finding.summary,
        finding.asset,
        finding.indication,
        finding.target_moa,
        " ".join(finding.aliases or []),
    ]
    return " ".join(bit for bit in bits if bit).lower()


def _entry_tokens(entry: CompetitorMapEntry) -> list[str]:
    return [
        token
        for token in [
            entry.competitor,
            entry.asset,
            entry.target_moa,
            entry.indication,
            *entry.aliases,
        ]
        if _normalize_text(token)
    ]


def _finding_matches_entry(finding: RawFinding, entry: CompetitorMapEntry) -> bool:
    if not _normalize_text(finding.entity) and not _normalize_text(finding.asset):
        return False
    haystack = _finding_tokens(finding)
    indication = normalize_monitor_indication(finding.indication)
    entry_indication = normalize_monitor_indication(entry.indication)
    if indication and entry_indication and indication != entry_indication:
        return False
    return any(token.lower() in haystack for token in _entry_tokens(entry))


def _page_item_matches_entry(page_item_title: str, page_item_entity: str | None, page_item_asset: str | None, page_item_indication: str | None, entry: CompetitorMapEntry) -> bool:
    combined = " ".join(
        bit for bit in (page_item_entity, page_item_title, page_item_asset, page_item_indication, entry.asset, entry.competitor) if _normalize_text(bit)
    ).lower()
    entry_indication = normalize_monitor_indication(entry.indication)
    page_indication = normalize_monitor_indication(page_item_indication)
    if entry_indication and page_indication and entry_indication != page_indication:
        return False
    return any(token.lower() in combined for token in _entry_tokens(entry))


def _entry_checked(entry: CompetitorMapEntry, checked_source_log: list[CheckedSourceLogEntry]) -> bool:
    source_label = _normalize_text(entry.source_label).lower()
    primary_source_url = _normalize_text(entry.primary_source_url)
    competitor_name = _normalize_text(entry.competitor).lower()
    for source in checked_source_log:
        source_name = _normalize_text(source.source_name).lower()
        latest_url = _normalize_text(source.latest_item_url)
        endpoint = _normalize_text(source.endpoint)
        latest_title = _normalize_text(source.latest_item_title).lower()
        if source_label and source_label in latest_title:
            return True
        if competitor_name and competitor_name in " ".join([source_name, latest_title]).lower():
            return True
        if primary_source_url and primary_source_url in {latest_url, endpoint}:
            return True
    return False


def build_competitor_universe_snapshot(
    *,
    findings: list[RawFinding],
    checked_source_log: list[CheckedSourceLogEntry],
    page_items: list[Any] | None = None,
    current_now: Any | None = None,
) -> list[CompetitorMapEntry]:
    observed_at = current_now or now_kst()
    runtime_auto_entries = build_auto_competitor_candidates(
        findings=findings,
        page_items=list(page_items or []),
        current_now=observed_at,
    )
    if runtime_auto_entries:
        persist_auto_competitor_candidates(
            runtime_auto_entries,
            observed_at_kst=observed_at.strftime("%Y-%m-%d %H:%M KST"),
        )
    merged_entries = merge_competitor_entries(
        curated_entries=load_hanall_competitor_universe(),
        auto_entries=[*load_persisted_auto_candidates(), *runtime_auto_entries],
    )
    snapshot: list[CompetitorMapEntry] = []
    for entry in merged_entries:
        evidence_titles = _normalize_list(
            [
                *entry.evidence_titles,
                *[
            finding.title
            for finding in findings
            if _finding_matches_entry(finding, entry)
                ],
                *[
                    _normalize_text(getattr(page_item, "item_title", ""))
                    for page_item in list(page_items or [])
                    if _page_item_matches_entry(
                        _normalize_text(getattr(page_item, "item_title", "")),
                        _normalize_text(getattr(page_item, "entity", "")),
                        _normalize_text(getattr(page_item, "asset", "")),
                        _normalize_text(getattr(page_item, "indication", "")),
                        entry,
                    )
                ],
            ]
        )[:3]
        stage_status = entry.stage_status
        if evidence_titles and not _normalize_text(stage_status):
            stage_status = "monitored update observed"
        if _entry_checked(entry, checked_source_log) and _normalize_text(stage_status) and "official page checked" not in stage_status.lower():
            stage_status = f"{stage_status} | official page checked"
        snapshot.append(
            CompetitorMapEntry.model_validate(
                {
                    **entry.model_dump(mode="json"),
                    "evidence_titles": evidence_titles,
                    "stage_status": stage_status,
                }
            )
        )
    return snapshot
