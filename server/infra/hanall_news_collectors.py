from __future__ import annotations

import io
import logging
import re
import zipfile
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from time import perf_counter
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

import requests

from server.application.hanall_page_items import (
    build_generated_known_events_from_page_items,
    build_page_item_canonical_payload,
    build_page_item_fingerprint,
    build_page_item_identity,
    parse_known_event_kst,
)
from server.application.hanall_research import (
    COMPETITOR_TERMS,
    DIRECT_TERMS,
    INDICATION_TERMS,
    _extract_anchor_items,
    _extract_visible_lines,
)
from server.config import get_hanall_sources_config
from server.core.hanall_news_models import (
    CheckedSourceLogEntry,
    CoverageGap,
    OfficialCollectionResult,
    OfficialPageItem,
    RawFinding,
)
from server.infra.hanall_page_parsers import parse_official_page_items, promote_official_page_item_to_finding
from server.infra.hanall_detail_parsers import parse_detail_page, should_follow_detail
from server.infra.sqlite_store import record_page_observation
from server.settings import AppSettings, get_settings
from server.utils import now_kst, smart_truncate

logger = logging.getLogger(__name__)

USER_AGENT = "kakao-bot/1.0 (hanall official collectors)"
PRIMARY_QUERY = '"Immunovant" OR "HanAll Biopharma" OR batoclimab OR IMVT-1401 OR IMVT-1402 OR HL161 OR HL036'
COMPETITOR_QUERY = 'FcRn OR argenx OR efgartigimod OR rozanolixizumab OR nipocalimab OR TED OR CIDP OR Sjogren'
REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_RESULT_LIMIT = 5
MFDS_DEFAULT_NUM_ROWS = 100
DART_COMPANY_ALIASES = ("한올바이오파마", "HanAll Biopharma", "HANALL BIOPHARMA")
IMMUNOVANT_ALIASES = ("Immunovant", "IMVT")
EUROPE_PMC_CANONICAL_TERMS = (
    "Immunovant",
    '"HanAll Biopharma"',
    "batoclimab",
    "IMVT-1401",
    "IMVT-1402",
    "HL161",
    "HL036",
)
SECRET_FIELD_PATTERNS = (
    re.compile(r'("?(?:api-key|api_key|serviceKey|token|Authorization|crtfc_key)"?\s*:\s*")([^"]+)(")', re.IGNORECASE),
    re.compile(r'("?(?:api-key|api_key|serviceKey|token|Authorization|crtfc_key)"?\s*:\s*)([^,"\s}]+)', re.IGNORECASE),
    re.compile(r'((?:api-key|api_key|serviceKey|token|Authorization|crtfc_key)=)([^&\s]+)', re.IGNORECASE),
)
LOW_PRECISION_SOURCE_NAMES = {"crossref", "biorxiv"}
STRICT_RELEVANCE_TERMS = (
    "immunovant",
    "hanall biopharma",
    "hanall",
    "한올바이오파마",
    "한올",
    "batoclimab",
    "imvt-1401",
    "imvt-1402",
    "hl161",
    "hl036",
    "tanfanercept",
)
TARGET_MOA_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("batoclimab", "hl161", "imvt-1401", "rvt-1401", "hbm9161", "imvt-1402", "hl161ans", "fcrn"), "FcRn antagonist"),
    (("tanfanercept", "hl036", "tnfr"), "TNFR1 fusion protein / anti-inflammatory biologic"),
    (("efgartigimod", "vyvgart", "rozanolixizumab", "rystiggo", "nipocalimab"), "FcRn antagonist"),
    (("teprotumumab", "tepezza"), "IGF-1R inhibitor"),
)


def _apply_page_item_detail_fields(page_item: OfficialPageItem, detail_fields: dict[str, Any]) -> None:
    for field_name, field_value in detail_fields.items():
        if field_value in (None, "", [], {}):
            continue
        if field_name == "published_at_kst":
            page_item.published_at_kst = _safe_text(field_value)
            parsed = parse_known_event_kst(page_item.published_at_kst)
            if parsed is not None:
                page_item.published_at = parsed
            continue
        if field_name == "updated_at_kst":
            page_item.updated_at_kst = _safe_text(field_value)
            parsed = parse_known_event_kst(page_item.updated_at_kst)
            if parsed is not None:
                page_item.updated_at = parsed
            continue
        setattr(page_item, field_name, field_value)


def _format_kst_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(now_kst().tzinfo).strftime("%Y-%m-%d %H:%M KST")


def _infer_target_moa(*values: Any) -> str | None:
    lowered = " ".join(_stringify_values(values)).lower()
    if not lowered:
        return None
    for aliases, target_moa in TARGET_MOA_RULES:
        if any(alias in lowered for alias in aliases):
            return target_moa
    return None


def _numeric_field_text(value: Any) -> str | None:
    text = _safe_text(value)
    return text or None


def _ensure_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stringify_values(value: Any) -> list[str]:
    values: list[str] = []
    if value is None:
        return values
    if isinstance(value, dict):
        preferred_keys = ("name", "label", "title", "text", "value", "id", "doi", "url")
        for key in preferred_keys:
            text = _safe_text(value.get(key))
            if text:
                values.append(text)
        if values:
            return values
        for nested_value in value.values():
            values.extend(_stringify_values(nested_value))
        return values
    if isinstance(value, (list, tuple, set)):
        for item in value:
            values.extend(_stringify_values(item))
        return values
    text = _safe_text(value)
    return [text] if text else []


def _join_values(value: Any, *, delimiter: str = ", ") -> str:
    seen: set[str] = set()
    normalized: list[str] = []
    for item in _stringify_values(value):
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return delimiter.join(normalized)


def _deep_get(payload: Any, path: str) -> Any:
    current = payload
    for segment in str(path or "").split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(segment)
            continue
        if isinstance(current, list):
            if segment.isdigit():
                index = int(segment)
                if index >= len(current):
                    return None
                current = current[index]
                continue
            expanded: list[Any] = []
            for item in current:
                if isinstance(item, dict) and segment in item:
                    expanded.append(item.get(segment))
            current = expanded if expanded else None
            continue
        return None
    return current


def _first_text(payload: Any, *paths: str, delimiter: str = ", ") -> str:
    for path in paths:
        text = _join_values(_deep_get(payload, path), delimiter=delimiter)
        if text:
            return text
    return ""


def _first_datetime(payload: Any, *paths: str) -> datetime | None:
    for path in paths:
        value = _deep_get(payload, path)
        if isinstance(value, list):
            for item in value:
                parsed = _parse_datetime(item)
                if parsed:
                    return parsed
            continue
        parsed = _parse_datetime(value)
        if parsed:
            return parsed
    return None


def _new_parser_stats() -> dict[str, Any]:
    return {"warning_count": 0, "field_failures": Counter()}


def _record_parser_warning(stats: dict[str, Any] | None, field_name: str) -> None:
    if stats is None:
        return
    stats["warning_count"] = int(stats.get("warning_count", 0)) + 1
    failures = stats.setdefault("field_failures", Counter())
    if isinstance(failures, Counter):
        failures[field_name] += 1


def _response_shape_summary(payload: Any) -> str:
    if isinstance(payload, dict):
        keys = ",".join(list(payload.keys())[:6]) or "-"
        nested_keys: list[str] = []
        for nested_key in ("message", "resultList", "response", "body", "meta", "data"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                nested_keys.append(f"{nested_key}({','.join(list(nested.keys())[:4]) or '-'})")
        return f"dict keys={keys} nested={'/'.join(nested_keys) if nested_keys else '-'}"
    if isinstance(payload, list):
        first_type = type(payload[0]).__name__ if payload else "-"
        return f"list len={len(payload)} first_type={first_type}"
    if isinstance(payload, bytes):
        return f"bytes len={len(payload)}"
    if isinstance(payload, str):
        compact = " ".join(str(payload).split())
        return f"text len={len(payload)} excerpt={smart_truncate(compact, 120)}"
    return f"type={type(payload).__name__}"


def _mask_secret_text(text: str) -> str:
    masked = str(text or "")
    for pattern in SECRET_FIELD_PATTERNS:
        masked = pattern.sub(lambda match: f"{match.group(1)}***{match.group(3) if match.lastindex and match.lastindex >= 3 else ''}", masked)
    return masked


def _masked_exception_text(exc: Exception) -> str:
    return _mask_secret_text(str(exc or ""))


def _response_body_excerpt(response: requests.Response | None) -> str:
    if response is None:
        return ""
    try:
        content_type = response.headers.get("Content-Type", "")
        text = _mask_secret_text(response.text or "")
        compact = " ".join(text.split())
        if "application/json" in content_type:
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                    code = _safe_text(payload["error"].get("code"))
                    message = _mask_secret_text(_safe_text(payload["error"].get("message")))
                    return smart_truncate(" | ".join(filter(None, [code, message])), 160)
            except ValueError:
                pass
        return smart_truncate(compact, 160)
    except Exception:
        return ""


def _response_json(response: requests.Response | None) -> dict[str, Any]:
    if response is None:
        return {}
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        return {}


def _is_auth_invalid_response(response: requests.Response | None, *, source_name: str) -> bool:
    if response is None:
        return False
    body_excerpt = _response_body_excerpt(response).lower()
    payload = _response_json(response)
    error_block = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    error_code = _safe_text(error_block.get("code")).lower()
    error_message = _safe_text(error_block.get("message")).lower()
    combined = " ".join(bit for bit in (body_excerpt, error_code, error_message) if bit)
    if response.status_code == 401:
        return True
    if source_name == "sec_api":
        return "api token invalid" in combined or "invalid token" in combined
    if source_name == "openfda":
        return "api_key_invalid" in combined or "invalid api_key" in combined
    if source_name == "ncbi":
        return "api key invalid" in combined
    return False


def _is_openfda_no_match_response(response: requests.Response | None) -> bool:
    if response is None or response.status_code != 404:
        return False
    payload = _response_json(response)
    error_block = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = _safe_text(error_block.get("code")).lower()
    message = _safe_text(error_block.get("message")).lower()
    combined = " ".join(filter(None, [code, message, _response_body_excerpt(response).lower()]))
    return "not_found" in combined and "no matches found" in combined


def _decode_data_go_kr_service_key(value: str | None) -> str:
    return unquote(_safe_text(value))


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(now_kst().tzinfo)

    if isinstance(value, dict):
        from_date_parts = _date_parts_to_datetime(value)
        if from_date_parts:
            return from_date_parts
        text = _first_text(value, "date", "value", "date-time", "datetime", "text")
    else:
        text = _safe_text(value)
    if not text:
        return None

    candidate_formats = (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y%m%d",
        "%Y%m%d%H%M%S",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y %b %d",
        "%Y %B %d",
        "%b %d, %Y",
        "%B %d, %Y",
    )
    normalized = text.replace("Z", "+00:00")
    for fmt in candidate_formats:
        try:
            parsed = datetime.strptime(normalized, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now_kst().tzinfo)
            return parsed.astimezone(now_kst().tzinfo)
        except ValueError:
            continue

    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now_kst().tzinfo)
        return parsed.astimezone(now_kst().tzinfo)
    except (TypeError, ValueError, IndexError):
        pass

    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now_kst().tzinfo)
        return parsed.astimezone(now_kst().tzinfo)
    except ValueError:
        return None


def _date_parts_to_datetime(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    date_parts = value.get("date-parts")
    if not isinstance(date_parts, list) or not date_parts or not isinstance(date_parts[0], list) or not date_parts[0]:
        return None
    try:
        year = int(date_parts[0][0])
        month = int(date_parts[0][1]) if len(date_parts[0]) > 1 else 1
        day = int(date_parts[0][2]) if len(date_parts[0]) > 2 else 1
        return datetime(year, month, day, tzinfo=now_kst().tzinfo)
    except (TypeError, ValueError):
        return None


def _coerce_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "message", "resultList", "results", "response", "body"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            nested_items = _extract_items(nested)
            if nested_items:
                return nested_items
    for key in ("data", "results", "studies", "collection", "items", "transactions", "filings", "records"):
        if isinstance(payload.get(key), list):
            return [item for item in payload.get(key, []) if isinstance(item, dict)]
        if isinstance(payload.get(key), dict):
            nested_items = _extract_items(payload.get(key))
            if nested_items:
                return nested_items
    if isinstance(payload.get("message"), dict) and isinstance(payload["message"].get("items"), list):
        return [item for item in payload["message"].get("items", []) if isinstance(item, dict)]
    if isinstance(payload.get("resultList"), dict) and isinstance(payload["resultList"].get("result"), list):
        return [item for item in payload["resultList"].get("result", []) if isinstance(item, dict)]
    if isinstance(payload.get("resultList"), dict) and isinstance(payload["resultList"].get("result"), dict):
        return [payload["resultList"]["result"]]
    body = payload.get("body") if isinstance(payload.get("body"), dict) else None
    if body and isinstance(body.get("items"), dict):
        raw_item = body["items"].get("item")
        return [item for item in _coerce_list(raw_item) if isinstance(item, dict)]
    if body and isinstance(body.get("items"), list):
        return [item for item in body.get("items", []) if isinstance(item, dict)]
    response = payload.get("response") if isinstance(payload.get("response"), dict) else None
    if response and isinstance(response.get("body"), dict):
        raw_item = response["body"].get("items", {}).get("item") if isinstance(response["body"].get("items"), dict) else None
        return [item for item in _coerce_list(raw_item) if isinstance(item, dict)]
    return []


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in terms)


def _classify_entity(title: str, summary: str, entity: str | None = None) -> tuple[str, str]:
    combined = " ".join(part for part in (entity or "", title, summary) if part).lower()
    resolved_entity = str(entity or "").strip() or "HanAll/Immunovant watch"
    if _contains_any(combined, DIRECT_TERMS):
        if "hanall" in combined or "한올바이오파마" in combined:
            resolved_entity = "HanAll Biopharma"
        elif "immunovant" in combined or "imvt" in combined:
            resolved_entity = "Immunovant"
        return resolved_entity, "company_direct"
    if _contains_any(combined, COMPETITOR_TERMS) or _contains_any(combined, INDICATION_TERMS):
        return resolved_entity, "competitor_relevant"
    return resolved_entity, "company_direct"


def _safe_text(value: Any, default: str = "") -> str:
    normalized = str(value or "").strip()
    return normalized or default


def _finding_reference_time(finding: RawFinding, *, fallback_tz: Any) -> datetime | None:
    reference_time = finding.updated_at or finding.published_at
    if reference_time is None:
        return None
    if reference_time.tzinfo is None:
        return reference_time.replace(tzinfo=fallback_tz)
    return reference_time.astimezone(fallback_tz)


def _is_recent_finding(finding: RawFinding, *, current_now: datetime) -> bool:
    tzinfo = current_now.tzinfo or now_kst().tzinfo
    reference_time = _finding_reference_time(finding, fallback_tz=tzinfo)
    if reference_time is None:
        return False
    normalized_now = current_now if current_now.tzinfo else current_now.replace(tzinfo=tzinfo)
    normalized_now = normalized_now.astimezone(tzinfo)
    window_start = normalized_now - timedelta(days=1)
    return window_start <= reference_time <= normalized_now


def _is_low_precision_relevant(finding: RawFinding) -> bool:
    if finding.source_name not in LOW_PRECISION_SOURCE_NAMES:
        return True
    combined = " ".join(
        part
        for part in (
            finding.entity,
            finding.title,
            finding.summary,
            finding.asset,
            finding.indication,
            finding.document_id,
        )
        if part
    ).lower()
    return any(term in combined for term in STRICT_RELEVANCE_TERMS)


class BaseHanallCollector(ABC):
    source_name: str
    source_family: str
    source_group: str = "regulator_disclosure"

    def __init__(self, settings: AppSettings, collector_config: dict[str, Any]) -> None:
        self.settings = settings
        self.collector_config = collector_config
        endpoints = collector_config.get("endpoints", {})
        self.endpoints = endpoints if isinstance(endpoints, dict) else {}

    def _is_enabled(self) -> bool:
        return bool(self.collector_config.get("enabled", True))

    def _requires_api_key(self) -> bool:
        return bool(self.collector_config.get("requires_api_key", False))

    def _current_kst(self, current_now: datetime | None = None) -> datetime:
        return current_now or now_kst()

    def _pick_text(
        self,
        payload: Any,
        *paths: str,
        field_name: str,
        default: str = "",
        stats: dict[str, Any] | None = None,
        delimiter: str = ", ",
    ) -> str:
        text = _first_text(payload, *paths, delimiter=delimiter)
        if text:
            return text
        _record_parser_warning(stats, field_name)
        return default

    def _pick_datetime(
        self,
        payload: Any,
        *paths: str,
        field_name: str,
        stats: dict[str, Any] | None = None,
    ) -> datetime | None:
        parsed = _first_datetime(payload, *paths)
        if parsed:
            return parsed
        _record_parser_warning(stats, field_name)
        return None

    def _data_go_kr_params(self, **params: Any) -> dict[str, Any]:
        return {
            **params,
            "serviceKey": _decode_data_go_kr_service_key(self._api_key()),
        }

    def _source_log(
        self,
        *,
        status: str,
        note: str,
        endpoint: str,
        checked_at: datetime,
        http_status: int | None = None,
    ) -> CheckedSourceLogEntry:
        return CheckedSourceLogEntry(
            source_family=self.source_family,
            source_name=self.source_name,
            source_group=self.source_group,
            status=status,
            checked_at_kst=checked_at.strftime("%Y-%m-%d %H:%M KST"),
            note=_mask_secret_text(note),
            endpoint=endpoint,
            http_status=http_status,
        )

    def _gap(
        self,
        *,
        gap_type: str,
        detail: str,
        endpoint: str,
        severity: str = "medium",
        http_status: int | None = None,
    ) -> CoverageGap:
        return CoverageGap(
            source_family=self.source_family,
            source_name=self.source_name,
            source_group=self.source_group,
            gap_type=gap_type,
            detail=_mask_secret_text(detail),
            severity=severity,
            endpoint=endpoint,
            http_status=http_status,
        )

    def _empty_result(
        self,
        *,
        checked_at: datetime,
        status: str,
        note: str,
        endpoint: str = "-",
        gap_type: str | None = None,
        severity: str = "medium",
        http_status: int | None = None,
    ) -> OfficialCollectionResult:
        gaps = []
        if gap_type:
            gaps.append(
                self._gap(
                    gap_type=gap_type,
                    detail=note,
                    endpoint=endpoint,
                    severity=severity,
                    http_status=http_status,
                )
            )
        return OfficialCollectionResult(
            checked_source_log=[
                self._source_log(
                    status=status,
                    note=note,
                    endpoint=endpoint,
                    checked_at=checked_at,
                    http_status=http_status,
                )
            ],
            coverage_gaps=gaps,
        )

    def _missing_api_key_result(self, *, checked_at: datetime) -> OfficialCollectionResult:
        return self._empty_result(
            checked_at=checked_at,
            status="missing_api_key",
            note=f"{self.source_name} collector requires API key",
            endpoint=next(iter(self.endpoints.values()), "-"),
            gap_type="missing_api_key",
            severity="high",
        )

    def _disabled_result(self, *, checked_at: datetime, reason: str | None = None) -> OfficialCollectionResult:
        return self._empty_result(
            checked_at=checked_at,
            status="disabled",
            note=reason or "collector disabled by config",
            endpoint=next(iter(self.endpoints.values()), "-"),
            gap_type="disabled",
            severity="low",
        )

    def _api_key(self) -> str | None:
        return None

    def _http_gap_type(self, status_code: int | None) -> str:
        if status_code == 401:
            return "http_401_unauthorized"
        if status_code == 403:
            return "http_403_forbidden"
        if status_code == 404:
            return "http_404_not_found"
        if status_code == 429:
            return "http_429_rate_limited"
        return "http_error"

    def _log_fetch_result(
        self,
        *,
        endpoint: str,
        status_code: int | None,
        elapsed_ms: float,
        item_count: int | None,
        note: str = "",
        ok: bool = True,
    ) -> None:
        logger.info(
            "hanall collector fetch source=%s family=%s endpoint=%s ok=%s status_code=%s elapsed_ms=%.1f item_count=%s note=%s",
            self.source_name,
            self.source_family,
            endpoint,
            ok,
            status_code,
            elapsed_ms,
            item_count,
            note,
        )

    def _log_response_summary(
        self,
        *,
        endpoint: str,
        payload: Any,
        item_count: int | None = None,
        note: str = "",
    ) -> None:
        logger.info(
            "hanall collector response_summary source=%s family=%s endpoint=%s item_count=%s summary=%s note=%s",
            self.source_name,
            self.source_family,
            endpoint,
            item_count,
            _response_shape_summary(payload),
            note,
        )

    def _log_parser_summary(
        self,
        *,
        endpoint: str,
        parsed_count: int,
        stats: dict[str, Any] | None = None,
        note: str = "",
    ) -> None:
        field_failures = stats.get("field_failures", Counter()) if isinstance(stats, dict) else Counter()
        if isinstance(field_failures, Counter):
            field_summary = ",".join(
                f"{field}={count}" for field, count in field_failures.most_common(5)
            ) or "-"
        else:
            field_summary = "-"
        warning_count = int(stats.get("warning_count", 0)) if isinstance(stats, dict) else 0
        logger.info(
            "hanall collector parser_summary source=%s family=%s endpoint=%s parsed_count=%s warning_count=%s field_failures=%s note=%s",
            self.source_name,
            self.source_family,
            endpoint,
            parsed_count,
            warning_count,
            field_summary,
            note,
        )

    def _http_error_detail(self, exc: requests.HTTPError) -> str:
        detail = _masked_exception_text(exc)
        response_excerpt = _response_body_excerpt(exc.response)
        return f"{detail} body={response_excerpt}" if response_excerpt else detail

    def _request(
        self,
        session: requests.Session,
        *,
        method: str = "GET",
        endpoint: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> requests.Response:
        start = perf_counter()
        response = session.request(
            method=method,
            url=endpoint,
            params=params,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/xml, application/xml, text/plain",
                **(headers or {}),
            },
            json=json_body,
            timeout=timeout,
        )
        self._log_fetch_result(
            endpoint=endpoint,
            status_code=response.status_code,
            elapsed_ms=(perf_counter() - start) * 1000,
            item_count=None,
            note=f"method={method} content_type={response.headers.get('Content-Type', '-')}",
            ok=response.ok,
        )
        response.raise_for_status()
        return response

    def _request_json(
        self,
        session: requests.Session,
        *,
        method: str = "GET",
        endpoint: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        response = self._request(
            session,
            method=method,
            endpoint=endpoint,
            params=params,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
        )
        payload = response.json()
        self._log_response_summary(endpoint=endpoint, payload=payload, note=f"method={method}")
        return payload

    def _request_text(
        self,
        session: requests.Session,
        *,
        method: str = "GET",
        endpoint: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> str:
        response = self._request(
            session,
            method=method,
            endpoint=endpoint,
            params=params,
            headers=headers,
            timeout=timeout,
        )
        self._log_response_summary(endpoint=endpoint, payload=response.text, note=f"method={method}")
        return response.text

    def _request_bytes(
        self,
        session: requests.Session,
        *,
        method: str = "GET",
        endpoint: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> bytes:
        response = self._request(
            session,
            method=method,
            endpoint=endpoint,
            params=params,
            headers=headers,
            timeout=timeout,
        )
        self._log_response_summary(endpoint=endpoint, payload=response.content, note=f"method={method}")
        return response.content

    def _http_error_result(
        self,
        *,
        checked_at: datetime,
        endpoint: str,
        exc: requests.HTTPError,
        note_prefix: str = "",
    ) -> OfficialCollectionResult:
        status_code = exc.response.status_code if exc.response is not None else None
        response_excerpt = _response_body_excerpt(exc.response)
        note = f"{note_prefix}{exc}"
        if response_excerpt:
            note = f"{note} body={response_excerpt}"
        return self._empty_result(
            checked_at=checked_at,
            status=f"http_{status_code}" if status_code else "http_error",
            note=note,
            endpoint=endpoint,
            gap_type=self._http_gap_type(status_code),
            http_status=status_code,
        )

    def _build_finding(
        self,
        *,
        title: str,
        summary: str,
        published_at: datetime | None,
        updated_at: datetime | None = None,
        entity: str | None = None,
        document_type: str | None = None,
        document_id: str | None = None,
        filing_type: str | None = None,
        trial_id: str | None = None,
        asset: str | None = None,
        indication: str | None = None,
        region: str | None = None,
        primary_source_url: str | None = None,
        secondary_source_url: str | None = None,
        source_note: str | None = None,
        confidence: float = 0.6,
        aliases: list[str] | None = None,
        sponsor: str | None = None,
        target_moa: str | None = None,
        phase: str | None = None,
        recruitment_status: str | None = None,
        enrollment: str | None = None,
        primary_completion_date: str | None = None,
        last_update_posted: str | None = None,
        site_countries: list[str] | None = None,
        changed_fields: list[str] | None = None,
        regulator: str | None = None,
        exchange: str | None = None,
        filed_at: str | None = None,
        accepted_at: str | None = None,
        event_action: str | None = None,
        key_numbers: list[str] | None = None,
        insider_person: str | None = None,
        insider_role: str | None = None,
        insider_quantity: str | None = None,
        insider_price: str | None = None,
        trade_date: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> RawFinding:
        resolved_entity, category = _classify_entity(title, summary, entity=entity)
        return RawFinding(
            source_family=self.source_family,
            source_name=self.source_name,
            source_group=self.source_group,
            source_tier="official_api",
            entity=resolved_entity,
            entity_type="company" if category == "company_direct" else "competitor",
            category=category,
            title=title,
            summary=summary,
            published_at=published_at,
            updated_at=updated_at,
            document_type=document_type,
            document_id=document_id,
            filing_type=filing_type,
            trial_id=trial_id,
            asset=asset,
            aliases=aliases or [],
            sponsor=sponsor,
            target_moa=target_moa,
            indication=indication,
            region=region,
            phase=phase,
            recruitment_status=recruitment_status,
            enrollment=enrollment,
            primary_completion_date=primary_completion_date,
            last_update_posted=last_update_posted,
            site_countries=site_countries or [],
            changed_fields=changed_fields or [],
            regulator=regulator,
            exchange=exchange,
            filed_at=filed_at,
            accepted_at=accepted_at,
            event_action=event_action,
            key_numbers=key_numbers or [],
            insider_person=insider_person,
            insider_role=insider_role,
            insider_quantity=insider_quantity,
            insider_price=insider_price,
            trade_date=trade_date,
            primary_source_url=primary_source_url,
            secondary_source_url=secondary_source_url,
            source_note=source_note,
            confidence=confidence,
            raw_payload=raw_payload or {},
        )

    def collect(self, session: requests.Session, *, current_now: datetime | None = None) -> OfficialCollectionResult:
        checked_at = self._current_kst(current_now)
        if not self._is_enabled():
            return self._disabled_result(checked_at=checked_at)
        if self._requires_api_key() and not self._api_key():
            return self._missing_api_key_result(checked_at=checked_at)
        return self._collect(session, checked_at=checked_at)

    @abstractmethod
    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        raise NotImplementedError

class SecApiCollector(BaseHanallCollector):
    source_name = "sec_api"
    source_family = "sec"
    source_group = "regulator_disclosure"

    def _api_key(self) -> str | None:
        value = self.settings.sec_api_key.strip()
        return value if value and value != "replace_me" else None

    def _sec_api_auth_attempts(self) -> list[tuple[str, dict[str, str] | None, dict[str, Any] | None]]:
        api_key = self._api_key()
        if not api_key:
            return []
        return [
            ("authorization_header", {"Authorization": api_key}, None),
            ("token_query_param", None, {"token": api_key}),
        ]

    def _request_sec_api_json(
        self,
        session: requests.Session,
        *,
        endpoint: str,
        json_body: dict[str, Any],
    ) -> tuple[Any, str]:
        last_auth_exc: requests.HTTPError | None = None
        for auth_mode, headers, params in self._sec_api_auth_attempts():
            try:
                payload = self._request_json(
                    session,
                    method="POST",
                    endpoint=endpoint,
                    params=params,
                    headers=headers,
                    json_body=json_body,
                )
                return payload, auth_mode
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code in {401, 403} and _is_auth_invalid_response(exc.response, source_name=self.source_name):
                    last_auth_exc = exc
                    continue
                raise
        if last_auth_exc is not None:
            raise last_auth_exc
        raise requests.HTTPError("sec-api request failed before auth attempt")

    def _parse_sec_items(self, items: list[dict[str, Any]], endpoint_key: str, endpoint: str) -> list[RawFinding]:
        findings: list[RawFinding] = []
        stats = _new_parser_stats()
        for item in items[:DEFAULT_RESULT_LIMIT]:
            filed_at_dt = self._pick_datetime(
                item,
                "filedAt",
                "periodOfReport",
                "filingDate",
                field_name="filed_at",
                stats=stats,
            )
            accepted_at_dt = self._pick_datetime(
                item,
                "acceptedAt",
                "filedAt",
                field_name="accepted_at",
                stats=stats,
            )
            title = self._pick_text(
                item,
                "description",
                "title",
                "documentFormatFiles.0.description",
                "documentFormatFiles.description",
                "documentType",
                "formType",
                field_name="title",
                default=f"SEC API {endpoint_key}",
                stats=stats,
            )
            entity = self._pick_text(
                item,
                "companyName",
                "issuerName",
                "issuerTradingSymbol",
                "ticker",
                "ownerName",
                field_name="entity",
                default="SEC API",
                stats=stats,
            )
            filing_type = self._pick_text(
                item,
                "formType",
                "documentType",
                "transactionCode",
                field_name="filing_type",
                stats=stats,
            )
            accession_no = self._pick_text(
                item,
                "accessionNo",
                "accessionNumber",
                "accessionNumberLong",
                "id",
                field_name="document_id",
                stats=stats,
            )
            primary_url = self._pick_text(
                item,
                "linkToFilingDetails",
                "linkToTxt",
                "linkToHtml",
                "link",
                "sourceUrl",
                field_name="primary_source_url",
                default=endpoint,
                stats=stats,
            )
            summary = smart_truncate(
                " | ".join(
                    filter(
                        None,
                        [
                            filing_type,
                            self._pick_text(item, "ticker", "issuerTradingSymbol", field_name="ticker", stats=None),
                            self._pick_text(item, "periodOfReport", "filingDate", field_name="period_of_report", stats=None),
                            self._pick_text(item, "filedAt", "acceptedAt", field_name="filed_at_raw", stats=None),
                            self._pick_text(item, "ownerName", field_name="owner_name", stats=None),
                        ],
                    )
                ),
                420,
            )
            findings.append(
                self._build_finding(
                    title=title,
                    summary=summary,
                    published_at=accepted_at_dt or filed_at_dt or self._pick_datetime(
                        item,
                        "transactionDate",
                        field_name="published_at",
                        stats=None,
                    ),
                    entity=entity,
                    document_type="sec_filing",
                    document_id=accession_no,
                    filing_type=filing_type,
                    regulator="SEC",
                    exchange=self._pick_text(item, "exchange", "issuerExchange", field_name="exchange", stats=None),
                    filed_at=_format_kst_text(filed_at_dt) or self._pick_text(item, "filingDate", field_name="filed_at_text", stats=None),
                    accepted_at=_format_kst_text(accepted_at_dt),
                    event_action=self._pick_text(
                        item,
                        "transactionCode",
                        "documentType",
                        "formType",
                        field_name="event_action",
                        stats=None,
                    ),
                    key_numbers=[
                        value
                        for value in [
                            _numeric_field_text(_deep_get(item, "transactionShares")),
                            _numeric_field_text(_deep_get(item, "transactionPricePerShare")),
                            _numeric_field_text(_deep_get(item, "sharesOwnedFollowingTransaction")),
                        ]
                        if value
                    ],
                    insider_person=self._pick_text(item, "ownerName", field_name="insider_person", stats=None),
                    insider_role=self._pick_text(
                        item,
                        "officerTitle",
                        "ownerRelationship",
                        field_name="insider_role",
                        stats=None,
                    ),
                    insider_quantity=self._pick_text(
                        item,
                        "transactionShares",
                        "amountOfShares",
                        field_name="insider_quantity",
                        stats=None,
                    ),
                    insider_price=self._pick_text(
                        item,
                        "transactionPricePerShare",
                        "price",
                        field_name="insider_price",
                        stats=None,
                    ),
                    trade_date=self._pick_text(item, "transactionDate", field_name="trade_date", stats=None),
                    primary_source_url=primary_url,
                    secondary_source_url=endpoint,
                    source_note=f"endpoint={endpoint_key}",
                    raw_payload=item,
                )
            )
        self._log_parser_summary(
            endpoint=endpoint,
            parsed_count=len(findings),
            stats=stats,
            note=f"endpoint={endpoint_key}",
        )
        return findings

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        source_logs: list[CheckedSourceLogEntry] = []
        coverage_gaps: list[CoverageGap] = []
        findings: list[RawFinding] = []
        endpoint_specs = (
            (
                "api_root",
                "POST",
                {
                    "query": 'ticker:IMVT OR companyName:"Immunovant"',
                    "from": "0",
                    "size": str(DEFAULT_RESULT_LIMIT),
                    "sort": [{"filedAt": {"order": "desc"}}],
                },
            ),
            (
                "full_text_search",
                "POST",
                {
                    "query": PRIMARY_QUERY.replace('"', ""),
                    "from": (checked_at - timedelta(days=1)).strftime("%Y-%m-%d"),
                    "to": checked_at.strftime("%Y-%m-%d"),
                    "page": "1",
                },
            ),
            (
                "form_8k",
                "POST",
                {
                    "query": 'ticker:IMVT AND formType:"8-K"',
                    "from": "0",
                    "size": str(DEFAULT_RESULT_LIMIT),
                    "sort": [{"filedAt": {"order": "desc"}}],
                },
            ),
            (
                "insider_trading",
                "POST",
                {
                    "query": 'issuer.tradingSymbol:IMVT OR issuerName:"Immunovant"',
                    "from": "0",
                    "size": str(DEFAULT_RESULT_LIMIT),
                    "sort": [{"filedAt": {"order": "desc"}}],
                },
            ),
            (
                "sec_litigation_releases",
                "POST",
                {
                    "query": 'entities.tickers:IMVT OR entities.companyName:"Immunovant"',
                    "from": "0",
                    "size": str(DEFAULT_RESULT_LIMIT),
                    "sort": [{"releasedAt": {"order": "desc"}}],
                },
            ),
        )
        for endpoint_key, method, payload in endpoint_specs:
            endpoint = _safe_text(self.endpoints.get(endpoint_key))
            if not endpoint:
                continue
            try:
                response_payload, auth_mode = self._request_sec_api_json(
                    session,
                    endpoint=endpoint,
                    json_body=payload,
                )
                endpoint_items = _extract_items(response_payload)
                parsed_findings = self._parse_sec_items(endpoint_items, endpoint_key, endpoint)
                findings.extend(parsed_findings)
                source_logs.append(
                    self._source_log(
                        status="checked",
                        note=f"endpoint={endpoint_key} items={len(endpoint_items)} auth={auth_mode}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                    )
                )
                self._log_fetch_result(
                    endpoint=endpoint,
                    status_code=200,
                    elapsed_ms=0,
                    item_count=len(endpoint_items),
                    note=f"collector={self.source_name} endpoint={endpoint_key} auth={auth_mode}",
                )
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                detail = self._http_error_detail(exc)
                logger.warning("sec-api collector http error endpoint=%s status=%s error=%s", endpoint_key, status_code, detail)
                source_logs.append(
                    self._source_log(
                        status=f"http_{status_code}" if status_code else "http_error",
                        note=f"endpoint={endpoint_key} error={detail}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                        http_status=status_code,
                    )
                )
                coverage_gaps.append(
                    self._gap(
                        gap_type=self._http_gap_type(status_code),
                        detail=detail,
                        endpoint=endpoint,
                        http_status=status_code,
                    )
                )
            except requests.RequestException as exc:
                logger.warning("sec-api collector request failed endpoint=%s error=%s", endpoint_key, _masked_exception_text(exc))
                source_logs.append(
                    self._source_log(
                        status="request_error",
                        note=f"endpoint={endpoint_key} error={exc}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                    )
                )
                coverage_gaps.append(
                    self._gap(
                        gap_type="request_error",
                        detail=_masked_exception_text(exc),
                        endpoint=endpoint,
                    )
                )
        return OfficialCollectionResult(findings=findings, checked_source_log=source_logs, coverage_gaps=coverage_gaps)


class OpenDartCollector(BaseHanallCollector):
    source_name = "opendart"
    source_family = "opendart"
    source_group = "regulator_disclosure"

    def _api_key(self) -> str | None:
        value = self.settings.opendart_api_key.strip()
        return value if value and value != "replace_me" else None

    def _load_corp_records(self, session: requests.Session) -> list[dict[str, str]]:
        endpoint = _safe_text(self.endpoints.get("corp_code"))
        if not endpoint:
            return []
        content = self._request_bytes(
            session,
            endpoint=endpoint,
            params={"crtfc_key": self._api_key()},
            headers={"Accept": "application/xml, application/octet-stream"},
        )
        xml_text: bytes | None = None
        first_name = "corpCode.xml"
        if content.lstrip().startswith(b"<"):
            xml_text = content
        else:
            with zipfile.ZipFile(io.BytesIO(content)) as zipped:
                xml_names = [name for name in zipped.namelist() if name.lower().endswith(".xml")]
                first_name = xml_names[0] if xml_names else zipped.namelist()[0]
                xml_text = zipped.read(first_name)
        root = ElementTree.fromstring(xml_text or b"")
        status = _safe_text(root.findtext(".//status"))
        message = _safe_text(root.findtext(".//message"))
        records: list[dict[str, str]] = []
        for item in root.findall(".//list") or root.findall(".//{*}list"):
            records.append(
                {
                    "corp_code": _safe_text(item.findtext("corp_code")),
                    "corp_name": _safe_text(item.findtext("corp_name")),
                    "stock_code": _safe_text(item.findtext("stock_code")),
                }
            )
        if status and status != "000" and not records:
            raise ValueError(f"OpenDART corpCode error status={status} message={message or '-'}")
        self._log_parser_summary(
            endpoint=endpoint,
            parsed_count=len(records),
            stats=None,
            note=f"corp_code_zip={first_name}",
        )
        return records

    def _match_corp_records(self, records: list[dict[str, str]]) -> list[dict[str, str]]:
        matched: list[dict[str, str]] = []
        for record in records:
            corp_name = _safe_text(record.get("corp_name")).lower()
            if any(alias.lower() in corp_name for alias in DART_COMPANY_ALIASES):
                matched.append(record)
        return matched[:1]

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        findings: list[RawFinding] = []
        source_logs: list[CheckedSourceLogEntry] = []
        coverage_gaps: list[CoverageGap] = []

        try:
            corp_records = self._load_corp_records(session)
            matched_records = self._match_corp_records(corp_records)
            source_logs.append(
                self._source_log(
                    status="checked",
                    note=f"corp_records={len(corp_records)} matched={len(matched_records)}",
                    endpoint=_safe_text(self.endpoints.get("corp_code")),
                    checked_at=checked_at,
                )
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            return OfficialCollectionResult(
                checked_source_log=[
                    self._source_log(
                        status=f"http_{status_code}" if status_code else "http_error",
                        note=_masked_exception_text(exc),
                        endpoint=_safe_text(self.endpoints.get("corp_code")),
                        checked_at=checked_at,
                        http_status=status_code,
                    )
                ],
                coverage_gaps=[
                    self._gap(
                        gap_type=self._http_gap_type(status_code),
                        detail=_masked_exception_text(exc),
                        endpoint=_safe_text(self.endpoints.get("corp_code")),
                        http_status=status_code,
                    )
                ],
            )
        except ValueError as exc:
            return OfficialCollectionResult(
                checked_source_log=[
                    self._source_log(
                        status="api_error",
                        note=_masked_exception_text(exc),
                        endpoint=_safe_text(self.endpoints.get("corp_code")),
                        checked_at=checked_at,
                    )
                ],
                coverage_gaps=[
                    self._gap(
                        gap_type="api_error",
                        detail=_masked_exception_text(exc),
                        endpoint=_safe_text(self.endpoints.get("corp_code")),
                    )
                ],
            )
        except (requests.RequestException, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            return OfficialCollectionResult(
                checked_source_log=[
                    self._source_log(
                        status="request_error",
                        note=_masked_exception_text(exc),
                        endpoint=_safe_text(self.endpoints.get("corp_code")),
                        checked_at=checked_at,
                    )
                ],
                coverage_gaps=[
                    self._gap(
                        gap_type="request_error",
                        detail=_masked_exception_text(exc),
                        endpoint=_safe_text(self.endpoints.get("corp_code")),
                    )
                ],
            )

        if not matched_records:
            coverage_gaps.append(
                self._gap(
                    gap_type="not_found",
                    detail="target corp_code not found from corpCode.xml",
                    endpoint=_safe_text(self.endpoints.get("corp_code")),
                    severity="medium",
                )
            )
            return OfficialCollectionResult(findings=findings, checked_source_log=source_logs, coverage_gaps=coverage_gaps)

        corp_record = matched_records[0]
        corp_code = _safe_text(corp_record.get("corp_code"))
        stock_code = _safe_text(corp_record.get("stock_code"))
        list_endpoint = _safe_text(self.endpoints.get("list"))
        company_endpoint = _safe_text(self.endpoints.get("company"))
        account_endpoint = _safe_text(self.endpoints.get("eng_single_account_all"))
        common_params = {"crtfc_key": self._api_key()}

        if list_endpoint:
            try:
                payload = self._request_json(
                    session,
                    endpoint=list_endpoint,
                    params={
                        **common_params,
                        "corp_code": corp_code,
                        "bgn_de": (checked_at - timedelta(days=1)).strftime("%Y%m%d"),
                        "end_de": checked_at.strftime("%Y%m%d"),
                        "page_no": 1,
                        "page_count": 10,
                    },
                )
                items = [item for item in _coerce_list(payload.get("list")) if isinstance(item, dict)]
                stats = _new_parser_stats()
                for item in items[:DEFAULT_RESULT_LIMIT]:
                    findings.append(
                        self._build_finding(
                            title=self._pick_text(
                                item,
                                "report_nm",
                                "rpt_nm",
                                field_name="title",
                                default="OpenDART filing",
                                stats=stats,
                            ),
                            summary=smart_truncate(
                                " | ".join(
                                    filter(
                                        None,
                                        [
                                            self._pick_text(item, "corp_name", field_name="corp_name", stats=None),
                                            self._pick_text(item, "flr_nm", "reporter_nm", field_name="reporter", stats=None),
                                            self._pick_text(item, "rm", field_name="remark", stats=None),
                                        ],
                                    )
                                ),
                                420,
                            ),
                            published_at=self._pick_datetime(item, "rcept_dt", "receipt_dt", field_name="published_at", stats=stats),
                            entity=self._pick_text(
                                item,
                                "corp_name",
                                "corpName",
                                field_name="entity",
                                default="HanAll Biopharma",
                                stats=stats,
                            ),
                            document_type="dart_filing",
                            document_id=self._pick_text(item, "rcept_no", "receipt_no", field_name="document_id", stats=stats),
                            filing_type=self._pick_text(item, "report_nm", "rpt_nm", field_name="filing_type", stats=None),
                            regulator="OpenDART / FSS",
                            exchange="KRX",
                            filed_at=_format_kst_text(self._pick_datetime(item, "rcept_dt", "receipt_dt", field_name="filed_at", stats=None)),
                            accepted_at=_format_kst_text(self._pick_datetime(item, "rcept_dt", "receipt_dt", field_name="accepted_at", stats=None)),
                            event_action=self._pick_text(item, "report_nm", "rpt_nm", field_name="event_action", stats=None),
                            key_numbers=[
                                value
                                for value in [
                                    self._pick_text(item, "rcept_no", field_name="rcept_no", stats=None),
                                    self._pick_text(item, "rm", field_name="remark", stats=None),
                                ]
                                if value
                            ],
                            primary_source_url=list_endpoint,
                            source_note="OpenDART list.json",
                            raw_payload=item,
                        )
                    )
                self._log_parser_summary(
                    endpoint=list_endpoint,
                    parsed_count=len(items[:DEFAULT_RESULT_LIMIT]),
                    stats=stats,
                    note=f"corp_code={corp_code}",
                )
                source_logs.append(
                    self._source_log(
                        status="checked",
                        note=f"items={len(items)} corp_code={corp_code}",
                        endpoint=list_endpoint,
                        checked_at=checked_at,
                    )
                )
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                detail = self._http_error_detail(exc)
                source_logs.append(
                    self._source_log(
                        status=f"http_{status_code}" if status_code else "http_error",
                        note=detail,
                        endpoint=list_endpoint,
                        checked_at=checked_at,
                        http_status=status_code,
                    )
                )
                coverage_gaps.append(
                    self._gap(
                        gap_type=self._http_gap_type(status_code),
                        detail=detail,
                        endpoint=list_endpoint,
                        http_status=status_code,
                    )
                )
            except requests.RequestException as exc:
                source_logs.append(
                    self._source_log(
                        status="request_error",
                        note=_masked_exception_text(exc),
                        endpoint=list_endpoint,
                        checked_at=checked_at,
                    )
                )
                coverage_gaps.append(self._gap(gap_type="request_error", detail=_masked_exception_text(exc), endpoint=list_endpoint))

        if company_endpoint:
            try:
                payload = self._request_json(
                    session,
                    endpoint=company_endpoint,
                    params={**common_params, "corp_code": corp_code},
                )
                source_logs.append(
                    self._source_log(
                        status="checked",
                        note=f"corp_name={_safe_text(payload.get('corp_name')) or corp_record.get('corp_name')}",
                        endpoint=company_endpoint,
                        checked_at=checked_at,
                    )
                )
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                detail = self._http_error_detail(exc)
                source_logs.append(
                    self._source_log(
                        status=f"http_{status_code}" if status_code else "http_error",
                        note=detail,
                        endpoint=company_endpoint,
                        checked_at=checked_at,
                        http_status=status_code,
                    )
                )
                coverage_gaps.append(
                    self._gap(
                        gap_type=self._http_gap_type(status_code),
                        detail=detail,
                        endpoint=company_endpoint,
                        http_status=status_code,
                    )
                )
            except requests.RequestException as exc:
                source_logs.append(
                    self._source_log(
                        status="request_error",
                        note=_masked_exception_text(exc),
                        endpoint=company_endpoint,
                        checked_at=checked_at,
                    )
                )
                coverage_gaps.append(self._gap(gap_type="request_error", detail=_masked_exception_text(exc), endpoint=company_endpoint))

        if account_endpoint and stock_code:
            try:
                payload = self._request_json(
                    session,
                    endpoint=account_endpoint,
                    params={
                        **common_params,
                        "stock_code": stock_code,
                        "bsns_year": checked_at.strftime("%Y"),
                        "reprt_code": "11011",
                    },
                )
                items = [item for item in _coerce_list(payload.get("list")) if isinstance(item, dict)]
                source_logs.append(
                    self._source_log(
                        status="checked",
                        note=f"financial_items={len(items)} stock_code={stock_code}",
                        endpoint=account_endpoint,
                        checked_at=checked_at,
                    )
                )
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                detail = self._http_error_detail(exc)
                source_logs.append(
                    self._source_log(
                        status=f"http_{status_code}" if status_code else "http_error",
                        note=detail,
                        endpoint=account_endpoint,
                        checked_at=checked_at,
                        http_status=status_code,
                    )
                )
                coverage_gaps.append(
                    self._gap(
                        gap_type=self._http_gap_type(status_code),
                        detail=detail,
                        endpoint=account_endpoint,
                        http_status=status_code,
                    )
                )
            except requests.RequestException as exc:
                source_logs.append(
                    self._source_log(
                        status="request_error",
                        note=_masked_exception_text(exc),
                        endpoint=account_endpoint,
                        checked_at=checked_at,
                    )
                )
                coverage_gaps.append(self._gap(gap_type="request_error", detail=_masked_exception_text(exc), endpoint=account_endpoint))

        return OfficialCollectionResult(findings=findings, checked_source_log=source_logs, coverage_gaps=coverage_gaps)


class CrisCollector(BaseHanallCollector):
    source_name = "cris"
    source_family = "cris"
    source_group = "trial_registry"

    def _api_key(self) -> str | None:
        value = self.settings.data_go_kr_api_key.strip()
        return value if value and value != "replace_me" else None

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        endpoint = _safe_text(self.endpoints.get("list"))
        if not endpoint:
            return self._empty_result(
                checked_at=checked_at,
                status="invalid_config",
                note="CRIS endpoint missing",
                endpoint="-",
                gap_type="invalid_config",
            )
        try:
            payload = self._request_json(
                session,
                endpoint=endpoint,
                params=self._data_go_kr_params(
                    pageNo=1,
                    numOfRows=10,
                    resultType="json",
                ),
            )
            items = _extract_items(payload)
            stats = _new_parser_stats()
            findings = []
            for item in items[:DEFAULT_RESULT_LIMIT]:
                findings.append(
                    self._build_finding(
                        title=self._pick_text(
                            item,
                            "resrchTaskNm",
                            "title",
                            "taskTitle",
                            field_name="title",
                            default="CRIS item",
                            stats=stats,
                        ),
                        summary=smart_truncate(
                            " | ".join(
                                filter(
                                    None,
                                    [
                                        self._pick_text(item, "resrchInstNm", "researchInstituteName", field_name="entity_name", stats=None),
                                        self._pick_text(item, "resrchPhaseNm", "phase", field_name="phase", stats=None),
                                        self._pick_text(item, "resrchWrd", "keyword", field_name="keyword", stats=None),
                                    ],
                                )
                            ),
                            420,
                        ),
                        published_at=self._pick_datetime(
                            item,
                            "aprvDt",
                            "lastUpdtDt",
                            "approvalDate",
                            field_name="published_at",
                            stats=stats,
                        ),
                        entity=self._pick_text(
                            item,
                            "resrchInstNm",
                            "researchInstituteName",
                            field_name="entity",
                            default="CRIS",
                            stats=stats,
                        ),
                        document_type="clinical_trial",
                        document_id=self._pick_text(item, "crisNo", "taskNo", "id", field_name="document_id", stats=stats),
                        trial_id=self._pick_text(item, "crisNo", "taskNo", "id", field_name="trial_id", stats=None),
                        sponsor=self._pick_text(item, "resrchInstNm", "researchInstituteName", field_name="sponsor", stats=None),
                        phase=self._pick_text(item, "resrchPhaseNm", "phase", field_name="phase", stats=None),
                        recruitment_status=self._pick_text(item, "recruitStatus", field_name="recruitment_status", stats=None),
                        regulator="CRIS",
                        primary_source_url=endpoint,
                        source_note="CRIS OpenAPI",
                        raw_payload=item,
                    )
                )
            self._log_parser_summary(endpoint=endpoint, parsed_count=len(findings), stats=stats, note="CRIS OpenAPI")
            return OfficialCollectionResult(
                findings=findings,
                checked_source_log=[
                    self._source_log(
                        status="checked",
                        note=f"items={len(items)}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                    )
                ],
            )
        except requests.HTTPError as exc:
            return self._http_error_result(checked_at=checked_at, endpoint=endpoint, exc=exc)
        except requests.RequestException as exc:
            return self._empty_result(
                checked_at=checked_at,
                status="request_error",
                note=_masked_exception_text(exc),
                endpoint=endpoint,
                gap_type="request_error",
            )


class MfdsCollector(BaseHanallCollector):
    source_name = "mfds"
    source_family = "mfds"
    source_group = "regulator_disclosure"

    def collect(self, session: requests.Session, *, current_now: datetime | None = None) -> OfficialCollectionResult:
        checked_at = self._current_kst(current_now)
        if not self._is_enabled():
            return self._disabled_result(
                checked_at=checked_at,
                reason="collector disabled by config; approval-gated endpoints remain opt-in",
            )
        if not self._api_key():
            return self._missing_api_key_result(checked_at=checked_at)
        return self._collect(session, checked_at=checked_at)

    def _api_key(self) -> str | None:
        value = self.settings.data_go_kr_api_key.strip()
        return value if value and value != "replace_me" else None

    def _service_enabled(self, service_key: str) -> bool:
        services = self.collector_config.get("services", {})
        if not isinstance(services, dict):
            return False
        return bool(services.get(service_key, False))

    def _service_request_params(self, service_key: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pageNo": 1,
            "numOfRows": MFDS_DEFAULT_NUM_ROWS,
            "type": "json",
        }
        raw_request_params = self.collector_config.get("request_params", {})
        if isinstance(raw_request_params, dict):
            service_params = raw_request_params.get(service_key, {})
            if isinstance(service_params, dict):
                params.update({str(key): value for key, value in service_params.items() if value not in (None, "")})
        return self._data_go_kr_params(**params)

    def _service_result_limit(self, request_params: dict[str, Any]) -> int:
        raw_value = request_params.get("numOfRows", MFDS_DEFAULT_NUM_ROWS)
        try:
            return max(1, min(int(raw_value), 200))
        except (TypeError, ValueError):
            return MFDS_DEFAULT_NUM_ROWS

    def _service_summary(self, service_key: str, item: dict[str, Any]) -> str:
        if service_key == "medicine_clinical_test_info":
            parts = [
                self._pick_text(item, "APPLY_ENTP_NAME", "applyEntpName", field_name="apply_enterprise", stats=None),
                self._pick_text(item, "GOODS_NAME", "goodsName", field_name="goods_name", stats=None),
                self._pick_text(item, "CLINIC_STEP_NAME", "clinicStepName", field_name="clinic_step", stats=None),
                self._pick_text(item, "LAB_NAME", "labName", field_name="lab_name", stats=None),
            ]
        else:
            parts = [
                self._pick_text(item, "ENTRPS", "enterprise", field_name="enterprise", stats=None),
                self._pick_text(item, "INGRED_NAME", "ingredientName", field_name="ingredient", stats=None),
                self._pick_text(item, "INDUTY_TYPE", "industryType", field_name="industry_type", stats=None),
                self._pick_text(item, "MNFDS_KIND", "mfdsKind", field_name="mfds_kind", stats=None),
            ]
        return smart_truncate(" | ".join(filter(None, parts)), 420)

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        findings: list[RawFinding] = []
        source_logs: list[CheckedSourceLogEntry] = []
        coverage_gaps: list[CoverageGap] = []
        for service_key, endpoint in self.endpoints.items():
            endpoint_text = _safe_text(endpoint)
            if not endpoint_text:
                continue
            if not self._service_enabled(service_key):
                source_logs.append(
                    self._source_log(
                        status="approval_gated_disabled",
                        note="dataset disabled in config",
                        endpoint=endpoint_text,
                        checked_at=checked_at,
                    )
                )
                coverage_gaps.append(
                    self._gap(
                        gap_type="approval_gated_disabled",
                        detail=f"{service_key} disabled by config",
                        endpoint=endpoint_text,
                        severity="low",
                    )
                )
                continue
            try:
                request_params = self._service_request_params(service_key)
                service_limit = self._service_result_limit(request_params)
                payload = self._request_json(
                    session,
                    endpoint=endpoint_text,
                    params=request_params,
                )
                items = _extract_items(payload)
                stats = _new_parser_stats()
                for item in items[:service_limit]:
                    if service_key == "medicine_clinical_test_info":
                        title = self._pick_text(
                            item,
                            "CLINIC_EXAM_TITLE",
                            "clinicExamTitle",
                            "GOODS_NAME",
                            "goodsName",
                            field_name="title",
                            default=service_key,
                            stats=stats,
                        )
                        published_at = self._pick_datetime(
                            item,
                            "APPROVAL_TIME",
                            "approvalTime",
                            field_name="published_at",
                            stats=stats,
                        )
                        entity = self._pick_text(
                            item,
                            "APPLY_ENTP_NAME",
                            "applyEntpName",
                            field_name="entity",
                            default="MFDS",
                            stats=stats,
                        )
                        document_id = self._pick_text(
                            item,
                            "CLNC_TEST_SN",
                            "clncTestSn",
                            field_name="document_id",
                            stats=stats,
                        )
                        trial_id = self._pick_text(
                            item,
                            "CLNC_TEST_SN",
                            "clncTestSn",
                            field_name="trial_id",
                            stats=None,
                        )
                        asset = self._pick_text(
                            item,
                            "GOODS_NAME",
                            "goodsName",
                            field_name="asset",
                            stats=None,
                        )
                        document_type = "clinical_trial"
                    else:
                        title = self._pick_text(
                            item,
                            "ITEM_NAME",
                            "PRDUCT",
                            "title",
                            "itemName",
                            field_name="title",
                            default=service_key,
                            stats=stats,
                        )
                        published_at = self._pick_datetime(
                            item,
                            "APPLY_DE",
                            "UPDATE_DE",
                            "REGIST_DE",
                            "applyDate",
                            field_name="published_at",
                            stats=stats,
                        )
                        entity = self._pick_text(
                            item,
                            "ENTRPS",
                            "enterprise",
                            field_name="entity",
                            default="MFDS",
                            stats=stats,
                        )
                        document_id = self._pick_text(
                            item,
                            "ITEM_SEQ",
                            "PRDUCT_ID",
                            "ID",
                            "itemSeq",
                            field_name="document_id",
                            stats=stats,
                        )
                        trial_id = None
                        asset = None
                        document_type = "mfds_dataset_item"
                    findings.append(
                        self._build_finding(
                            title=title,
                            summary=self._service_summary(service_key, item),
                            published_at=published_at,
                            entity=entity,
                            document_type=document_type,
                            document_id=document_id,
                            trial_id=trial_id,
                            asset=asset,
                            sponsor=entity if document_type == "clinical_trial" else None,
                            phase=self._pick_text(
                                item,
                                "CLINIC_STEP_NAME",
                                "clinicStepName",
                                field_name="phase",
                                stats=None,
                            )
                            if document_type == "clinical_trial"
                            else None,
                            regulator="MFDS",
                            primary_source_url=endpoint_text,
                            source_note=f"mfds_service={service_key}",
                            raw_payload=item,
                        )
                    )
                self._log_parser_summary(
                    endpoint=endpoint_text,
                    parsed_count=min(service_limit, len(items)),
                    stats=stats,
                    note=f"service={service_key}",
                )
                source_logs.append(
                    self._source_log(
                        status="checked",
                        note=f"service={service_key} items={len(items)}",
                        endpoint=endpoint_text,
                        checked_at=checked_at,
                    )
                )
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                detail = self._http_error_detail(exc)
                source_logs.append(
                    self._source_log(
                        status=f"http_{status_code}" if status_code else "http_error",
                        note=f"service={service_key} error={detail}",
                        endpoint=endpoint_text,
                        checked_at=checked_at,
                        http_status=status_code,
                    )
                )
                coverage_gaps.append(
                    self._gap(
                        gap_type=self._http_gap_type(status_code),
                        detail=f"{service_key}: {detail}",
                        endpoint=endpoint_text,
                        http_status=status_code,
                    )
                )
            except requests.RequestException as exc:
                source_logs.append(
                    self._source_log(
                        status="request_error",
                        note=f"service={service_key} error={exc}",
                        endpoint=endpoint_text,
                        checked_at=checked_at,
                    )
                )
                coverage_gaps.append(
                    self._gap(
                        gap_type="request_error",
                        detail=f"{service_key}: {exc}",
                        endpoint=endpoint_text,
                    )
                )
        return OfficialCollectionResult(findings=findings, checked_source_log=source_logs, coverage_gaps=coverage_gaps)


class OpenFdaCollector(BaseHanallCollector):
    source_name = "openfda"
    source_family = "openfda"
    source_group = "regulator_disclosure"

    def _api_key(self) -> str | None:
        value = self.settings.openfda_api_key.strip()
        return value if value and value != "replace_me" else None

    def _request_openfda_json(
        self,
        session: requests.Session,
        *,
        endpoint: str,
        params: dict[str, Any],
    ) -> tuple[Any, str]:
        api_key = self._api_key()
        if api_key:
            try:
                return (
                    self._request_json(session, endpoint=endpoint, params={**params, "api_key": api_key}),
                    "with_key",
                )
            except requests.HTTPError as exc:
                if _is_auth_invalid_response(exc.response, source_name=self.source_name):
                    payload = self._request_json(session, endpoint=endpoint, params=params)
                    return payload, "no_key_fallback"
                raise
        return self._request_json(session, endpoint=endpoint, params=params), "no_key"

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        findings: list[RawFinding] = []
        source_logs: list[CheckedSourceLogEntry] = []
        coverage_gaps: list[CoverageGap] = []
        search_term = PRIMARY_QUERY.replace('"', "")

        for endpoint_key in ("drugsfda", "label", "event", "enforcement", "shortages", "crl"):
            endpoint = _safe_text(self.endpoints.get(endpoint_key))
            if not endpoint:
                continue
            params: dict[str, Any] = {"search": search_term, "limit": 5}
            try:
                payload, auth_mode = self._request_openfda_json(session, endpoint=endpoint, params=params)
                results = _extract_items(payload)
                stats = _new_parser_stats()
                for item in results[:5]:
                    brand_names = self._pick_text(
                        item,
                        "openfda.brand_name",
                        "brand_name",
                        field_name="brand_name",
                        stats=None,
                    )
                    generic_names = self._pick_text(
                        item,
                        "openfda.generic_name",
                        "generic_name",
                        field_name="generic_name",
                        stats=None,
                    )
                    title = self._pick_text(
                        item,
                        "purpose",
                        "reason_for_recall",
                        "classification",
                        "submission_type",
                        "application_number",
                        "openfda.brand_name.0",
                        "products.0.brand_name",
                        "id",
                        field_name="title",
                        default=f"openFDA {endpoint_key}",
                        stats=stats,
                    )
                    summary = smart_truncate(
                        " | ".join(
                            filter(
                                None,
                                [
                                    self._pick_text(item, "sponsor_name", "openfda.manufacturer_name.0", "manufacturer_name", field_name="sponsor_name", stats=None),
                                    self._pick_text(item, "applicant", field_name="applicant", stats=None),
                                    self._pick_text(item, "product_description", "description", field_name="product_description", stats=None),
                                    self._pick_text(item, "report_number", "recall_number", field_name="report_number", stats=None),
                                    generic_names,
                                    brand_names,
                                ],
                            )
                        ),
                        420,
                    )
                    findings.append(
                        self._build_finding(
                            title=title,
                            summary=summary,
                            published_at=self._pick_datetime(
                                item,
                                "effective_time",
                                "publication_date",
                                "report_date",
                                "event_date",
                                "date",
                                field_name="published_at",
                                stats=stats,
                            ),
                            updated_at=self._pick_datetime(item, "report_date", "date", field_name="updated_at", stats=stats),
                            entity=self._pick_text(
                                item,
                                "sponsor_name",
                                "manufacturer_name",
                                "openfda.manufacturer_name.0",
                                "applicant",
                                field_name="entity",
                                default="openFDA",
                                stats=stats,
                            ),
                            document_type="openfda_record",
                            document_id=self._pick_text(
                                item,
                                "id",
                                "application_number",
                                "report_number",
                                field_name="document_id",
                                stats=stats,
                            ),
                            regulator="FDA",
                            event_action=self._pick_text(
                                item,
                                "classification",
                                "reason_for_recall",
                                "submission_type",
                                field_name="event_action",
                                stats=None,
                            ),
                            key_numbers=[
                                value
                                for value in [
                                    self._pick_text(item, "application_number", field_name="application_number", stats=None),
                                    self._pick_text(item, "report_number", "recall_number", field_name="report_number", stats=None),
                                ]
                                if value
                            ],
                            primary_source_url=endpoint,
                            source_note=f"endpoint={endpoint_key}",
                            asset=(
                                "batoclimab"
                                if "batoclimab" in summary.lower() or "batoclimab" in title.lower() or "batoclimab" in generic_names.lower()
                                else None
                            ),
                            target_moa=_infer_target_moa(title, summary, generic_names, brand_names),
                            raw_payload=item if isinstance(item, dict) else {},
                        )
                    )
                self._log_parser_summary(
                    endpoint=endpoint,
                    parsed_count=min(5, len(results)),
                    stats=stats,
                    note=f"endpoint={endpoint_key} auth={auth_mode}",
                )
                source_logs.append(
                    self._source_log(
                        status="checked",
                        note=f"endpoint={endpoint_key} items={len(results)} auth={auth_mode}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                    )
                )
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if _is_openfda_no_match_response(exc.response):
                    source_logs.append(
                        self._source_log(
                            status="checked",
                            note=f"endpoint={endpoint_key} no_match=1",
                            endpoint=endpoint,
                            checked_at=checked_at,
                            http_status=status_code,
                        )
                    )
                    continue
                detail = self._http_error_detail(exc)
                logger.warning("openfda collector http error endpoint=%s status=%s error=%s", endpoint_key, status_code, detail)
                coverage_gaps.append(
                    self._gap(
                        gap_type=self._http_gap_type(status_code),
                        detail=detail,
                        endpoint=endpoint,
                        http_status=status_code,
                    )
                )
                source_logs.append(
                    self._source_log(
                        status=f"http_{status_code}" if status_code else "http_error",
                        note=f"endpoint={endpoint_key} error={detail}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                        http_status=status_code,
                    )
                )
            except requests.RequestException as exc:
                logger.warning("openfda collector request failed endpoint=%s error=%s", endpoint_key, _masked_exception_text(exc))
                coverage_gaps.append(
                    self._gap(
                        gap_type="request_error",
                        detail=_masked_exception_text(exc),
                        endpoint=endpoint,
                    )
                )
                source_logs.append(
                    self._source_log(
                        status="request_error",
                        note=f"endpoint={endpoint_key} error={exc}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                    )
                )

        return OfficialCollectionResult(findings=findings, checked_source_log=source_logs, coverage_gaps=coverage_gaps)


class ClinicalTrialsCollector(BaseHanallCollector):
    source_name = "clinicaltrials"
    source_family = "clinicaltrials"
    source_group = "trial_registry"

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        endpoint = _safe_text(self.endpoints.get("studies"))
        if not endpoint:
            return self._empty_result(
                checked_at=checked_at,
                status="invalid_config",
                note="studies endpoint missing",
                endpoint="-",
                gap_type="invalid_config",
                severity="high",
            )

        try:
            payload = self._request_json(
                session,
                endpoint=endpoint,
                params={
                    "query.term": PRIMARY_QUERY,
                    "pageSize": 10,
                    "sort": "LastUpdatePostDate:desc",
                },
            )
            studies = payload.get("studies", []) if isinstance(payload, dict) else []
            findings: list[RawFinding] = []
            source_logs: list[CheckedSourceLogEntry] = []
            coverage_gaps: list[CoverageGap] = []
            detail_endpoint_template = _safe_text(self.endpoints.get("study_by_id"))
            stats = _new_parser_stats()
            for item in studies[:10]:
                protocol = _ensure_dict(item.get("protocolSection") if isinstance(item, dict) else None)
                identification = _ensure_dict(protocol.get("identificationModule"))
                status = _ensure_dict(protocol.get("statusModule"))
                conditions = _ensure_dict(protocol.get("conditionsModule"))
                interventions = _ensure_dict(protocol.get("armsInterventionsModule"))
                design = _ensure_dict(protocol.get("designModule"))
                contacts_locations = _ensure_dict(protocol.get("contactsLocationsModule"))
                sponsor_module = _ensure_dict(protocol.get("sponsorCollaboratorsModule"))
                title = self._pick_text(
                    identification,
                    "officialTitle",
                    "briefTitle",
                    field_name="title",
                    default="ClinicalTrials.gov study",
                    stats=stats,
                )
                summary = smart_truncate(
                    " | ".join(
                        filter(
                            None,
                            [
                                self._pick_text(status, "overallStatus", field_name="overall_status", stats=None),
                                self._pick_text(status, "lastUpdatePostDateStruct.date", "lastUpdatePostDate", field_name="last_update", stats=None),
                                self._pick_text(status, "studyFirstPostDateStruct.date", "studyFirstPostDate", field_name="first_post_date", stats=None),
                                self._pick_text(conditions, "conditions", field_name="conditions", stats=None),
                            ],
                        )
                    ),
                    420,
                )
                nct_id = self._pick_text(identification, "nctId", field_name="nct_id", stats=stats)
                intervention_name = None
                interventions_list = interventions.get("interventions", []) if isinstance(interventions, dict) else []
                if interventions_list and isinstance(interventions_list[0], dict):
                    intervention_name = self._pick_text(interventions_list[0], "name", field_name="asset", stats=None)
                sponsor_name = self._pick_text(
                    sponsor_module,
                    "leadSponsor.name",
                    field_name="sponsor",
                    default=self._pick_text(identification, "organization.fullName", field_name="organization_name", stats=None) or "ClinicalTrials.gov",
                    stats=stats,
                )
                phase = self._pick_text(
                    design,
                    "phases.0",
                    "phaseList.phase.0",
                    "phaseList.phase",
                    field_name="phase",
                    stats=None,
                )
                enrollment = self._pick_text(
                    design,
                    "enrollmentInfo.count",
                    "enrollmentInfo.enrollmentCount",
                    field_name="enrollment",
                    stats=None,
                )
                site_countries = _stringify_values(_deep_get(contacts_locations, "locations.country"))
                findings.append(
                    self._build_finding(
                        title=title,
                        summary=summary,
                        published_at=self._pick_datetime(
                            status,
                            "studyFirstPostDateStruct.date",
                            "studyFirstPostDate",
                            field_name="published_at",
                            stats=stats,
                        ),
                        updated_at=self._pick_datetime(
                            status,
                            "lastUpdatePostDateStruct.date",
                            "lastUpdatePostDate",
                            field_name="updated_at",
                            stats=stats,
                        ),
                        entity=self._pick_text(
                            identification,
                            "organization.fullName",
                            field_name="entity",
                            default=self._pick_text(protocol, "sponsorCollaboratorsModule.leadSponsor.name", field_name="lead_sponsor", stats=None) or "ClinicalTrials.gov",
                            stats=stats,
                        ),
                        document_type="clinical_trial",
                        document_id=nct_id,
                        trial_id=nct_id,
                        asset=intervention_name,
                        aliases=[intervention_name] if intervention_name else [],
                        sponsor=sponsor_name,
                        target_moa=_infer_target_moa(title, summary, intervention_name),
                        indication=self._pick_text(conditions, "conditions", field_name="indication", stats=None),
                        phase=phase,
                        recruitment_status=self._pick_text(status, "overallStatus", field_name="recruitment_status", stats=None),
                        enrollment=enrollment,
                        primary_completion_date=self._pick_text(
                            status,
                            "primaryCompletionDateStruct.date",
                            "primaryCompletionDate",
                            field_name="primary_completion_date",
                            stats=None,
                        ),
                        last_update_posted=self._pick_text(
                            status,
                            "lastUpdatePostDateStruct.date",
                            "lastUpdatePostDate",
                            field_name="last_update_posted",
                            stats=None,
                        ),
                        site_countries=site_countries,
                        regulator="ClinicalTrials.gov",
                        primary_source_url=f"{endpoint}/{nct_id}" if nct_id else endpoint,
                        source_note="ClinicalTrials.gov v2 study result",
                        raw_payload=item if isinstance(item, dict) else {},
                    )
                )
            if detail_endpoint_template:
                detail_checked = 0
                for finding in findings[:2]:
                    if not finding.trial_id:
                        continue
                    detail_endpoint = detail_endpoint_template.replace("{NCT_ID}", finding.trial_id)
                    try:
                        self._request_json(session, endpoint=detail_endpoint)
                        detail_checked += 1
                    except requests.RequestException as exc:
                        logger.warning(
                            "clinicaltrials detail fetch failed trial_id=%s error=%s",
                            finding.trial_id,
                            _masked_exception_text(exc),
                        )
                        status_code = exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response is not None else None
                        coverage_gaps.append(
                            self._gap(
                                gap_type=self._http_gap_type(status_code) if status_code else "request_error",
                                detail=f"{finding.trial_id}: {exc}",
                                endpoint=detail_endpoint,
                                http_status=status_code,
                            )
                        )
            source_logs.append(
                self._source_log(
                    status="checked",
                    note=f"items={len(studies)} detail_checked={min(2, len(findings)) if detail_endpoint_template else 0}",
                    endpoint=endpoint,
                    checked_at=checked_at,
                )
            )
            self._log_parser_summary(
                endpoint=endpoint,
                parsed_count=len(findings),
                stats=stats,
                note="ClinicalTrials.gov v2 studies",
            )
            return OfficialCollectionResult(
                findings=findings,
                checked_source_log=source_logs,
                coverage_gaps=coverage_gaps,
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            return self._empty_result(
                checked_at=checked_at,
                status=f"http_{status_code}" if status_code else "http_error",
                note=_masked_exception_text(exc),
                endpoint=endpoint,
                gap_type=self._http_gap_type(status_code),
                http_status=status_code,
            )
        except requests.RequestException as exc:
            return self._empty_result(
                checked_at=checked_at,
                status="request_error",
                note=_masked_exception_text(exc),
                endpoint=endpoint,
                gap_type="request_error",
            )


class NcbiCollector(BaseHanallCollector):
    source_name = "ncbi"
    source_family = "ncbi"
    source_group = "discovery_only"

    def _api_key(self) -> str | None:
        value = self.settings.ncbi_api_key.strip()
        return value if value and value != "replace_me" else None

    def _ncbi_contact_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if _safe_text(getattr(self.settings, "ncbi_tool_name", "")):
            params["tool"] = _safe_text(self.settings.ncbi_tool_name)
        if _safe_text(getattr(self.settings, "ncbi_email", "")):
            params["email"] = _safe_text(self.settings.ncbi_email)
        return params

    def _warn_missing_ncbi_contact(self, state: dict[str, Any]) -> None:
        if state.get("warned_missing_contact"):
            return
        tool_present = bool(_safe_text(getattr(self.settings, "ncbi_tool_name", "")))
        email_present = bool(_safe_text(getattr(self.settings, "ncbi_email", "")))
        if not tool_present or not email_present:
            logger.warning("ncbi request metadata missing tool_present=%s email_present=%s", tool_present, email_present)
        state["warned_missing_contact"] = True

    def _request_ncbi_json(
        self,
        session: requests.Session,
        *,
        endpoint: str,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[Any, str]:
        self._warn_missing_ncbi_contact(state)
        base_params = {**params, **self._ncbi_contact_params()}
        api_key = self._api_key()
        if api_key and not state.get("force_no_key"):
            try:
                payload = self._request_json(session, endpoint=endpoint, params={**base_params, "api_key": api_key})
                state["auth_mode"] = "with_key"
                return payload, "with_key"
            except requests.HTTPError as exc:
                if _is_auth_invalid_response(exc.response, source_name=self.source_name):
                    logger.warning("ncbi api key invalid; retrying without api_key endpoint=%s", endpoint)
                    state["force_no_key"] = True
                    state["auth_mode"] = "forced_no_key"
                    payload = self._request_json(session, endpoint=endpoint, params=base_params)
                    return payload, "forced_no_key"
                raise
        auth_mode = "forced_no_key" if state.get("force_no_key") and api_key else "no_key"
        state["auth_mode"] = auth_mode
        return self._request_json(session, endpoint=endpoint, params=base_params), auth_mode

    def _request_ncbi_text(
        self,
        session: requests.Session,
        *,
        endpoint: str,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[str, str]:
        self._warn_missing_ncbi_contact(state)
        base_params = {**params, **self._ncbi_contact_params()}
        api_key = self._api_key()
        if api_key and not state.get("force_no_key"):
            try:
                text = self._request_text(session, endpoint=endpoint, params={**base_params, "api_key": api_key})
                state["auth_mode"] = "with_key"
                return text, "with_key"
            except requests.HTTPError as exc:
                if _is_auth_invalid_response(exc.response, source_name=self.source_name):
                    logger.warning("ncbi api key invalid; retrying without api_key endpoint=%s", endpoint)
                    state["force_no_key"] = True
                    state["auth_mode"] = "forced_no_key"
                    text = self._request_text(session, endpoint=endpoint, params=base_params)
                    return text, "forced_no_key"
                raise
        auth_mode = "forced_no_key" if state.get("force_no_key") and api_key else "no_key"
        state["auth_mode"] = auth_mode
        return self._request_text(session, endpoint=endpoint, params=base_params), auth_mode

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        esearch_endpoint = _safe_text(self.endpoints.get("esearch"))
        esummary_endpoint = _safe_text(self.endpoints.get("esummary"))
        if not esearch_endpoint or not esummary_endpoint:
            return self._empty_result(
                checked_at=checked_at,
                status="invalid_config",
                note="ncbi endpoints missing",
                endpoint="-",
                gap_type="invalid_config",
                severity="high",
            )

        search_params: dict[str, Any] = {
            "db": "pubmed",
            "term": PRIMARY_QUERY,
            "retmode": "json",
            "retmax": 5,
            "sort": "pub_date",
        }
        state: dict[str, Any] = {
            "force_no_key": False,
            "auth_mode": "with_key" if self._api_key() else "no_key",
            "warned_missing_contact": False,
        }
        try:
            search_payload, _ = self._request_ncbi_json(session, endpoint=esearch_endpoint, params=search_params, state=state)
            raw_id_list = search_payload.get("esearchresult", {}).get("idlist", []) if isinstance(search_payload, dict) else []
            id_list = [str(identifier) for identifier in _coerce_list(raw_id_list) if _safe_text(identifier)]
            findings: list[RawFinding] = []
            stats = _new_parser_stats()
            if id_list:
                summary_params: dict[str, Any] = {
                    "db": "pubmed",
                    "id": ",".join(str(identifier) for identifier in id_list[:5]),
                    "retmode": "json",
                }
                summary_payload, _ = self._request_ncbi_json(session, endpoint=esummary_endpoint, params=summary_params, state=state)
                result_block = summary_payload.get("result", {}) if isinstance(summary_payload, dict) else {}
                for pubmed_id in id_list[:5]:
                    item = result_block.get(str(pubmed_id), {}) if isinstance(result_block, dict) else {}
                    title = self._pick_text(item, "title", field_name="title", default=f"PubMed {pubmed_id}", stats=stats)
                    first_author = self._pick_text(
                        item,
                        "authors.0.name",
                        "sortfirstauthor",
                        field_name="first_author",
                        stats=stats,
                    )
                    summary = smart_truncate(
                        " | ".join(
                            filter(
                                None,
                                [
                                    self._pick_text(item, "fulljournalname", "source", field_name="journal_name", stats=None),
                                    self._pick_text(item, "pubdate", "epubdate", field_name="pubdate_raw", stats=None),
                                    first_author,
                                ],
                            )
                        ),
                        420,
                    )
                    findings.append(
                        self._build_finding(
                            title=title,
                            summary=summary,
                            published_at=self._pick_datetime(item, "pubdate", "epubdate", field_name="published_at", stats=stats),
                            entity=first_author or "PubMed",
                            document_type="publication",
                            document_id=str(pubmed_id),
                            primary_source_url=f"https://pubmed.ncbi.nlm.nih.gov/{pubmed_id}/",
                            source_note="NCBI E-utilities esummary",
                            raw_payload=item if isinstance(item, dict) else {},
                        )
                    )
                efetch_endpoint = _safe_text(self.endpoints.get("efetch"))
                elink_endpoint = _safe_text(self.endpoints.get("elink"))
                if efetch_endpoint:
                    try:
                        self._request_ncbi_text(
                            session,
                            endpoint=efetch_endpoint,
                            params={
                                "db": "pubmed",
                                "id": ",".join(str(identifier) for identifier in id_list[:3]),
                                "retmode": "xml",
                            },
                            state=state,
                        )
                    except requests.RequestException as exc:
                        logger.warning("ncbi efetch failed error=%s", _masked_exception_text(exc))
                if elink_endpoint:
                    try:
                        self._request_ncbi_json(
                            session,
                            endpoint=elink_endpoint,
                            params={
                                "dbfrom": "pubmed",
                                "id": ",".join(str(identifier) for identifier in id_list[:3]),
                                "retmode": "json",
                            },
                            state=state,
                        )
                    except requests.RequestException as exc:
                        logger.warning("ncbi elink failed error=%s", _masked_exception_text(exc))
            self._log_parser_summary(
                endpoint=esummary_endpoint,
                parsed_count=len(findings),
                stats=stats,
                note=f"pubmed_ids={len(id_list)} auth_mode={state['auth_mode']}",
            )
            return OfficialCollectionResult(
                findings=findings,
                checked_source_log=[
                    self._source_log(
                        status="checked",
                        note=f"pubmed_ids={len(id_list)} endpoints=esearch,esummary,efetch,elink auth_mode={state['auth_mode']}",
                        endpoint=esearch_endpoint,
                        checked_at=checked_at,
                    )
                ],
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            return self._empty_result(
                checked_at=checked_at,
                status=f"http_{status_code}" if status_code else "http_error",
                note=_masked_exception_text(exc),
                endpoint=esearch_endpoint,
                gap_type=self._http_gap_type(status_code),
                http_status=status_code,
            )
        except requests.RequestException as exc:
            return self._empty_result(
                checked_at=checked_at,
                status="request_error",
                note=_masked_exception_text(exc),
                endpoint=esearch_endpoint,
                gap_type="request_error",
            )


class EuropePmcCollector(BaseHanallCollector):
    source_name = "europe_pmc"
    source_family = "europe_pmc"
    source_group = "discovery_only"

    def _europe_pmc_params(self, query: str, *, page_size: int = 5) -> dict[str, Any]:
        return {
            "query": query,
            "format": "json",
            "resultType": "lite",
            "pageSize": page_size,
        }

    def _search_europe_pmc(
        self,
        session: requests.Session,
        *,
        endpoint: str,
        query: str,
        page_size: int = 5,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            session,
            method="GET",
            endpoint=endpoint,
            params=self._europe_pmc_params(query, page_size=page_size),
        )
        return _extract_items(payload)

    def _dedupe_europe_pmc_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            key = "|".join(
                filter(
                    None,
                    [
                        _first_text(item, "title"),
                        _first_text(item, "doi"),
                        _first_text(item, "pmid"),
                        _first_text(item, "source"),
                        _first_text(item, "id"),
                    ],
                )
            )
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        endpoint = _safe_text(self.endpoints.get("search"))
        if not endpoint:
            return self._empty_result(
                checked_at=checked_at,
                status="invalid_config",
                note="europe pmc endpoint missing",
                endpoint="-",
                gap_type="invalid_config",
            )
        try:
            probe_results = self._search_europe_pmc(session, endpoint=endpoint, query="Immunovant", page_size=5)
            strategy = "canonical_or"
            canonical_query = f'{" OR ".join(EUROPE_PMC_CANONICAL_TERMS)} sort_date:y'
            try:
                results = self._search_europe_pmc(session, endpoint=endpoint, query=canonical_query, page_size=5)
            except requests.RequestException:
                strategy = "split_terms"
                results = []
            if not results:
                strategy = "split_terms"
                split_results: list[dict[str, Any]] = []
                for term in EUROPE_PMC_CANONICAL_TERMS:
                    split_results.extend(
                        self._search_europe_pmc(session, endpoint=endpoint, query=f"{term} sort_date:y", page_size=5)
                    )
                results = self._dedupe_europe_pmc_items(split_results)
            stats = _new_parser_stats()
            findings = []
            for item in results[:5]:
                doi = self._pick_text(item, "doi", field_name="doi", stats=None)
                document_id = self._pick_text(item, "id", "pmcid", "pmid", field_name="document_id", stats=stats)
                primary_source_url = f"https://doi.org/{doi}" if doi else f"https://europepmc.org/article/{self._pick_text(item, 'source', field_name='source', stats=None) or 'MED'}/{document_id}" if document_id else endpoint
                findings.append(
                    self._build_finding(
                        title=self._pick_text(item, "title", field_name="title", default="Europe PMC result", stats=stats),
                        summary=smart_truncate(
                            " | ".join(
                                filter(
                                    None,
                                    [
                                        self._pick_text(item, "journalTitle", field_name="journal_title", stats=None),
                                        self._pick_text(item, "pubYear", field_name="pub_year", stats=None),
                                        self._pick_text(item, "authorString", field_name="author_string", stats=None),
                                    ],
                                )
                            ),
                            420,
                        ),
                        published_at=self._pick_datetime(
                            item,
                            "firstPublicationDate",
                            "dateOfCreation",
                            "electronicPublicationDate",
                            field_name="published_at",
                            stats=stats,
                        ),
                        entity=self._pick_text(item, "authorString", field_name="entity", default="Europe PMC", stats=stats),
                        document_type="publication",
                        document_id=document_id,
                        primary_source_url=primary_source_url,
                        source_note="Europe PMC search",
                        raw_payload=item if isinstance(item, dict) else {},
                    )
                )
            self._log_parser_summary(
                endpoint=endpoint,
                parsed_count=len(findings),
                stats=stats,
                note=f"probe_items={len(probe_results)} strategy={strategy}",
            )
            return OfficialCollectionResult(
                findings=findings,
                checked_source_log=[
                    self._source_log(
                        status="checked",
                        note=f"probe_query=Immunovant probe_items={len(probe_results)} strategy={strategy} items={len(results)}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                    )
                ],
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            return self._empty_result(
                checked_at=checked_at,
                status=f"http_{status_code}" if status_code else "http_error",
                note=_masked_exception_text(exc),
                endpoint=endpoint,
                gap_type=self._http_gap_type(status_code),
                http_status=status_code,
            )
        except requests.RequestException as exc:
            return self._empty_result(
                checked_at=checked_at,
                status="request_error",
                note=_masked_exception_text(exc),
                endpoint=endpoint,
                gap_type="request_error",
            )


class CrossrefCollector(BaseHanallCollector):
    source_name = "crossref"
    source_family = "crossref"
    source_group = "discovery_only"

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        endpoint = _safe_text(self.endpoints.get("works"))
        if not endpoint:
            return self._empty_result(
                checked_at=checked_at,
                status="invalid_config",
                note="crossref works endpoint missing",
                endpoint="-",
                gap_type="invalid_config",
            )
        try:
            payload = self._request_json(
                session,
                endpoint=endpoint,
                params={"query": PRIMARY_QUERY.replace('"', ""), "rows": 5},
            )
            items = _extract_items(payload)
            findings = []
            stats = _new_parser_stats()
            for item in items[:5]:
                title = self._pick_text(item, "title.0", "title", field_name="title", default="Crossref result", stats=stats)
                container = self._pick_text(item, "container-title.0", "container-title", field_name="container_title", stats=None)
                doi = self._pick_text(item, "DOI", field_name="doi", stats=stats)
                summary = smart_truncate(
                    " | ".join(
                        filter(
                            None,
                            [
                                container,
                                self._pick_text(item, "published-print.date-parts.0.0", "published-online.date-parts.0.0", "issued.date-parts.0.0", field_name="pub_year", stats=None),
                                doi,
                            ],
                        )
                    ),
                    420,
                )
                findings.append(
                    self._build_finding(
                        title=title,
                        summary=summary,
                        published_at=_date_parts_to_datetime(item.get("published-print"))
                        or _date_parts_to_datetime(item.get("published-online"))
                        or _date_parts_to_datetime(item.get("issued"))
                        or _date_parts_to_datetime(item.get("published"))
                        or _date_parts_to_datetime(item.get("created")),
                        entity="Crossref",
                        document_type="publication",
                        document_id=doi,
                        primary_source_url=f"https://doi.org/{doi}" if doi else endpoint,
                        source_note="Crossref works",
                            raw_payload=item if isinstance(item, dict) else {},
                    )
                )
            self._log_parser_summary(endpoint=endpoint, parsed_count=len(findings), stats=stats, note="Crossref works")
            detail_endpoint_template = _safe_text(self.endpoints.get("work_by_doi"))
            if detail_endpoint_template and items:
                first_doi = self._pick_text(items[0], "DOI", field_name="first_doi", stats=None)
                if first_doi:
                    detail_endpoint = detail_endpoint_template.replace("{doi}", first_doi)
                    try:
                        self._request_json(session, endpoint=detail_endpoint)
                    except requests.RequestException as exc:
                        logger.warning("crossref detail fetch failed doi=%s error=%s", first_doi, _masked_exception_text(exc))
            return OfficialCollectionResult(
                findings=findings,
                checked_source_log=[
                    self._source_log(
                        status="checked",
                        note=f"items={len(items)}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                    )
                ],
            )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            return self._empty_result(
                checked_at=checked_at,
                status=f"http_{status_code}" if status_code else "http_error",
                note=_masked_exception_text(exc),
                endpoint=endpoint,
                gap_type=self._http_gap_type(status_code),
                http_status=status_code,
            )
        except requests.RequestException as exc:
            return self._empty_result(
                checked_at=checked_at,
                status="request_error",
                note=_masked_exception_text(exc),
                endpoint=endpoint,
                gap_type="request_error",
            )


class BiorxivCollector(BaseHanallCollector):
    source_name = "biorxiv"
    source_family = "biorxiv"
    source_group = "discovery_only"

    def _collect(self, session: requests.Session, *, checked_at: datetime) -> OfficialCollectionResult:
        template = _safe_text(self.endpoints.get("details_interval"))
        if not template:
            return self._empty_result(
                checked_at=checked_at,
                status="invalid_config",
                note="biorxiv interval endpoint missing",
                endpoint="-",
                gap_type="invalid_config",
            )

        findings: list[RawFinding] = []
        source_logs: list[CheckedSourceLogEntry] = []
        coverage_gaps: list[CoverageGap] = []
        interval = f"{(checked_at - timedelta(days=1)).strftime('%Y-%m-%d')}/{checked_at.strftime('%Y-%m-%d')}"
        for server_name in ("biorxiv", "medrxiv"):
            endpoint = template.replace("[server]", server_name).replace("[interval]", interval).replace("[cursor]", "0").replace("[format]", "json")
            try:
                payload = self._request_json(session, endpoint=endpoint)
                collection = _extract_items(payload)
                stats = _new_parser_stats()
                for item in collection[:5]:
                    title = self._pick_text(item, "title", field_name="title", default=f"{server_name} preprint", stats=stats)
                    summary = smart_truncate(
                        " | ".join(
                            filter(
                                None,
                                [
                                    self._pick_text(item, "authors", "author_corresponding", field_name="authors", stats=None),
                                    self._pick_text(item, "category", field_name="category", stats=None),
                                    self._pick_text(item, "date", field_name="date_raw", stats=None),
                                ],
                            )
                        ),
                        420,
                    )
                    doi = self._pick_text(item, "doi", field_name="doi", stats=stats)
                    findings.append(
                        self._build_finding(
                            title=title,
                            summary=summary,
                            published_at=self._pick_datetime(item, "date", field_name="published_at", stats=stats),
                            entity=self._pick_text(item, "authors", "author_corresponding", field_name="entity", default=server_name, stats=stats),
                            document_type="preprint",
                            document_id=doi,
                            primary_source_url=f"https://doi.org/{doi}" if doi else endpoint,
                            source_note=f"{server_name} preprint feed",
                            raw_payload=item if isinstance(item, dict) else {},
                        )
                    )
                source_logs.append(
                    self._source_log(
                        status="checked",
                        note=f"server={server_name} items={len(collection)}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                    )
                )
                self._log_parser_summary(
                    endpoint=endpoint,
                    parsed_count=min(5, len(collection)),
                    stats=stats,
                    note=f"server={server_name}",
                )
                pubs_template = _safe_text(self.endpoints.get("pubs_interval"))
                if pubs_template:
                    pubs_endpoint = (
                        pubs_template.replace("[server]", server_name)
                        .replace("[interval]", interval)
                        .replace("[cursor]", "0")
                    )
                    try:
                        self._request_json(session, endpoint=pubs_endpoint)
                    except requests.RequestException as exc:
                        logger.warning("biorxiv pubs fetch failed server=%s error=%s", server_name, _masked_exception_text(exc))
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                coverage_gaps.append(
                    self._gap(
                        gap_type=self._http_gap_type(status_code),
                        detail=_masked_exception_text(exc),
                        endpoint=endpoint,
                        http_status=status_code,
                    )
                )
                source_logs.append(
                    self._source_log(
                        status=f"http_{status_code}" if status_code else "http_error",
                        note=f"server={server_name} error={exc}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                        http_status=status_code,
                    )
                )
            except requests.RequestException as exc:
                coverage_gaps.append(
                    self._gap(
                        gap_type="request_error",
                        detail=_masked_exception_text(exc),
                        endpoint=endpoint,
                    )
                )
                source_logs.append(
                    self._source_log(
                        status="request_error",
                        note=f"server={server_name} error={exc}",
                        endpoint=endpoint,
                        checked_at=checked_at,
                    )
                )
        return OfficialCollectionResult(findings=findings, checked_source_log=source_logs, coverage_gaps=coverage_gaps)


def _page_check_latest_item(
    *,
    html_text: str,
    base_url: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
) -> tuple[str | None, str | None]:
    include = [keyword.lower() for keyword in include_keywords if _safe_text(keyword)]
    exclude = [keyword.lower() for keyword in exclude_keywords if _safe_text(keyword)]
    for label, href in _extract_anchor_items(html_text, base_url):
        lowered = f"{label} {href}".lower()
        if include and not any(keyword in lowered for keyword in include):
            continue
        if exclude and any(keyword in lowered for keyword in exclude):
            continue
        if len(_safe_text(label)) < 4:
            continue
        return label, href
    visible_lines = _extract_visible_lines(html_text)
    for line in visible_lines:
        lowered = line.lower()
        if include and not any(keyword in lowered for keyword in include):
            continue
        if exclude and any(keyword in lowered for keyword in exclude):
            continue
        if len(line) < 8:
            continue
        return line, base_url
    return None, None


def _page_check_access_restriction(html_text: str) -> str | None:
    lowered = html_text.lower()
    if "access denied" in lowered or "forbidden" in lowered:
        return "access_denied"
    if "sign in" in lowered or "log in" in lowered or "login" in lowered:
        return "login_wall"
    if "robots" in lowered and "disallow" in lowered:
        return "robots_notice"
    return None


def _page_check_event_finding(
    *,
    source_name: str,
    source_group: str,
    entity: str,
    checked_at: datetime,
    latest_item_title: str | None,
    latest_item_url: str | None,
    source_label: str,
) -> RawFinding | None:
    title = _safe_text(latest_item_title)
    if not title:
        return None
    current_date_markers = (
        checked_at.strftime("%Y-%m-%d"),
        checked_at.strftime("%Y.%m.%d"),
        checked_at.strftime("%Y/%m/%d"),
        checked_at.strftime("%b %d, %Y"),
        checked_at.strftime("%B %d, %Y"),
    )
    if not any(marker in title for marker in current_date_markers):
        return None
    return RawFinding(
        source_family=source_group,
        source_name=source_name,
        source_group=source_group,
        source_tier="page_check",
        entity=entity or "HanAll/Immunovant watch",
        entity_type="company",
        category="scheduled_event",
        title=title,
        summary=f"official page-check detected same-day event candidate from {source_label}",
        published_at=checked_at,
        published_at_kst=_format_kst_text(checked_at),
        primary_source_url=latest_item_url or None,
        source_note=f"page_check_source={source_label}",
        confidence=0.55,
    )


def collect_hanall_page_checks(
    *,
    session: requests.Session,
    page_check_config: dict[str, Any],
    checked_at: datetime,
) -> OfficialCollectionResult:
    sources = page_check_config.get("sources", [])
    if not isinstance(sources, list) or not sources:
        return OfficialCollectionResult()

    timeout_seconds = int(page_check_config.get("timeout_seconds", 12) or 12)
    findings: list[RawFinding] = []
    page_items: list[OfficialPageItem] = []
    checked_source_log: list[CheckedSourceLogEntry] = []
    coverage_gaps: list[CoverageGap] = []
    for raw_source in sources:
        if not isinstance(raw_source, dict):
            continue
        if not bool(raw_source.get("enabled", True)):
            continue

        source_name = _safe_text(raw_source.get("name") or raw_source.get("source_name")) or "page_check"
        source_group = _safe_text(raw_source.get("source_group")) or "discovery_only"
        url = _safe_text(raw_source.get("url"))
        entity = _safe_text(raw_source.get("entity")) or "HanAll/Immunovant watch"
        source_label = _safe_text(raw_source.get("source_label")) or source_name
        discovery_only = bool(raw_source.get("discovery_only", source_group == "discovery_only"))
        include_keywords = [
            keyword
            for keyword in _stringify_values(raw_source.get("include_keywords"))
            if _safe_text(keyword)
        ]
        exclude_keywords = [
            keyword
            for keyword in _stringify_values(raw_source.get("exclude_keywords"))
            if _safe_text(keyword)
        ]
        if not url:
            checked_source_log.append(
                CheckedSourceLogEntry(
                    source_family=source_group,
                    source_name=source_name,
                    source_group=source_group,
                    status="invalid_config",
                    checked_at_kst=checked_at.strftime("%Y-%m-%d %H:%M KST"),
                    note="page-check url missing",
                    endpoint="-",
                    discovery_only=discovery_only,
                )
            )
            coverage_gaps.append(
                CoverageGap(
                    source_family=source_group,
                    source_name=source_name,
                    source_group=source_group,
                    gap_type="invalid_config",
                    detail="page-check url missing",
                    severity="high",
                    endpoint="-",
                    discovery_only=discovery_only,
                )
            )
            continue

        try:
            request_headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
            }
            response = session.get(
                url,
                headers=request_headers,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            html_text = response.text
            access_restriction = _page_check_access_restriction(html_text)
            latest_item_title: str | None = None
            latest_item_url: str | None = None
            latest_item_date: str | None = None
            latest_item_type: str | None = None
            latest_freshness: str | None = None
            structured_items, parser_warnings = parse_official_page_items(
                source_config=raw_source,
                html_text=html_text,
            )
            if access_restriction == "login_wall":
                parser_warnings.append(("login_wall", "page appears to require login"))
            elif access_restriction == "robots_notice":
                parser_warnings.append(("robots_blocked", "page contains robots/disallow notice"))

            if structured_items:
                for item in structured_items:
                    item.access_restriction = access_restriction
                    item.login_wall = access_restriction == "login_wall"
                    item.robots_blocked = access_restriction == "robots_notice"
                    if not item.login_wall and not item.robots_blocked and should_follow_detail(item, raw_source):
                        try:
                            detail_response = session.get(
                                item.item_url or item.page_url,
                                headers=request_headers,
                                timeout=timeout_seconds,
                            )
                            detail_response.raise_for_status()
                            detail_fields, detail_warnings = parse_detail_page(
                                page_item=item,
                                html_text=detail_response.text,
                            )
                            _apply_page_item_detail_fields(item, detail_fields)
                            if detail_fields:
                                item.detection_method = f"{item.detection_method or 'html_anchor_and_visible_line'}+detail_followup"
                                item.raw_snapshot["detail_followup_url"] = item.item_url or item.page_url
                                item.raw_snapshot["detail_followup_fields"] = sorted(detail_fields.keys())
                            parser_warnings.extend(detail_warnings)
                        except requests.HTTPError as detail_exc:
                            status_code = detail_exc.response.status_code if detail_exc.response is not None else None
                            parser_warnings.append(
                                (
                                    f"detail_http_{status_code}" if status_code else "detail_http_error",
                                    _response_body_excerpt(detail_exc.response) or _masked_exception_text(detail_exc),
                                )
                            )
                        except requests.RequestException as detail_exc:
                            parser_warnings.append(("detail_request_error", _masked_exception_text(detail_exc)))
                    item.item_identity_key, item.source_specific_identity_json = build_page_item_identity(item)
                    item.content_fingerprint = build_page_item_fingerprint(item)
                    structured_payload = build_page_item_canonical_payload(item)
                    observation_item_url = item.item_url if item.item_url and item.item_url != item.page_url else ""
                    observation = record_page_observation(
                        source_name=item.source_name,
                        page_name=item.page_name,
                        item_url=observation_item_url,
                        item_identity_key=item.item_identity_key,
                        item_title=item.item_title,
                        content_fingerprint=item.content_fingerprint or "",
                        observed_at_kst=checked_at.strftime("%Y-%m-%d %H:%M KST"),
                        published_at_kst=item.published_at_kst,
                        updated_at_kst=item.updated_at_kst or item.scheduled_for_kst,
                        structured_payload=structured_payload,
                        source_specific_identity=item.source_specific_identity_json,
                    )
                    item.freshness_state = observation.freshness_state
                    item.changed_fields = observation.changed_fields or []
                    item.first_seen_at_kst = observation.first_seen_at_kst
                    item.last_seen_at_kst = observation.last_seen_at_kst
                    if item.changed_fields:
                        item.raw_snapshot["changed_fields"] = item.changed_fields
                    page_items.append(item)
                    promoted_finding = promote_official_page_item_to_finding(
                        page_item=item,
                        checked_at=checked_at,
                        discovery_only=discovery_only,
                    )
                    if promoted_finding is not None:
                        findings.append(promoted_finding)

                latest_item = structured_items[0]
                latest_item_title = latest_item.item_title
                latest_item_url = latest_item.item_url
                latest_item_date = latest_item.scheduled_for_kst or latest_item.updated_at_kst or latest_item.published_at_kst
                latest_item_type = latest_item.item_type
                latest_freshness = latest_item.freshness_state
            else:
                latest_item_title, latest_item_url = _page_check_latest_item(
                    html_text=html_text,
                    base_url=url,
                    include_keywords=include_keywords,
                    exclude_keywords=exclude_keywords,
                )

            note_bits = [f"latest_title={_safe_text(latest_item_title) or '-'}"]
            if latest_item_url:
                note_bits.append(f"latest_url={latest_item_url}")
            if latest_item_date:
                note_bits.append(f"latest_date={latest_item_date}")
            if latest_item_type:
                note_bits.append(f"item_type={latest_item_type}")
            if latest_freshness:
                note_bits.append(f"freshness={latest_freshness}")
            if access_restriction:
                note_bits.append(f"access={access_restriction}")
            checked_source_log.append(
                CheckedSourceLogEntry(
                    source_family=source_group,
                    source_name=source_name,
                    source_group=source_group,
                    status="checked",
                    checked_at_kst=checked_at.strftime("%Y-%m-%d %H:%M KST"),
                    note=" | ".join(note_bits),
                    endpoint=url,
                    latest_item_title=latest_item_title,
                    latest_item_url=latest_item_url,
                    access_restriction=access_restriction,
                    discovery_only=discovery_only,
                )
            )
            seen_gap_keys: set[str] = set()
            for gap_type, detail in parser_warnings:
                normalized_gap_type = _safe_text(gap_type) or "parse_failed"
                gap_key = f"{normalized_gap_type}|{detail}"
                if gap_key in seen_gap_keys:
                    continue
                seen_gap_keys.add(gap_key)
                coverage_gaps.append(
                    CoverageGap(
                        source_family=source_group,
                        source_name=source_name,
                        source_group=source_group,
                        gap_type=normalized_gap_type,
                        detail=_safe_text(detail) or "page parser warning",
                        severity="low" if discovery_only else "medium",
                        endpoint=url,
                        discovery_only=discovery_only,
                    )
                )
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            detail = _response_body_excerpt(exc.response) or _masked_exception_text(exc)
            restriction = "login_wall" if status_code in {401, 403} else None
            checked_source_log.append(
                CheckedSourceLogEntry(
                    source_family=source_group,
                    source_name=source_name,
                    source_group=source_group,
                    status=f"http_{status_code}" if status_code else "http_error",
                    checked_at_kst=checked_at.strftime("%Y-%m-%d %H:%M KST"),
                    note=detail,
                    endpoint=url,
                    http_status=status_code,
                    access_restriction=restriction,
                    discovery_only=discovery_only,
                )
            )
            coverage_gaps.append(
                CoverageGap(
                    source_family=source_group,
                    source_name=source_name,
                    source_group=source_group,
                    gap_type=f"http_{status_code}" if status_code else "http_error",
                    detail=detail,
                    severity="medium" if discovery_only else "high",
                    endpoint=url,
                    http_status=status_code,
                    discovery_only=discovery_only,
                )
            )
        except requests.RequestException as exc:
            checked_source_log.append(
                CheckedSourceLogEntry(
                    source_family=source_group,
                    source_name=source_name,
                    source_group=source_group,
                    status="request_error",
                    checked_at_kst=checked_at.strftime("%Y-%m-%d %H:%M KST"),
                    note=_masked_exception_text(exc),
                    endpoint=url,
                    discovery_only=discovery_only,
                )
            )
            coverage_gaps.append(
                CoverageGap(
                    source_family=source_group,
                    source_name=source_name,
                    source_group=source_group,
                    gap_type="request_error",
                    detail=_masked_exception_text(exc),
                    severity="low" if discovery_only else "medium",
                    endpoint=url,
                    discovery_only=discovery_only,
                )
            )

    generated_known_events = build_generated_known_events_from_page_items(page_items, current_now=checked_at)
    return OfficialCollectionResult(
        findings=findings,
        page_items=page_items,
        generated_known_events=generated_known_events,
        checked_source_log=checked_source_log,
        coverage_gaps=coverage_gaps,
    )


COLLECTOR_CLASSES = {
    "sec_api": SecApiCollector,
    "opendart": OpenDartCollector,
    "openfda": OpenFdaCollector,
    "clinicaltrials": ClinicalTrialsCollector,
    "cris": CrisCollector,
    "mfds": MfdsCollector,
    "ncbi": NcbiCollector,
    "europe_pmc": EuropePmcCollector,
    "crossref": CrossrefCollector,
    "biorxiv": BiorxivCollector,
}


def _dedupe_findings(findings: list[RawFinding]) -> list[RawFinding]:
    deduped: list[RawFinding] = []
    seen: set[str] = set()
    for finding in findings:
        page_item_payload = finding.raw_payload.get("page_item", {}) if isinstance(finding.raw_payload, dict) else {}
        key = "|".join(
            [
                finding.source_family,
                finding.source_name,
                str(page_item_payload.get("item_identity_key") or "-"),
                finding.document_id or "-",
                finding.trial_id or "-",
                finding.primary_source_url or "-",
                finding.title,
                finding.accepted_at or finding.updated_at_kst or finding.published_at_kst or "-",
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def collect_hanall_official_findings(
    *,
    session: requests.Session | None = None,
    current_now: datetime | None = None,
) -> OfficialCollectionResult:
    start = perf_counter()
    settings = get_settings()
    config = get_hanall_sources_config()
    collector_config = config.get("collectors", {}) if isinstance(config.get("collectors"), dict) else {}
    page_check_config = config.get("page_checks", {}) if isinstance(config.get("page_checks"), dict) else {}
    owned_session = session is None
    client = session or requests.Session()
    findings: list[RawFinding] = []
    page_items: list[OfficialPageItem] = []
    checked_source_log: list[CheckedSourceLogEntry] = []
    coverage_gaps: list[CoverageGap] = []
    generated_known_events = []
    checked_at = current_now or now_kst()

    try:
        for collector_key, collector_class in COLLECTOR_CLASSES.items():
            collector_start = perf_counter()
            raw_source_config = collector_config.get(collector_key, {})
            source_config = raw_source_config if isinstance(raw_source_config, dict) else {}
            collector = collector_class(settings, source_config)
            try:
                result = collector.collect(client, current_now=checked_at)
            except Exception as exc:
                masked_error = _masked_exception_text(exc)
                logger.error("hanall collector failed collector=%s error=%s", collector_key, masked_error)
                endpoint = "-"
                if isinstance(source_config.get("endpoints"), dict):
                    endpoint = next(iter(source_config.get("endpoints", {}).values()), "-")
                checked_source_log.append(
                    CheckedSourceLogEntry(
                        source_family=collector.source_family,
                        source_name=collector.source_name,
                        status="collector_exception",
                        checked_at_kst=checked_at.strftime("%Y-%m-%d %H:%M KST"),
                        note=_mask_secret_text(masked_error),
                        endpoint=endpoint,
                    )
                )
                coverage_gaps.append(
                    CoverageGap(
                        source_family=collector.source_family,
                        source_name=collector.source_name,
                        gap_type="collector_exception",
                        detail=_mask_secret_text(masked_error),
                        severity="high",
                        endpoint=endpoint,
                    )
                )
                continue

            findings.extend(result.findings)
            page_items.extend(result.page_items)
            generated_known_events.extend(result.generated_known_events)
            checked_source_log.extend(result.checked_source_log)
            coverage_gaps.extend(result.coverage_gaps)
            logger.info(
                "hanall collector completed collector=%s findings=%s source_logs=%s coverage_gaps=%s elapsed_ms=%.1f",
                collector_key,
                len(result.findings),
                len(result.checked_source_log),
                len(result.coverage_gaps),
                (perf_counter() - collector_start) * 1000,
            )

        page_checks_enabled = bool(page_check_config.get("enabled", False))
        if page_checks_enabled:
            page_check_result = collect_hanall_page_checks(
                session=client,
                page_check_config=page_check_config,
                checked_at=checked_at,
            )
            findings.extend(page_check_result.findings)
            page_items.extend(page_check_result.page_items)
            generated_known_events.extend(page_check_result.generated_known_events)
            checked_source_log.extend(page_check_result.checked_source_log)
            coverage_gaps.extend(page_check_result.coverage_gaps)
            logger.info(
                "hanall page checks completed sources=%s findings=%s source_logs=%s coverage_gaps=%s",
                len(page_check_config.get("sources", []) if isinstance(page_check_config.get("sources"), list) else []),
                len(page_check_result.findings),
                len(page_check_result.checked_source_log),
                len(page_check_result.coverage_gaps),
            )

        deduped_findings = _dedupe_findings(findings)
        recent_findings = [finding for finding in deduped_findings if _is_recent_finding(finding, current_now=checked_at)]
        relevant_findings = [finding for finding in recent_findings if _is_low_precision_relevant(finding)]
        logger.info(
            "hanall official collection completed collectors=%s dedupe_before=%s dedupe_after=%s recent_after=%s relevant_after=%s removed_out_of_window=%s removed_low_precision_noise=%s source_logs=%s coverage_gaps=%s elapsed_ms=%.1f",
            len(COLLECTOR_CLASSES),
            len(findings),
            len(deduped_findings),
            len(recent_findings),
            len(relevant_findings),
            max(0, len(deduped_findings) - len(recent_findings)),
            max(0, len(recent_findings) - len(relevant_findings)),
            len(checked_source_log),
            len(coverage_gaps),
            (perf_counter() - start) * 1000,
        )
        return OfficialCollectionResult(
            findings=relevant_findings,
            page_items=page_items,
            generated_known_events=generated_known_events,
            checked_source_log=checked_source_log,
            coverage_gaps=coverage_gaps,
        )
    finally:
        if owned_session:
            client.close()
