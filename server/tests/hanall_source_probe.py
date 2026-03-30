from __future__ import annotations

import json
import zipfile
from io import BytesIO
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

import requests

from server.config import get_hanall_sources_config
from server.infra.hanall_news_collectors import EUROPE_PMC_CANONICAL_TERMS, _decode_data_go_kr_service_key, _mask_secret_text
from server.settings import get_settings


USER_AGENT = "kakao-bot/1.0 (hanall source probe)"


@dataclass(frozen=True)
class ProbeRow:
    source: str
    target: str
    classification: str
    http_status: int | None
    note: str


def _excerpt(text: str, limit: int = 120) -> str:
    compact = " ".join(_mask_secret_text(str(text or "")).split())
    return compact[:limit]


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        return {}


def _item_count(payload: dict[str, Any]) -> int:
    for key in ("filings", "data", "transactions", "results", "studies", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            nested_item = value.get("item")
            if isinstance(nested_item, list):
                return len(nested_item)
            if isinstance(nested_item, dict):
                return 1
    result_list = payload.get("resultList")
    if isinstance(result_list, dict):
        result_value = result_list.get("result")
        if isinstance(result_value, list):
            return len(result_value)
        if isinstance(result_value, dict):
            return 1
    return 0


def _probe_sec_api(session: requests.Session, config: dict[str, Any], settings: Any) -> list[ProbeRow]:
    api_key = settings.sec_api_key
    endpoints = config["collectors"]["sec_api"]["endpoints"]
    specs = {
        "query_api": (
            endpoints["api_root"],
            {"query": 'ticker:IMVT', "from": "0", "size": "1", "sort": [{"filedAt": {"order": "desc"}}]},
        ),
        "form_8k": (
            endpoints["form_8k"],
            {"query": 'ticker:IMVT', "from": "0", "size": "1", "sort": [{"filedAt": {"order": "desc"}}]},
        ),
        "insider_trading": (
            endpoints["insider_trading"],
            {"query": 'issuer.tradingSymbol:IMVT', "from": "0", "size": "1", "sort": [{"filedAt": {"order": "desc"}}]},
        ),
        "sec_litigation_releases": (
            endpoints["sec_litigation_releases"],
            {"query": 'entities.tickers:IMVT OR entities.companyName:"Immunovant"', "from": "0", "size": "1", "sort": [{"releasedAt": {"order": "desc"}}]},
        ),
    }
    rows: list[ProbeRow] = []
    for target, (endpoint, payload) in specs.items():
        for auth_mode, headers, params in (
            ("authorization_header", {"Authorization": api_key}, None),
            ("token_query_param", None, {"token": api_key}),
        ):
            response = session.post(endpoint, headers=headers, params=params, json=payload, timeout=30)
            body = response.text
            if response.ok:
                data = _json(response)
                count = _item_count(data)
                rows.append(
                    ProbeRow(
                        source="sec_api",
                        target=target,
                        classification="auth_valid" if count else "no_match",
                        http_status=response.status_code,
                        note=f"auth={auth_mode} items={count}",
                    )
                )
                break
            lower_body = body.lower()
            if response.status_code in {401, 403} and "invalid" in lower_body and auth_mode == "authorization_header":
                continue
            if response.status_code == 404:
                rows.append(
                    ProbeRow(
                        source="sec_api",
                        target=target,
                        classification="method_or_path_invalid",
                        http_status=response.status_code,
                        note=_excerpt(body),
                    )
                )
            elif response.status_code in {401, 403}:
                rows.append(
                    ProbeRow(
                        source="sec_api",
                        target=target,
                        classification="auth_invalid",
                        http_status=response.status_code,
                        note=_excerpt(body),
                    )
                )
            else:
                rows.append(
                    ProbeRow(
                        source="sec_api",
                        target=target,
                        classification="request_error",
                        http_status=response.status_code,
                        note=_excerpt(body),
                    )
                )
            break
    return rows


def _probe_opendart(session: requests.Session, config: dict[str, Any], settings: Any) -> ProbeRow:
    endpoint = config["collectors"]["opendart"]["endpoints"]["corp_code"]
    response = session.get(endpoint, params={"crtfc_key": settings.opendart_api_key}, timeout=30)
    body = response.text
    content = response.content
    if not response.ok:
        return ProbeRow("opendart", "corp_code", "request_error", response.status_code, _excerpt(body))
    xml_bytes = content
    if content[:2] == b"PK":
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                xml_name = next((name for name in archive.namelist() if name.lower().endswith(".xml")), "")
                if not xml_name:
                    return ProbeRow("opendart", "corp_code", "request_error", response.status_code, "zip_without_xml_entry")
                xml_bytes = archive.read(xml_name)
        except zipfile.BadZipFile:
            return ProbeRow("opendart", "corp_code", "request_error", response.status_code, "invalid_zip_payload")
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return ProbeRow("opendart", "corp_code", "request_error", response.status_code, _excerpt(body))
    status = (root.findtext(".//status") or "").strip()
    message = (root.findtext(".//message") or "").strip()
    record_count = len(root.findall(".//list"))
    if status and status != "000" and "인증키" in message:
        return ProbeRow("opendart", "corp_code", "auth_invalid", response.status_code, f"status={status} message={message}")
    return ProbeRow("opendart", "corp_code", "auth_valid", response.status_code, f"corp_records={record_count}")


def _probe_data_go_kr(session: requests.Session, config: dict[str, Any], settings: Any, source: str, endpoint_key: str, extra_params: dict[str, Any]) -> list[ProbeRow]:
    endpoint = config["collectors"][source]["endpoints"][endpoint_key]
    raw_key = settings.data_go_kr_api_key
    decoded_key = _decode_data_go_kr_service_key(raw_key)
    rows: list[ProbeRow] = []
    modes = [
        ("mode_raw_querystring", f"{endpoint}?{urlencode({**extra_params, 'serviceKey': raw_key}, safe='%')}", None),
        ("mode_requests_params_as_is", endpoint, {**extra_params, "serviceKey": raw_key}),
        ("mode_decode_once_then_requests_params", endpoint, {**extra_params, "serviceKey": decoded_key}),
    ]
    for target, url_or_endpoint, params in modes:
        response = session.get(url_or_endpoint, params=params, timeout=30)
        body = response.text
        if response.ok:
            rows.append(ProbeRow(source, target, "auth_valid", response.status_code, _excerpt(body)))
        elif response.status_code == 401:
            rows.append(ProbeRow(source, target, "auth_invalid", response.status_code, _excerpt(body)))
        else:
            rows.append(ProbeRow(source, target, "request_error", response.status_code, _excerpt(body)))
    return rows


def _probe_openfda(session: requests.Session, config: dict[str, Any], settings: Any) -> list[ProbeRow]:
    endpoints = config["collectors"]["openfda"]["endpoints"]
    rows: list[ProbeRow] = []
    label_response = session.get(
        endpoints["label"],
        params={"limit": 1, "api_key": settings.openfda_api_key},
        timeout=30,
    )
    rows.append(
        ProbeRow(
            "openfda",
            "auth_probe",
            "auth_valid" if label_response.ok else "auth_invalid",
            label_response.status_code,
            _excerpt(label_response.text),
        )
    )
    enforcement_response = session.get(
        endpoints["enforcement"],
        params={"search": "recalling_firm:Immunovant", "limit": 1},
        timeout=30,
    )
    payload = _json(enforcement_response)
    if enforcement_response.status_code == 404 and payload.get("error", {}).get("message") == "No matches found!":
        rows.append(ProbeRow("openfda", "enforcement_query", "no_match", enforcement_response.status_code, "NOT_FOUND | No matches found!"))
    else:
        rows.append(
            ProbeRow(
                "openfda",
                "enforcement_query",
                "auth_valid" if enforcement_response.ok else "request_error",
                enforcement_response.status_code,
                _excerpt(enforcement_response.text),
            )
        )
    return rows


def _probe_clinicaltrials(session: requests.Session, config: dict[str, Any]) -> list[ProbeRow]:
    endpoint = config["collectors"]["clinicaltrials"]["endpoints"]["studies"]
    current = session.get(
        endpoint,
        params={
            "query.term": 'Immunovant OR HanAll Biopharma OR batoclimab OR IMVT-1401 OR IMVT-1402 OR HL161 OR HL036',
            "pageSize": 10,
            "sort": "@lastUpdatePostDate desc",
        },
        timeout=30,
    )
    corrected = session.get(
        endpoint,
        params={
            "query.term": 'Immunovant OR HanAll Biopharma OR batoclimab OR IMVT-1401 OR IMVT-1402 OR HL161 OR HL036',
            "pageSize": 1,
            "sort": "LastUpdatePostDate:desc",
        },
        timeout=30,
    )
    return [
        ProbeRow("clinicaltrials", "current_query", "query_invalid" if current.status_code == 400 else "request_error", current.status_code, _excerpt(current.text)),
        ProbeRow("clinicaltrials", "corrected_query", "auth_valid" if corrected.ok else "request_error", corrected.status_code, f"items={_item_count(_json(corrected))}"),
    ]


def _dedupe_probe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = "|".join(
            filter(
                None,
                [
                    str(item.get("title", "")).strip(),
                    str(item.get("doi", "")).strip(),
                    str(item.get("pmid", "")).strip(),
                    str(item.get("source", "")).strip(),
                    str(item.get("id", "")).strip(),
                ],
            )
        )
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _probe_europe_pmc(session: requests.Session, config: dict[str, Any]) -> list[ProbeRow]:
    endpoint = config["collectors"]["europe_pmc"]["endpoints"]["search"]
    rows: list[ProbeRow] = []
    simple_response = session.get(
        endpoint,
        params={"query": "Immunovant", "format": "json", "resultType": "lite", "pageSize": 5},
        timeout=30,
    )
    if not simple_response.ok:
        rows.append(
            ProbeRow("europe_pmc", "simple_probe", "request_error", simple_response.status_code, _excerpt(simple_response.text))
        )
        return rows
    rows.append(
        ProbeRow(
            "europe_pmc",
            "simple_probe",
            "auth_valid" if _item_count(_json(simple_response)) else "no_match",
            simple_response.status_code,
            f"query=Immunovant items={_item_count(_json(simple_response))}",
        )
    )
    canonical_query = f'{" OR ".join(EUROPE_PMC_CANONICAL_TERMS)} sort_date:y'
    canonical_response = session.get(
        endpoint,
        params={"query": canonical_query, "format": "json", "resultType": "lite", "pageSize": 5},
        timeout=30,
    )
    canonical_count = _item_count(_json(canonical_response)) if canonical_response.ok else 0
    if canonical_response.ok and canonical_count:
        rows.append(
            ProbeRow(
                "europe_pmc",
                "canonical_query",
                "auth_valid",
                canonical_response.status_code,
                f"strategy=canonical_or items={canonical_count}",
            )
        )
        return rows
    split_items: list[dict[str, Any]] = []
    for term in EUROPE_PMC_CANONICAL_TERMS:
        split_response = session.get(
            endpoint,
            params={"query": f"{term} sort_date:y", "format": "json", "resultType": "lite", "pageSize": 5},
            timeout=30,
        )
        if not split_response.ok:
            rows.append(
                ProbeRow("europe_pmc", "split_terms", "request_error", split_response.status_code, _excerpt(split_response.text))
            )
            return rows
        payload = _json(split_response)
        result_list = payload.get("resultList", {})
        result_value = result_list.get("result") if isinstance(result_list, dict) else []
        if isinstance(result_value, list):
            split_items.extend([item for item in result_value if isinstance(item, dict)])
        elif isinstance(result_value, dict):
            split_items.append(result_value)
    deduped = _dedupe_probe_items(split_items)
    rows.append(
        ProbeRow(
            "europe_pmc",
            "split_terms",
            "auth_valid" if deduped else "no_match",
            200,
            f"strategy=split_terms items={len(deduped)}",
        )
    )
    return rows


def _ncbi_contact_params(settings: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if str(getattr(settings, "ncbi_tool_name", "") or "").strip():
        params["tool"] = str(settings.ncbi_tool_name).strip()
    if str(getattr(settings, "ncbi_email", "") or "").strip():
        params["email"] = str(settings.ncbi_email).strip()
    return params


def _probe_ncbi(session: requests.Session, config: dict[str, Any], settings: Any) -> list[ProbeRow]:
    endpoint = config["collectors"]["ncbi"]["endpoints"]["esearch"]
    base_params = {
        "db": "pubmed",
        "term": 'Immunovant OR HanAll Biopharma OR batoclimab OR IMVT-1401 OR IMVT-1402 OR HL161 OR HL036',
        "retmode": "json",
        "retmax": 1,
        "sort": "pub_date",
        **_ncbi_contact_params(settings),
    }
    rows: list[ProbeRow] = []
    api_key = str(getattr(settings, "ncbi_api_key", "") or "").strip()
    if api_key and api_key != "replace_me":
        current = session.get(endpoint, params={**base_params, "api_key": api_key}, timeout=30)
        rows.append(
            ProbeRow(
                "ncbi",
                "with_key_probe",
                "auth_invalid" if current.status_code == 400 else ("auth_valid" if current.ok else "request_error"),
                current.status_code,
                f"auth_mode=with_key {_excerpt(current.text)}",
            )
        )
        if current.ok:
            return rows
        fallback = session.get(endpoint, params=base_params, timeout=30)
        rows.append(
            ProbeRow(
                "ncbi",
                "forced_no_key_probe",
                "auth_valid" if fallback.ok else "request_error",
                fallback.status_code,
                f"auth_mode=forced_no_key items={_item_count(_json(fallback))}",
            )
        )
        return rows
    fallback = session.get(endpoint, params=base_params, timeout=30)
    rows.append(
        ProbeRow(
            "ncbi",
            "no_key_probe",
            "auth_valid" if fallback.ok else "request_error",
            fallback.status_code,
            f"auth_mode=no_key items={_item_count(_json(fallback))}",
        )
    )
    return rows


def _probe_generic_success(session: requests.Session, source: str, endpoint: str, params: dict[str, Any] | None = None) -> ProbeRow:
    response = session.get(endpoint, params=params, timeout=30)
    return ProbeRow(source, "probe", "auth_valid" if response.ok else "request_error", response.status_code, _excerpt(response.text))


def _render_table(rows: list[ProbeRow]) -> str:
    lines = ["source | target | classification | http | note", "--- | --- | --- | --- | ---"]
    for row in rows:
        lines.append(f"{row.source} | {row.target} | {row.classification} | {row.http_status or '-'} | {row.note}")
    return "\n".join(lines)


def main() -> None:
    settings = get_settings()
    config = get_hanall_sources_config()
    rows: list[ProbeRow] = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": USER_AGENT})
        rows.extend(_probe_sec_api(session, config, settings))
        rows.append(_probe_opendart(session, config, settings))
        rows.extend(_probe_openfda(session, config, settings))
        rows.extend(_probe_data_go_kr(session, config, settings, "cris", "list", {"pageNo": 1, "numOfRows": 1, "resultType": "json"}))
        rows.extend(_probe_data_go_kr(session, config, settings, "mfds", "drug_product_permission", {"pageNo": 1, "numOfRows": 1, "type": "json"}))
        rows.extend(_probe_clinicaltrials(session, config))
        rows.extend(_probe_ncbi(session, config, settings))
        rows.extend(_probe_europe_pmc(session, config))
    print(_render_table(rows))


if __name__ == "__main__":
    main()
