from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from server.config import get_hanall_sources_config
from server.core.hanall_news_models import HanallStage1StructuredOutput, SearchGapTarget, StageFinding
from server.infra.sqlite_store import (
    get_hanall_run_snapshot,
    list_hanall_run_snapshots,
    list_stage2_finding_provenance,
    list_stage2_search_evidence,
    list_stage2_verification_runs,
)
from server.utils import now_kst, smart_truncate

REUSE_TTL_HOURS = {
    "official": 72,
    "regulator": 72,
    "registry": 72,
    "trusted_press": 36,
    "discovery_only": 12,
}

SOURCE_STRENGTH_SCORES = {
    "trial_registry": 34,
    "regulator_disclosure": 32,
    "company_official": 28,
    "competitor_official": 22,
    "discovery_only": 2,
}

PROVENANCE_STRENGTH_SCORES = {
    "stage1_official_structured": 18,
    "stage2_official_search": 14,
    "stage1_metadata_only": 6,
    "stage2_trusted_newswire": 4,
    "discovery_only": 0,
    "unknown": 0,
}

EVENT_ACTION_PRIORITY = {
    "approved": 24,
    "approval": 24,
    "accepted": 22,
    "crl": 22,
    "complete_response": 22,
    "label_change": 18,
    "label update": 18,
    "safety": 18,
    "phase_3": 16,
    "pivotal": 16,
    "investor_event": 8,
    "presentation": 5,
    "earnings": 5,
    "career_posting": -18,
}

NOISE_KEYWORDS = {
    "career": -18,
    "careers": -18,
    "hiring": -14,
    "presentation": -4,
    "webcast": -2,
    "deck": -5,
}

COMPETITOR_LAYER_SCORES = {
    "direct_class": 8,
    "indication": 6,
    "standard_of_care": 3,
    "regional": 2,
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_key(value: Any) -> str:
    return _normalize_text(value).lower()


def _parse_kst_text(value: str | None) -> datetime | None:
    normalized = _normalize_text(value).removesuffix(" KST").strip()
    if not normalized:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=now_kst().tzinfo)
        except ValueError:
            continue
    return None


def _normalize_source_type(value: str | None) -> str:
    normalized = _normalize_key(value)
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


def _stage_finding_identity_dict(payload: dict[str, Any]) -> str:
    return "|".join(
        [
            _normalize_text(payload.get("candidate_id")) or "-",
            _normalize_text(payload.get("source_name")) or "-",
            _normalize_text(payload.get("document_id") or payload.get("trial_id")) or "-",
            _normalize_text(payload.get("primary_source_url")) or "-",
            _normalize_text(payload.get("title")) or "-",
        ]
    )


def build_stage_finding_identity(item: StageFinding) -> str:
    return _stage_finding_identity_dict(item.model_dump(mode="json"))


def _recent_evidence_timestamp(row: dict[str, Any]) -> datetime | None:
    return (
        _parse_kst_text(_normalize_text(row.get("updated_at_kst")) or None)
        or _parse_kst_text(_normalize_text(row.get("published_at_kst")) or None)
        or _parse_kst_text(_normalize_text(row.get("inserted_at_kst")) or None)
    )


def _evidence_reuse_ttl_hours(source_type: str) -> int:
    return REUSE_TTL_HOURS.get(_normalize_source_type(source_type), 24)


def _gap_target_match_score(gap_target: SearchGapTarget, evidence: dict[str, Any], *, current_now: datetime) -> float:
    source_type = _normalize_source_type(evidence.get("source_type"))
    recent_at = _recent_evidence_timestamp(evidence)
    if recent_at is None:
        return -1.0
    ttl_hours = _evidence_reuse_ttl_hours(source_type)
    if recent_at < current_now - timedelta(hours=ttl_hours):
        return -1.0

    score = 0.0
    score += {"official": 24.0, "regulator": 22.0, "registry": 22.0, "trusted_press": 10.0, "discovery_only": 2.0}.get(
        source_type,
        0.0,
    )

    field_targets = {_normalize_key(value) for value in gap_target.field_targets}
    confirms_fields = {_normalize_key(value) for value in evidence.get("confirms_fields") or []}
    score += 8.0 * len(field_targets & confirms_fields)

    for field_name, weight in (("entity", 10.0), ("asset", 12.0), ("indication", 9.0), ("region", 5.0)):
        gap_value = _normalize_key(getattr(gap_target, field_name))
        evidence_value = _normalize_key(evidence.get(field_name))
        if gap_value and evidence_value and gap_value == evidence_value:
            score += weight

    topic_blob = " ".join(
        [
            _normalize_text(evidence.get("topic")),
            _normalize_text(evidence.get("title")),
            _normalize_text(evidence.get("source_name")),
            _normalize_text(evidence.get("source_url")),
        ]
    ).lower()
    if gap_target.finding_identity and _normalize_key(gap_target.finding_identity) in topic_blob:
        score += 10.0
    if gap_target.preferred_domains:
        evidence_domain = urlparse(_normalize_text(evidence.get("source_url"))).netloc.lower()
        if evidence_domain and evidence_domain in {_normalize_key(domain) for domain in gap_target.preferred_domains}:
            score += 6.0
    return score


def select_reusable_evidence(
    *,
    gap_target: SearchGapTarget,
    evidence_rows: list[dict[str, Any]],
    current_now: datetime,
    max_items: int = 3,
) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in evidence_rows:
        score = _gap_target_match_score(gap_target, row, current_now=current_now)
        if score < 0:
            continue
        ranked.append((score, row))
    ranked.sort(
        key=lambda item: (
            -item[0],
            -(
                _recent_evidence_timestamp(item[1]).timestamp()
                if _recent_evidence_timestamp(item[1]) is not None
                else 0.0
            ),
            _normalize_text(item[1].get("evidence_id")),
        )
    )
    selected: list[dict[str, Any]] = []
    for score, row in ranked[: max(1, max_items)]:
        selected.append(
            {
                **row,
                "reuse_score": round(score, 2),
                "reused": True,
                "reuse_reason": smart_truncate(
                    ", ".join(
                        bit
                        for bit in (
                            f"source_type={_normalize_source_type(row.get('source_type'))}",
                            f"fields={','.join(row.get('confirms_fields') or []) or '-'}",
                            f"entity={row.get('entity') or '-'}",
                            f"asset={row.get('asset') or '-'}",
                            f"indication={row.get('indication') or '-'}",
                        )
                        if bit
                    ),
                    160,
                ),
            }
        )
    return selected


def build_stage2_search_memory(
    *,
    stage1_output: HanallStage1StructuredOutput,
    current_now: datetime,
    room_key: str | None = None,
    evidence_limit: int = 120,
) -> dict[str, Any]:
    recent_runs = list_stage2_verification_runs(room_key=room_key, limit=12)
    evidence_rows = list_stage2_search_evidence(room_key=room_key, limit=evidence_limit)
    return {
        "generated_at_kst": current_now.strftime("%Y-%m-%d %H:%M KST"),
        "recent_run_count": len(recent_runs),
        "recent_success_count": sum(1 for row in recent_runs if _normalize_key(row.get("stage2_status")) in {"success", "no_result"}),
        "gap_target_count": len(stage1_output.search_gap_targets),
        "evidence_catalog": evidence_rows,
        "ttl_hours_by_source_type": dict(REUSE_TTL_HOURS),
    }


def build_search_plan_from_memory(
    *,
    stage1_output: HanallStage1StructuredOutput,
    search_memory: dict[str, Any],
    current_now: datetime,
) -> dict[str, Any]:
    evidence_rows = list(search_memory.get("evidence_catalog") or [])
    plan_targets: list[dict[str, Any]] = []
    reused_ids: list[str] = []
    for gap_target in stage1_output.search_gap_targets:
        reused = select_reusable_evidence(
            gap_target=gap_target,
            evidence_rows=evidence_rows,
            current_now=current_now,
        )
        reused_ids.extend(_normalize_text(item.get("evidence_id")) for item in reused if _normalize_text(item.get("evidence_id")))
        plan_targets.append(
            {
                "priority": gap_target.priority,
                "candidate_id": gap_target.candidate_id,
                "finding_identity": gap_target.finding_identity,
                "gap_type": gap_target.gap_type,
                "field_targets": list(gap_target.field_targets),
                "entity": gap_target.entity,
                "asset": gap_target.asset,
                "indication": gap_target.indication,
                "region": gap_target.region,
                "preferred_queries": list(gap_target.preferred_queries),
                "preferred_domains": list(gap_target.preferred_domains),
                "preferred_source_types": list(gap_target.preferred_source_types),
                "reused_evidence": reused,
                "preverified": bool(reused),
                "search_required": not bool(reused),
            }
        )
    distinct_reused_ids = [item for item in dict.fromkeys(reused_ids) if item]
    return {
        "generated_at_kst": search_memory.get("generated_at_kst"),
        "recent_run_count": int(search_memory.get("recent_run_count") or 0),
        "recent_success_count": int(search_memory.get("recent_success_count") or 0),
        "reused_evidence_count": len(distinct_reused_ids),
        "reused_evidence_ids": distinct_reused_ids,
        "targets": plan_targets,
        "preverified_evidence": [item for target in plan_targets for item in target["reused_evidence"]][:12],
        "ttl_hours_by_source_type": dict(search_memory.get("ttl_hours_by_source_type") or REUSE_TTL_HOURS),
    }


def _latest_kst_for_item(item: StageFinding) -> datetime | None:
    return _parse_kst_text(item.updated_at_kst) or _parse_kst_text(item.published_at_kst) or _parse_kst_text(item.discovered_at_kst)


def _event_action_score(item: StageFinding) -> int:
    action_blob = " ".join(
        [
            _normalize_text(item.event_action),
            _normalize_text(item.filing_type),
            _normalize_text(item.regulatory_phrase),
            _normalize_text(item.title),
        ]
    ).lower()
    score = 0
    for keyword, value in EVENT_ACTION_PRIORITY.items():
        if keyword.replace("_", " ") in action_blob or keyword in action_blob:
            score = max(score, value)
    return score


def _structured_richness_score(item: StageFinding) -> int:
    fields = [
        item.phase,
        item.enrollment,
        item.primary_completion_date,
        item.last_update_posted,
        item.accepted_at,
        item.regulatory_phrase,
        item.stage_status,
        item.target_moa,
    ]
    score = sum(2 for value in fields if _normalize_text(value))
    if item.site_countries:
        score += 2
    if item.key_numbers:
        score += 2
    if item.changed_fields:
        score += min(4, len(item.changed_fields))
    return score


def _provenance_score(item: StageFinding) -> int:
    strengths = [
        PROVENANCE_STRENGTH_SCORES.get(_normalize_key(entry.provenance_strength), 0)
        for field_entries in item.field_provenance.values()
        for entry in field_entries
    ]
    return max(strengths or [0])


def _freshness_score(item: StageFinding, *, current_now: datetime) -> int:
    latest = _latest_kst_for_item(item)
    if latest is None:
        return 0
    age_hours = max(0.0, (current_now - latest).total_seconds() / 3600.0)
    if age_hours <= 2:
        return 10
    if age_hours <= 6:
        return 7
    if age_hours <= 12:
        return 4
    return 1


def _noise_penalty(item: StageFinding) -> int:
    blob = " ".join([_normalize_text(item.title), _normalize_text(item.event_action), _normalize_text(item.summary)]).lower()
    penalty = 0
    for keyword, value in NOISE_KEYWORDS.items():
        if keyword in blob:
            penalty = min(penalty, value)
    return penalty


def score_finding_importance(
    item: StageFinding,
    *,
    bucket: str,
    current_now: datetime | None = None,
) -> dict[str, Any]:
    now = current_now or now_kst()
    source_group = _normalize_text(item.source_group) or "discovery_only"
    score = SOURCE_STRENGTH_SCORES.get(source_group, 0)
    reasons: list[str] = [f"source_group={source_group}"]
    if bucket == "company_direct_confirmed":
        score += 18
        reasons.append("direct_company")
    elif bucket == "competitor_relevant_confirmed":
        score += 10
        reasons.append("competitor_relevant")
    elif bucket == "unverified_leads":
        score -= 12
        reasons.append("unverified_penalty")

    action_score = _event_action_score(item)
    if action_score:
        score += action_score
        reasons.append(f"event_action={action_score}")

    richness = _structured_richness_score(item)
    if richness:
        score += richness
        reasons.append(f"structured={richness}")

    freshness = _freshness_score(item, current_now=now)
    if freshness:
        score += freshness
        reasons.append(f"freshness={freshness}")

    provenance = _provenance_score(item)
    if provenance:
        score += provenance
        reasons.append(f"provenance={provenance}")

    layer_score = COMPETITOR_LAYER_SCORES.get(_normalize_key(getattr(item, "source_note", None)), 0)
    if layer_score:
        score += layer_score
        reasons.append(f"layer={layer_score}")

    penalty = _noise_penalty(item)
    if penalty:
        score += penalty
        reasons.append(f"noise={penalty}")

    return {
        "identity": build_stage_finding_identity(item),
        "candidate_id": item.candidate_id,
        "bucket": bucket,
        "entity": item.entity,
        "title": item.title,
        "score": score,
        "reasons": reasons,
        "source_group": source_group,
        "published_at_kst": item.published_at_kst,
        "updated_at_kst": item.updated_at_kst,
        "event_action": item.event_action,
        "asset": item.asset,
        "indication": item.indication,
        "primary_source_url": item.primary_source_url,
    }


def _ranked_summary_line(item: StageFinding, score_payload: dict[str, Any]) -> str:
    detail_bits = [item.asset, item.indication, item.phase or item.filing_type or item.event_action]
    detail_text = ", ".join(bit for bit in detail_bits if _normalize_text(bit))
    prefix = "회사" if score_payload["bucket"] == "company_direct_confirmed" else "경쟁사"
    return f"- {prefix} 상위 이슈: {item.entity} | {item.title}{f' | {detail_text}' if detail_text else ''}"


def build_ranked_issue_list(
    *,
    stage1_output: HanallStage1StructuredOutput,
    current_now: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    now = current_now or now_kst()
    ranked: list[tuple[dict[str, Any], StageFinding]] = []
    for bucket, items in (
        ("company_direct_confirmed", stage1_output.company_direct_confirmed),
        ("competitor_relevant_confirmed", stage1_output.competitor_relevant_confirmed),
        ("unverified_leads", stage1_output.unverified_leads if not stage1_output.company_direct_confirmed and not stage1_output.competitor_relevant_confirmed else []),
    ):
        for item in items:
            payload = score_finding_importance(item, bucket=bucket, current_now=now)
            payload["summary_line"] = _ranked_summary_line(item, payload)
            ranked.append((payload, item))
    ranked.sort(
        key=lambda entry: (
            -float(entry[0]["score"]),
            -(
                (_parse_kst_text(entry[0].get("updated_at_kst")) or _parse_kst_text(entry[0].get("published_at_kst")) or now)
                .timestamp()
            ),
            _normalize_text(entry[0].get("entity")),
            _normalize_text(entry[0].get("title")),
        )
    )
    resolved = [payload for payload, _ in ranked]
    return resolved[:limit] if limit is not None else resolved


def _deserialize_stage_output(payload: dict[str, Any]) -> HanallStage1StructuredOutput:
    return HanallStage1StructuredOutput.model_validate(payload or {})


def get_latest_hanall_runs(limit: int, room_key: str | None = None) -> list[dict[str, Any]]:
    runs = list_stage2_verification_runs(room_key=room_key, limit=limit)
    snapshots = {entry["trace_id"]: entry for entry in list_hanall_run_snapshots(limit=limit, room_key=room_key)}
    items: list[dict[str, Any]] = []
    for run in runs:
        snapshot = snapshots.get(run["trace_id"]) or {}
        merged_output = _deserialize_stage_output(snapshot.get("merged_stage1_output") or {})
        items.append(
            {
                **run,
                "summary_lines": list(snapshot.get("summary_lines") or []),
                "coverage_level": merged_output.coverage.level,
                "company_direct_count": len(merged_output.company_direct_confirmed),
                "competitor_count": len(merged_output.competitor_relevant_confirmed),
                "unverified_count": len(merged_output.unverified_leads),
            }
        )
    return items


def get_hanall_run_debug_bundle(trace_id: str) -> dict[str, Any]:
    run_rows = list_stage2_verification_runs(trace_id)
    snapshot = get_hanall_run_snapshot(trace_id)
    if not run_rows or snapshot is None:
        raise ValueError(f"hanall run not found: {trace_id}")
    run = run_rows[0]
    stage2_output = snapshot.get("stage2_output") or {}
    return {
        "run_metadata": run,
        "room_key": run.get("room_key"),
        "run_status": run.get("stage2_status"),
        "stage1_candidate_summary": snapshot.get("stage1_candidate_summary") or {},
        "stage1_output": snapshot.get("stage1_output") or {},
        "merged_stage1_output": snapshot.get("merged_stage1_output") or {},
        "stage2_evidence_catalog": list_stage2_search_evidence(trace_id),
        "stage2_provenance_rows": list_stage2_finding_provenance(trace_id),
        "backfills": list(stage2_output.get("backfills") or []),
        "discovered_confirmed_findings": list(stage2_output.get("discovered_confirmed_findings") or []),
        "discovered_unverified_leads": list(stage2_output.get("discovered_unverified_leads") or []),
        "coverage_deltas": {
            "updated_source_logs": list(stage2_output.get("updated_source_logs") or []),
            "updated_coverage_gaps": list(stage2_output.get("updated_coverage_gaps") or []),
            "updated_omission_audit": list(stage2_output.get("updated_omission_audit") or []),
        },
        "search_memory": snapshot.get("search_memory") or {},
        "final_ranking_inputs": list(snapshot.get("ranked_issues") or []),
        "final_summary_lines": list(snapshot.get("summary_lines") or []),
        "final_text": snapshot.get("final_text") or "",
        "debug_meta": snapshot.get("debug_meta") or {},
    }


def _finding_index(stage_output: HanallStage1StructuredOutput) -> dict[str, StageFinding]:
    indexed: dict[str, StageFinding] = {}
    for item in [*stage_output.company_direct_confirmed, *stage_output.competitor_relevant_confirmed]:
        indexed[build_stage_finding_identity(item)] = item
    return indexed


def _finding_change_fields(before: StageFinding, after: StageFinding) -> list[str]:
    candidate_fields = [
        "title",
        "summary",
        "phase",
        "recruitment_status",
        "enrollment",
        "primary_completion_date",
        "last_update_posted",
        "filing_type",
        "accepted_at",
        "filed_at",
        "event_action",
        "key_numbers",
        "regulatory_phrase",
        "stage_status",
        "target_moa",
        "region",
        "changed_fields",
    ]
    changed: list[str] = []
    for field_name in candidate_fields:
        if getattr(before, field_name, None) != getattr(after, field_name, None):
            changed.append(field_name)
    return changed


def build_hanall_run_drift_report(current_trace_id: str, previous_trace_id: str) -> dict[str, Any]:
    current_snapshot = get_hanall_run_snapshot(current_trace_id)
    previous_snapshot = get_hanall_run_snapshot(previous_trace_id)
    if current_snapshot is None or previous_snapshot is None:
        raise ValueError("hanall drift report requires two existing snapshots")

    current_output = _deserialize_stage_output(current_snapshot.get("merged_stage1_output") or {})
    previous_output = _deserialize_stage_output(previous_snapshot.get("merged_stage1_output") or {})
    current_index = _finding_index(current_output)
    previous_index = _finding_index(previous_output)

    added_ids = sorted(set(current_index) - set(previous_index))
    removed_ids = sorted(set(previous_index) - set(current_index))
    shared_ids = sorted(set(current_index) & set(previous_index))

    changed_findings: list[dict[str, Any]] = []
    for identity in shared_ids:
        before = previous_index[identity]
        after = current_index[identity]
        changed_fields = _finding_change_fields(before, after)
        if not changed_fields:
            continue
        changed_findings.append(
            {
                "identity": identity,
                "title": after.title,
                "entity": after.entity,
                "changed_fields": changed_fields,
            }
        )

    current_ranked = [entry.get("identity") for entry in current_snapshot.get("ranked_issues") or [] if _normalize_text(entry.get("identity"))]
    previous_ranked = [entry.get("identity") for entry in previous_snapshot.get("ranked_issues") or [] if _normalize_text(entry.get("identity"))]

    return {
        "current_trace_id": current_trace_id,
        "previous_trace_id": previous_trace_id,
        "added_confirmed_findings": [current_index[identity].model_dump(mode="json") for identity in added_ids],
        "removed_confirmed_findings": [previous_index[identity].model_dump(mode="json") for identity in removed_ids],
        "changed_findings": changed_findings,
        "coverage_level_change": {
            "before": previous_output.coverage.level,
            "after": current_output.coverage.level,
        },
        "source_log_change": {
            "before": len(previous_output.checked_source_log),
            "after": len(current_output.checked_source_log),
        },
        "coverage_gap_change": {
            "before": len(previous_output.coverage_gaps),
            "after": len(current_output.coverage_gaps),
        },
        "omission_audit_change": {
            "before": len(previous_output.omission_audit),
            "after": len(current_output.omission_audit),
        },
        "competitor_snapshot_change": {
            "before": len(previous_output.competitor_map_snapshot),
            "after": len(current_output.competitor_map_snapshot),
        },
        "summary_rank_order_change": {
            "before": previous_ranked[:5],
            "after": current_ranked[:5],
        },
    }


def build_latest_hanall_drift_report(room_key: str | None = None) -> dict[str, Any] | None:
    snapshots = list_hanall_run_snapshots(limit=2, room_key=room_key)
    if len(snapshots) < 2:
        return None
    return build_hanall_run_drift_report(snapshots[0]["trace_id"], snapshots[1]["trace_id"])


def get_hanall_recent_run_summary(limit: int = 10, room_key: str | None = None) -> list[dict[str, Any]]:
    return get_latest_hanall_runs(limit=limit, room_key=room_key)


def get_hanall_ops_metrics(limit: int = 20, room_key: str | None = None) -> dict[str, Any]:
    runs = list_stage2_verification_runs(room_key=room_key, limit=limit)
    trace_ids = {row["trace_id"] for row in runs}
    snapshots = {entry["trace_id"]: entry for entry in list_hanall_run_snapshots(limit=limit, room_key=room_key)}
    provenance_rows = [row for row in list_stage2_finding_provenance() if row["trace_id"] in trace_ids]

    coverage_distribution: Counter[str] = Counter()
    source_group_hits: Counter[str] = Counter()
    source_group_gaps: Counter[str] = Counter()
    stale_candidate_count = 0
    for trace_id, snapshot in snapshots.items():
        if trace_id not in trace_ids:
            continue
        stage_output = _deserialize_stage_output(snapshot.get("merged_stage1_output") or {})
        coverage_distribution[stage_output.coverage.level] += 1
        for item in [*stage_output.company_direct_confirmed, *stage_output.competitor_relevant_confirmed]:
            source_group_hits[_normalize_text(item.source_group) or "unknown"] += 1
        for gap in stage_output.coverage_gaps:
            source_group_gaps[_normalize_text(gap.source_group) or "unknown"] += 1
            if "stale" in _normalize_key(gap.gap_type) or "resurfaced" in _normalize_key(gap.gap_type):
                stale_candidate_count += 1

    status_counts = Counter(_normalize_text(row["stage2_status"]) or "unknown" for row in runs)
    conflict_counts = Counter(row["action_type"] for row in provenance_rows if row["action_type"] in {"retained_stage1", "conflict_kept_stage1"})
    success_runs = max(1, len(runs))
    total_gap_targets = sum(int(row.get("gap_target_count") or 0) for row in runs)
    total_reused = sum(int(row.get("reused_evidence_count") or 0) for row in runs)
    return {
        "recent_run_count": len(runs),
        "status_counts": dict(status_counts),
        "average_evidence_count": round(sum(int(row.get("evidence_count") or 0) for row in runs) / success_runs, 2),
        "average_backfill_count": round(sum(int(row.get("backfill_count") or 0) for row in runs) / success_runs, 2),
        "discovered_confirmed_trend": [
            {"trace_id": row["trace_id"], "value": int(row.get("discovered_confirmed_count") or 0)}
            for row in runs
        ],
        "discovered_unverified_trend": [
            {"trace_id": row["trace_id"], "value": int(row.get("discovered_unverified_count") or 0)}
            for row in runs
        ],
        "coverage_level_distribution": dict(coverage_distribution),
        "source_group_hit_rate": dict(source_group_hits),
        "source_group_gap_rate": dict(source_group_gaps),
        "search_reuse_hit_rate": round(total_reused / max(1, total_gap_targets), 3),
        "stage2_conflict_count": sum(conflict_counts.values()),
        "stage2_conflict_breakdown": dict(conflict_counts),
        "stale_candidate_count": stale_candidate_count,
    }


def _source_config_map() -> dict[str, dict[str, Any]]:
    config = get_hanall_sources_config()
    page_checks = config.get("page_checks", {}) if isinstance(config, dict) else {}
    sources = page_checks.get("sources", []) if isinstance(page_checks, dict) else []
    mapping: dict[str, dict[str, Any]] = {}
    for raw_source in sources:
        if not isinstance(raw_source, dict):
            continue
        name = _normalize_text(raw_source.get("name"))
        if name:
            mapping[name] = raw_source
    return mapping


def get_hanall_source_health_report(limit: int = 20, room_key: str | None = None) -> list[dict[str, Any]]:
    snapshots = list_hanall_run_snapshots(limit=limit, room_key=room_key)
    source_config = _source_config_map()
    health: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "source_name": "",
            "recent_checked_count": 0,
            "recent_gap_count": 0,
            "recent_official_hit_count": 0,
            "last_successful_seen_at": None,
            "parser_specialization_used": False,
            "stage2_search_rescue_count": 0,
        }
    )

    for snapshot in snapshots:
        stage_output = _deserialize_stage_output(snapshot.get("merged_stage1_output") or {})
        stage2_output = snapshot.get("stage2_output") or {}
        created_at = snapshot.get("created_at_kst")
        for entry in stage_output.checked_source_log:
            source_name = _normalize_text(entry.source_name) or "-"
            bucket = health[source_name]
            bucket["source_name"] = source_name
            bucket["recent_checked_count"] += 1
            if entry.status == "checked":
                bucket["last_successful_seen_at"] = max(
                    [value for value in [bucket["last_successful_seen_at"], created_at] if value],
                    default=created_at,
                )
        for gap in stage_output.coverage_gaps:
            source_name = _normalize_text(gap.source_name) or "-"
            bucket = health[source_name]
            bucket["source_name"] = source_name
            bucket["recent_gap_count"] += 1
        for item in [*stage_output.company_direct_confirmed, *stage_output.competitor_relevant_confirmed]:
            source_name = _normalize_text(item.source_name) or "-"
            bucket = health[source_name]
            bucket["source_name"] = source_name
            bucket["recent_official_hit_count"] += 1
        for entry in list(stage2_output.get("updated_source_logs") or []):
            source_name = _normalize_text(entry.get("source_name")) or "-"
            bucket = health[source_name]
            bucket["source_name"] = source_name
            bucket["stage2_search_rescue_count"] += 1

    for source_name, bucket in health.items():
        config = source_config.get(source_name) or {}
        bucket["parser_specialization_used"] = bool(
            config.get("detail_followup") or config.get("event_detection") or int(config.get("max_items_per_page") or 1) > 1
        )

    return sorted(
        health.values(),
        key=lambda item: (
            -int(item["recent_gap_count"]),
            -int(item["stage2_search_rescue_count"]),
            _normalize_key(item["source_name"]),
        ),
    )
