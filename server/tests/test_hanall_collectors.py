from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
from urllib.parse import urljoin

import requests

from server.application.hanall_news_pipeline import (
    _final_text_has_required_sections,
    build_stage1_deterministic_base,
    render_stage1_fallback_text,
    run_hanall_news_pipeline,
)
from server.application.hanall_page_items import build_page_item_identity
from server.application.hanall_research import get_hanall_known_events
from server.core.hanall_news_models import OfficialCollectionResult, OfficialPageItem, RSSCollectionResult
from server.infra.hanall_news_collectors import (
    BiorxivCollector,
    ClinicalTrialsCollector,
    CrisCollector,
    CrossrefCollector,
    EuropePmcCollector,
    MfdsCollector,
    NcbiCollector,
    OpenDartCollector,
    OpenFdaCollector,
    SecApiCollector,
    _decode_data_go_kr_service_key,
    _mask_secret_text,
    collect_hanall_page_checks,
)
from server.infra.sqlite_store import init_db
from server.utils import now_kst

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "hanall"
NOW = datetime(2026, 3, 29, 9, 0, tzinfo=now_kst().tzinfo)


def _fixture_json(source: str, name: str) -> dict | list:
    return json.loads((FIXTURES_DIR / source / f"{name}.json").read_text(encoding="utf-8"))


def _fixture_text(source: str, name: str, suffix: str) -> str:
    return (FIXTURES_DIR / source / f"{name}.{suffix}").read_text(encoding="utf-8")


def _fixture_html(name: str) -> str:
    return (FIXTURES_DIR / "page_checks" / f"{name}.html").read_text(encoding="utf-8")


def _zip_xml_fixture(source: str, name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr(f"{name}.xml", _fixture_text(source, name, "xml"))
    return buffer.getvalue()


def _http_error(status_code: int, body: str, *, url: str = "https://example.com/api") -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/json"
    return requests.HTTPError(f"{status_code} error", response=response)


def _html_response(body: str, *, url: str = "https://example.com/page") -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response


def _stage1_json() -> str:
    base = build_stage1_deterministic_base(
        official_collection=OfficialCollectionResult(
            findings=[
                {
                    "source_family": "clinicaltrials",
                    "source_name": "clinicaltrials",
                    "entity": "Immunovant",
                    "category": "company_direct",
                    "title": "IMVT-1401 study updated",
                    "published_at_kst": "2026-03-28 10:00 KST",
                    "primary_source_url": "https://clinicaltrials.gov/api/v2/studies/NCT12345678",
                    "trial_id": "NCT12345678",
                    "confidence": 0.9,
                }
            ],
            checked_source_log=[
                {
                    "source_family": "clinicaltrials",
                    "source_name": "clinicaltrials",
                    "status": "checked",
                    "checked_at_kst": "2026-03-29 09:00 KST",
                    "note": "items=1",
                    "endpoint": "https://clinicaltrials.gov/api/v2/studies",
                }
            ],
            coverage_gaps=[
                {
                    "source_family": "sec",
                    "source_name": "sec_api",
                    "gap_type": "http_403_forbidden",
                    "detail": "403 error",
                    "severity": "medium",
                    "endpoint": "https://api.sec-api.io/form-8k",
                    "http_status": 403,
                }
            ],
        ),
        current_now=NOW,
    )
    return json.dumps(
        {
            "company_direct_confirmed_ids": [base.candidate_order[0]],
            "coverage": {
                "level": "Medium",
                "rationale": "fixture rationale",
            },
        }
    )


class HanallCollectorFixtureRegressionTest(unittest.TestCase):
    def _settings(self, **overrides: str) -> SimpleNamespace:
        values = {
            "sec_api_key": "sec-key",
            "opendart_api_key": "dart-key",
            "openfda_api_key": "openfda-key",
            "data_go_kr_api_key": "data-key",
            "ncbi_api_key": "ncbi-key",
            "ncbi_tool_name": "kakao_bot",
            "ncbi_email": "ops@example.com",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_sec_api_parses_fixture_variants_and_captures_429_gap(self) -> None:
        collector = SecApiCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "endpoints": {
                    "api_root": "https://api.sec-api.io",
                    "full_text_search": "https://api.sec-api.io/full-text-search",
                    "form_8k": "https://api.sec-api.io/form-8k",
                    "insider_trading": "https://api.sec-api.io/insider-trading",
                    "sec_litigation_releases": "https://api.sec-api.io/sec-litigation-releases",
                },
            },
        )
        collector._request_json = Mock(
            side_effect=[
                _fixture_json("sec_api", "api_root"),
                _fixture_json("sec_api", "empty"),
                _fixture_json("sec_api", "api_root"),
                _fixture_json("sec_api", "insider_trading"),
                _http_error(429, _fixture_text("openfda", "http_429", "txt")),
            ]
        )

        result = collector.collect(Mock(), current_now=NOW)

        self.assertTrue(any(finding.document_id == "insider-1" for finding in result.findings))
        self.assertTrue(any(finding.filing_type == "P" for finding in result.findings))
        self.assertTrue(any(finding.published_at_kst for finding in result.findings if finding.document_id == "insider-1"))
        self.assertTrue(any(gap.gap_type == "http_429_rate_limited" for gap in result.coverage_gaps))

    def test_sec_api_captures_actual_invalid_token_and_route_mismatch_body(self) -> None:
        collector = SecApiCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "endpoints": {
                    "api_root": "https://api.sec-api.io",
                    "full_text_search": "https://api.sec-api.io/full-text-search",
                    "form_8k": "https://api.sec-api.io/form-8k",
                    "insider_trading": "https://api.sec-api.io/insider-trading",
                    "sec_litigation_releases": "https://api.sec-api.io/sec-litigation-releases",
                },
            },
        )
        collector._request_json = Mock(
            side_effect=[
                _http_error(403, json.dumps(_fixture_json("sec_api", "actual_invalid_token"))),
                _http_error(403, json.dumps(_fixture_json("sec_api", "actual_invalid_token"))),
                _http_error(403, json.dumps(_fixture_json("sec_api", "actual_invalid_token"))),
                _http_error(403, json.dumps(_fixture_json("sec_api", "actual_invalid_token"))),
                _http_error(403, json.dumps(_fixture_json("sec_api", "actual_invalid_token"))),
                _http_error(403, json.dumps(_fixture_json("sec_api", "actual_invalid_token"))),
                _http_error(404, _fixture_text("sec_api", "actual_insider_404", "html")),
                _http_error(404, _fixture_text("sec_api", "actual_litigation_404", "html")),
            ]
        )

        result = collector.collect(Mock(), current_now=NOW)

        invalid_token_gap = next(gap for gap in result.coverage_gaps if gap.http_status == 403)
        route_mismatch_gap = next(gap for gap in result.coverage_gaps if gap.http_status == 404)
        self.assertIn("API token invalid", invalid_token_gap.detail)
        self.assertIn("Cannot GET /insider-trading", route_mismatch_gap.detail)

    def test_sec_api_uses_post_with_authorization_then_token_fallback(self) -> None:
        collector = SecApiCollector(
            self._settings(sec_api_key="sec-key"),
            {
                "enabled": True,
                "requires_api_key": True,
                "endpoints": {
                    "api_root": "https://api.sec-api.io",
                    "full_text_search": "https://api.sec-api.io/full-text-search",
                    "form_8k": "https://api.sec-api.io/form-8k",
                    "insider_trading": "https://api.sec-api.io/insider-trading",
                    "sec_litigation_releases": "https://api.sec-api.io/sec-litigation-releases",
                },
            },
        )

        def _side_effect(*args, **kwargs):
            params = kwargs.get("params")
            headers = kwargs.get("headers")
            endpoint = kwargs.get("endpoint")
            if endpoint == "https://api.sec-api.io" and headers and headers.get("Authorization") == "sec-key":
                raise _http_error(403, json.dumps(_fixture_json("sec_api", "actual_invalid_token")), url=endpoint)
            if endpoint == "https://api.sec-api.io" and params == {"token": "sec-key"}:
                return _fixture_json("sec_api", "actual_query_post_success")
            if endpoint == "https://api.sec-api.io/full-text-search":
                return _fixture_json("sec_api", "empty")
            if endpoint == "https://api.sec-api.io/form-8k":
                return _fixture_json("sec_api", "api_root")
            if endpoint == "https://api.sec-api.io/insider-trading":
                return _fixture_json("sec_api", "actual_insider_post_success")
            if endpoint == "https://api.sec-api.io/sec-litigation-releases":
                return _fixture_json("sec_api", "actual_litigation_post_empty")
            raise AssertionError(f"unexpected call endpoint={endpoint} params={params} headers={headers}")

        collector._request_json = Mock(side_effect=_side_effect)

        result = collector.collect(Mock(), current_now=NOW)

        first_call = collector._request_json.call_args_list[0].kwargs
        fallback_call = collector._request_json.call_args_list[1].kwargs
        self.assertEqual(first_call["method"], "POST")
        self.assertEqual(first_call["headers"], {"Authorization": "sec-key"})
        self.assertIsNone(first_call["params"])
        self.assertEqual(fallback_call["params"], {"token": "sec-key"})
        self.assertTrue(any("auth=token_query_param" in entry.note for entry in result.checked_source_log if entry.source_name == "sec_api"))
        self.assertTrue(any(finding.document_id == "0001764013-26-000015" for finding in result.findings))
        self.assertTrue(any(finding.filing_type == "4" for finding in result.findings))

    def test_opendart_parses_zip_xml_fixture(self) -> None:
        collector = OpenDartCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "endpoints": {
                    "corp_code": "https://opendart.fss.or.kr/api/corpCode.xml",
                    "list": "https://opendart.fss.or.kr/api/list.json",
                    "company": "https://opendart.fss.or.kr/api/company.json",
                    "eng_single_account_all": "https://engopendart.fss.or.kr/engapi/fnlttSinglAcntAll.json",
                },
            },
        )
        collector._request_bytes = Mock(return_value=_zip_xml_fixture("opendart", "corp_code"))
        collector._request_json = Mock(
            side_effect=[
                _fixture_json("opendart", "list"),
                _fixture_json("opendart", "company"),
                _fixture_json("opendart", "account"),
            ]
        )

        result = collector.collect(Mock(), current_now=NOW)

        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].document_id, "20260327000123")
        self.assertEqual(result.findings[0].entity, "HanAll Biopharma")
        self.assertTrue(all(entry.status == "checked" for entry in result.checked_source_log))

    def test_opendart_handles_actual_corp_code_xml_api_error_body(self) -> None:
        collector = OpenDartCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "endpoints": {
                    "corp_code": "https://opendart.fss.or.kr/api/corpCode.xml",
                },
            },
        )
        collector._request_bytes = Mock(return_value=_fixture_text("opendart", "corp_code_invalid_key", "xml").encode("utf-8"))

        result = collector.collect(Mock(), current_now=NOW)

        self.assertEqual(result.checked_source_log[0].status, "api_error")
        self.assertEqual(result.coverage_gaps[0].gap_type, "api_error")
        self.assertIn("status=010", result.coverage_gaps[0].detail)

    def test_openfda_parses_endpoint_variants_and_http_status_gaps(self) -> None:
        collector = OpenFdaCollector(
            self._settings(),
            {
                "enabled": True,
                "endpoints": {
                    "drugsfda": "https://api.fda.gov/drug/drugsfda.json",
                    "label": "https://api.fda.gov/drug/label.json",
                    "event": "https://api.fda.gov/drug/event.json",
                    "enforcement": "https://api.fda.gov/drug/enforcement.json",
                    "shortages": "https://api.fda.gov/drug/shortages.json",
                    "crl": "https://api.fda.gov/transparency/crl.json",
                },
            },
        )
        collector._request_json = Mock(
            side_effect=[
                _fixture_json("openfda", "drugsfda"),
                _fixture_json("openfda", "label_variation"),
                _fixture_json("openfda", "empty"),
                _http_error(403, json.dumps(_fixture_json("errors", "http_403"))),
                _http_error(404, json.dumps(_fixture_json("errors", "http_404"))),
                _http_error(429, json.dumps(_fixture_json("errors", "http_429"))),
            ]
        )

        result = collector.collect(Mock(), current_now=NOW)

        self.assertGreaterEqual(len(result.findings), 2)
        self.assertTrue(any(finding.asset == "batoclimab" for finding in result.findings))
        self.assertEqual(
            {gap.gap_type for gap in result.coverage_gaps},
            {"http_403_forbidden", "http_404_not_found", "http_429_rate_limited"},
        )

    def test_openfda_uses_no_key_fallback_for_invalid_key_and_treats_no_match_as_checked(self) -> None:
        collector = OpenFdaCollector(
            self._settings(),
            {
                "enabled": True,
                "endpoints": {
                    "drugsfda": "https://api.fda.gov/drug/drugsfda.json",
                    "label": "https://api.fda.gov/drug/label.json",
                    "event": "https://api.fda.gov/drug/event.json",
                    "enforcement": "https://api.fda.gov/drug/enforcement.json",
                    "shortages": "https://api.fda.gov/drug/shortages.json",
                    "crl": "https://api.fda.gov/transparency/crl.json",
                },
            },
        )

        def _side_effect(*args, **kwargs):
            endpoint = kwargs.get("endpoint")
            params = kwargs.get("params") or {}
            if params.get("api_key"):
                if endpoint == "https://api.fda.gov/drug/enforcement.json":
                    raise _http_error(404, json.dumps(_fixture_json("openfda", "enforcement_no_matches")), url=endpoint)
                raise _http_error(403, json.dumps(_fixture_json("openfda", "api_key_invalid")), url=endpoint)
            if endpoint == "https://api.fda.gov/drug/enforcement.json":
                raise _http_error(404, json.dumps(_fixture_json("openfda", "enforcement_no_matches")), url=endpoint)
            return _fixture_json("openfda", "drugsfda")

        collector._request_json = Mock(side_effect=_side_effect)

        result = collector.collect(Mock(), current_now=NOW)

        self.assertFalse(any("enforcement" in (gap.endpoint or "") for gap in result.coverage_gaps))
        self.assertTrue(any("auth=no_key_fallback" in entry.note for entry in result.checked_source_log if "drugsfda" in (entry.endpoint or "")))
        self.assertTrue(any("no_match=1" in entry.note for entry in result.checked_source_log if "enforcement" in (entry.endpoint or "")))

    def test_clinicaltrials_parses_fixture_variants_and_logs_parser_summary(self) -> None:
        collector = ClinicalTrialsCollector(
            self._settings(),
            {
                "enabled": True,
                "endpoints": {
                    "studies": "https://clinicaltrials.gov/api/v2/studies",
                    "study_by_id": "https://clinicaltrials.gov/api/v2/studies/{NCT_ID}",
                },
            },
        )
        collector._request_json = Mock(
            side_effect=[
                _fixture_json("clinicaltrials", "studies"),
                {},
                _http_error(404, _fixture_text("clinicaltrials", "http_404", "txt")),
            ]
        )

        with self.assertLogs("server.infra.hanall_news_collectors", level="INFO") as logs:
            result = collector.collect(Mock(), current_now=NOW)

        joined_logs = "\n".join(logs.output)
        self.assertEqual(len(result.findings), 2)
        self.assertTrue(any(finding.entity == "HanAll Biopharma" for finding in result.findings))
        self.assertTrue(any(gap.gap_type == "http_404_not_found" for gap in result.coverage_gaps))
        self.assertIn("parser_summary source=clinicaltrials", joined_logs)
        self.assertIn("warning_count=", joined_logs)
        self.assertEqual(collector._request_json.call_args_list[0].kwargs["params"]["sort"], "LastUpdatePostDate:desc")

    def test_clinicaltrials_actual_400_body_fixture_matches_sort_error(self) -> None:
        body = _fixture_text("clinicaltrials", "actual_sort_invalid", "txt")
        self.assertIn("parameter `sort`", body)
        self.assertIn("incorrect format", body)

    def test_cris_parses_json_envelope_and_empty_results(self) -> None:
        collector = CrisCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "endpoints": {"list": "http://apis.data.go.kr/1352159/crisinfodataview/list"},
            },
        )
        collector._request_json = Mock(return_value=_fixture_json("cris", "list"))

        result = collector.collect(Mock(), current_now=NOW)

        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].trial_id, "KCT0009999")

        collector._request_json = Mock(return_value=_fixture_json("cris", "empty"))
        empty_result = collector.collect(Mock(), current_now=NOW)
        self.assertEqual(len(empty_result.findings), 0)
        self.assertEqual(empty_result.checked_source_log[0].status, "checked")

    def test_cris_and_mfds_map_401_unauthorized_from_actual_payload(self) -> None:
        cris_collector = CrisCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "endpoints": {"list": "http://apis.data.go.kr/1352159/crisinfodataview/list"},
            },
        )
        cris_collector._request_json = Mock(side_effect=_http_error(401, _fixture_text("cris", "unauthorized", "txt")))
        cris_result = cris_collector.collect(Mock(), current_now=NOW)
        self.assertEqual(cris_result.coverage_gaps[0].gap_type, "http_401_unauthorized")
        self.assertIn("Unauthorized", cris_result.coverage_gaps[0].detail)

        mfds_collector = MfdsCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "services": {"drug_product_permission": True},
                "endpoints": {
                    "drug_product_permission": "http://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnInq07"
                },
            },
        )
        mfds_collector._request_json = Mock(side_effect=_http_error(401, _fixture_text("mfds", "unauthorized", "txt")))
        mfds_result = mfds_collector.collect(Mock(), current_now=NOW)
        self.assertEqual(mfds_result.coverage_gaps[0].gap_type, "http_401_unauthorized")
        self.assertIn("Unauthorized", mfds_result.coverage_gaps[0].detail)

    def test_data_go_kr_service_key_is_decoded_once_for_requests_params(self) -> None:
        encoded_key = "abc%2Fdef%3D%3D"
        decoded_key = "abc/def=="
        self.assertEqual(_decode_data_go_kr_service_key(encoded_key), decoded_key)

        cris_collector = CrisCollector(
            self._settings(data_go_kr_api_key=encoded_key),
            {
                "enabled": True,
                "requires_api_key": True,
                "endpoints": {"list": "http://apis.data.go.kr/1352159/crisinfodataview/list"},
            },
        )
        self.assertEqual(
            cris_collector._data_go_kr_params(pageNo=1, numOfRows=1, resultType="json")["serviceKey"],
            decoded_key,
        )

    def test_secret_text_masker_redacts_key_like_fields(self) -> None:
        raw = '{"api-key":"secret123","token":"abc","serviceKey":"xyz","crtfc_key":"dart"}?api_key=secret123&Authorization=abc&crtfc_key=dart'
        masked = _mask_secret_text(raw)
        self.assertNotIn("secret123", masked)
        self.assertNotIn("xyz", masked)
        self.assertNotIn("dart", masked)
        self.assertIn('"api-key":"***"', masked)
        self.assertIn('"serviceKey":"***"', masked)
        self.assertIn('"crtfc_key":"***"', masked)
        self.assertIn("Authorization=***", masked)

    def test_mfds_reports_approval_gated_disabled_and_parses_enabled_fixture(self) -> None:
        disabled_collector = MfdsCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "services": {"drug_product_permission": False},
                "endpoints": {
                    "drug_product_permission": "http://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnInq07"
                },
            },
        )
        disabled_result = disabled_collector.collect(Mock(), current_now=NOW)
        self.assertEqual(disabled_result.checked_source_log[0].status, "approval_gated_disabled")

        enabled_collector = MfdsCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "services": {"drug_product_permission": True},
                "endpoints": {
                    "drug_product_permission": "http://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService07/getDrugPrdtPrmsnInq07"
                },
            },
        )
        enabled_collector._request_json = Mock(return_value=_fixture_json("mfds", "drug_product_permission"))
        enabled_result = enabled_collector.collect(Mock(), current_now=NOW)
        self.assertEqual(len(enabled_result.findings), 1)
        self.assertEqual(enabled_result.findings[0].entity, "HanAll Biopharma")

    def test_mfds_medicine_clinical_test_info_uses_service_specific_fields(self) -> None:
        collector = MfdsCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "services": {"medicine_clinical_test_info": True},
                "endpoints": {
                    "medicine_clinical_test_info": "http://apis.data.go.kr/1471000/MdcinClincTestInfoService02/getMdcinClincTestInfoList02"
                },
            },
        )
        collector._request_json = Mock(return_value=_fixture_json("mfds", "medicine_clinical_test_info"))

        result = collector.collect(Mock(), current_now=NOW)

        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.title, "중증근무력증 환자에서 Batoclimab의 유효성 및 안전성을 평가하기 위한 임상시험")
        self.assertEqual(finding.entity, "HanAll Biopharma")
        self.assertEqual(finding.document_id, "202600123")
        self.assertEqual(finding.trial_id, "202600123")
        self.assertEqual(finding.asset, "Batoclimab")
        self.assertEqual(finding.document_type, "clinical_trial")
        self.assertEqual(result.checked_source_log[0].status, "checked")

    def test_mfds_uses_wide_default_page_size_without_guessing_bgn_date(self) -> None:
        collector = MfdsCollector(
            self._settings(),
            {
                "enabled": True,
                "requires_api_key": True,
                "services": {"drug_safe_letter": True},
                "endpoints": {
                    "drug_safe_letter": "http://apis.data.go.kr/1471000/DrugSafeLetterService02/getDrugSafeLetterList02"
                },
            },
        )
        collector._request_json = Mock(return_value=_fixture_json("mfds", "drug_product_permission"))

        result = collector.collect(Mock(), current_now=NOW)

        self.assertEqual(result.checked_source_log[0].status, "checked")
        request_params = collector._request_json.call_args.kwargs["params"]
        self.assertEqual(request_params["numOfRows"], 100)
        self.assertEqual(request_params["pageNo"], 1)
        self.assertEqual(request_params["type"], "json")
        self.assertNotIn("BGN_DATE", request_params)

    def test_ncbi_parses_pubdate_variants_and_empty_search(self) -> None:
        collector = NcbiCollector(
            self._settings(),
            {
                "enabled": True,
                "endpoints": {
                    "esearch": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    "esummary": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                    "efetch": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    "elink": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
                },
            },
        )
        collector._request_json = Mock(
            side_effect=[
                _fixture_json("ncbi", "esearch"),
                _fixture_json("ncbi", "esummary"),
                _fixture_json("ncbi", "elink"),
            ]
        )
        collector._request_text = Mock(return_value=_fixture_text("ncbi", "efetch", "xml"))

        result = collector.collect(Mock(), current_now=NOW)

        self.assertEqual(len(result.findings), 2)
        self.assertTrue(all(finding.published_at_kst for finding in result.findings))
        self.assertTrue(any(finding.document_id == "401002" and finding.title == "FcRn review with Immunovant context" for finding in result.findings))

        collector._request_json = Mock(return_value=_fixture_json("ncbi", "esearch_empty"))
        collector._request_text = Mock(return_value=_fixture_text("ncbi", "efetch", "xml"))
        empty_result = collector.collect(Mock(), current_now=NOW)
        self.assertEqual(len(empty_result.findings), 0)

    def test_ncbi_forces_no_key_mode_for_rest_of_run_after_invalid_key(self) -> None:
        collector = NcbiCollector(
            self._settings(ncbi_api_key="bad-key", ncbi_tool_name="kakao_bot", ncbi_email="ops@example.com"),
            {
                "enabled": True,
                "endpoints": {
                    "esearch": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    "esummary": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                    "efetch": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    "elink": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
                },
            },
        )

        def _side_effect(*args, **kwargs):
            endpoint = kwargs.get("endpoint")
            params = kwargs.get("params") or {}
            if params.get("api_key") == "bad-key":
                raise _http_error(400, json.dumps(_fixture_json("ncbi", "api_key_invalid")), url=endpoint)
            if endpoint.endswith("esearch.fcgi"):
                return _fixture_json("ncbi", "esearch")
            if endpoint.endswith("esummary.fcgi"):
                return _fixture_json("ncbi", "esummary")
            if endpoint.endswith("elink.fcgi"):
                return _fixture_json("ncbi", "elink")
            raise AssertionError(f"unexpected endpoint={endpoint}")

        collector._request_json = Mock(side_effect=_side_effect)
        collector._request_text = Mock(return_value=_fixture_text("ncbi", "efetch", "xml"))

        result = collector.collect(Mock(), current_now=NOW)

        self.assertEqual(len(result.findings), 2)
        self.assertIn("auth_mode=forced_no_key", result.checked_source_log[0].note)
        json_calls = collector._request_json.call_args_list
        self.assertEqual(json_calls[0].kwargs["params"]["api_key"], "bad-key")
        self.assertEqual(json_calls[0].kwargs["params"]["tool"], "kakao_bot")
        self.assertEqual(json_calls[0].kwargs["params"]["email"], "ops@example.com")
        for call in json_calls[1:]:
            self.assertNotIn("api_key", call.kwargs["params"])
            self.assertEqual(call.kwargs["params"]["tool"], "kakao_bot")
            self.assertEqual(call.kwargs["params"]["email"], "ops@example.com")
        self.assertNotIn("api_key", collector._request_text.call_args.kwargs["params"])
        self.assertEqual(collector._request_text.call_args.kwargs["params"]["tool"], "kakao_bot")
        self.assertEqual(collector._request_text.call_args.kwargs["params"]["email"], "ops@example.com")

    def test_europe_pmc_uses_runtime_search_endpoint_and_split_term_fallback(self) -> None:
        collector = EuropePmcCollector(
            self._settings(),
            {
                "enabled": True,
                "endpoints": {"search": "https://www.ebi.ac.uk/europepmc/webservices/rest/search"},
            },
        )

        def _side_effect(*args, **kwargs):
            params = kwargs.get("params") or {}
            query = params.get("query")
            if query == "Immunovant":
                return _fixture_json("europe_pmc", "search")
            if query and " OR " in query:
                return _fixture_json("europe_pmc", "empty")
            if query == "batoclimab sort_date:y":
                return _fixture_json("europe_pmc", "search")
            return _fixture_json("europe_pmc", "empty")

        collector._request_json = Mock(side_effect=_side_effect)

        result = collector.collect(Mock(), current_now=NOW)

        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].primary_source_url, "https://doi.org/10.1101/2026.03.27.123456")
        self.assertIn("strategy=split_terms", result.checked_source_log[0].note)
        self.assertEqual(collector._request_json.call_args_list[0].kwargs["endpoint"], "https://www.ebi.ac.uk/europepmc/webservices/rest/search")
        self.assertEqual(collector._request_json.call_args_list[0].kwargs["method"], "GET")
        self.assertEqual(collector._request_json.call_args_list[0].kwargs["params"]["format"], "json")
        self.assertEqual(collector._request_json.call_args_list[0].kwargs["params"]["resultType"], "lite")

    def test_crossref_parses_date_parts_variants_and_logs_field_failures(self) -> None:
        collector = CrossrefCollector(
            self._settings(),
            {
                "enabled": True,
                "endpoints": {
                    "works": "https://api.crossref.org/works",
                    "work_by_doi": "https://api.crossref.org/works/{doi}",
                },
            },
        )
        collector._request_json = Mock(
            side_effect=[
                _fixture_json("crossref", "works"),
                _fixture_json("crossref", "work_by_doi"),
            ]
        )

        with self.assertLogs("server.infra.hanall_news_collectors", level="INFO") as logs:
            result = collector.collect(Mock(), current_now=NOW)

        joined_logs = "\n".join(logs.output)
        self.assertEqual(len(result.findings), 2)
        self.assertTrue(result.findings[0].published_at_kst)
        self.assertIn("parser_summary source=crossref", joined_logs)
        self.assertIn("field_failures=doi=1", joined_logs)

    def test_biorxiv_parses_biorxiv_and_medrxiv_with_http_403_gap(self) -> None:
        collector = BiorxivCollector(
            self._settings(),
            {
                "enabled": True,
                "endpoints": {
                    "details_interval": "https://api.biorxiv.org/details/[server]/[interval]/[cursor]/[format]",
                    "pubs_interval": "https://api.biorxiv.org/pubs/[server]/[interval]/[cursor]",
                },
            },
        )
        collector._request_json = Mock(
            side_effect=[
                _fixture_json("biorxiv", "details_biorxiv"),
                _fixture_json("biorxiv", "pubs_empty"),
                _http_error(403, _fixture_text("biorxiv", "http_403", "txt")),
            ]
        )

        result = collector.collect(Mock(), current_now=NOW)

        self.assertEqual(len(result.findings), 1)
        self.assertTrue(any(gap.gap_type == "http_403_forbidden" for gap in result.coverage_gaps))


class HanallPageCheckPromotionTest(unittest.TestCase):
    def _page_check_config(
        self,
        *,
        source_name: str,
        source_group: str,
        entity: str,
        url: str,
        discovery_only: bool = False,
        extra_source_fields: dict[str, object] | None = None,
    ) -> dict[str, object]:
        source = {
            "name": source_name,
            "source_group": source_group,
            "entity": entity,
            "source_label": source_name,
            "url": url,
            "discovery_only": discovery_only,
            "enabled": True,
        }
        if extra_source_fields:
            source.update(extra_source_fields)
        return {
            "timeout_seconds": 12,
            "sources": [source],
        }

    def _collect_page_check(
        self,
        *,
        html_name: str,
        source_name: str,
        source_group: str,
        entity: str,
        checked_at: datetime,
        url: str,
        discovery_only: bool = False,
        detail_pages: dict[str, str] | None = None,
        extra_source_fields: dict[str, object] | None = None,
    ) -> OfficialCollectionResult:
        session = Mock()

        def _get(requested_url: str, **kwargs):
            if requested_url == url:
                return _html_response(_fixture_html(html_name), url=requested_url)
            if detail_pages and requested_url in detail_pages:
                return _html_response(_fixture_html(detail_pages[requested_url]), url=requested_url)
            response = requests.Response()
            response.status_code = 404
            response.url = requested_url
            response._content = b"Not Found"
            response.encoding = "utf-8"
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            return response

        session.get.side_effect = _get
        return collect_hanall_page_checks(
            session=session,
            page_check_config=self._page_check_config(
                source_name=source_name,
                source_group=source_group,
                entity=entity,
                url=url,
                discovery_only=discovery_only,
                extra_source_fields=extra_source_fields,
            ),
            checked_at=checked_at,
        )

    def test_ir_calendar_page_item_promotes_to_generated_known_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="immunovant_calendar_today",
                source_name="immunovant_ir_calendar",
                source_group="company_official",
                entity="Immunovant",
                checked_at=NOW,
                url="https://www.immunovant.com/investors/news-events",
            )

        self.assertEqual(result.findings, [])
        self.assertEqual(len(result.generated_known_events), 1)
        event = result.generated_known_events[0]
        self.assertEqual(event.entity, "Immunovant")
        self.assertEqual(event.category, "investor_event")
        self.assertEqual(event.event_source_type, "generated_official")
        self.assertEqual(event.freshness_status, "due_today")
        self.assertIn("Jefferies Biotech Conference", event.fact)
        self.assertIn("page_name=immunovant_ir_calendar", event.basis)

    @patch("server.application.hanall_research._load_hanall_known_event_overrides")
    def test_generated_known_event_overrides_yaml_event(self, mocked_overrides) -> None:
        mocked_overrides.return_value = [
            {
                "entity": "Immunovant",
                "category": "investor_event",
                "scheduled_for_kst": "2026-03-29 20:30 KST",
                "fact": "Manual YAML event text that should not win",
                "basis": "manual yaml",
                "primary_source": "https://example.com/manual",
                "status_note": "manual fallback",
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="immunovant_calendar_today",
                source_name="immunovant_ir_calendar",
                source_group="company_official",
                entity="Immunovant",
                checked_at=NOW,
                url="https://www.immunovant.com/investors/news-events",
            )
            events = get_hanall_known_events("2026-03-29", current_now=NOW, generated_events=result.generated_known_events)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_source_type"], "generated_official")
        self.assertIn("Jefferies Biotech Conference", events[0]["fact"])
        self.assertNotIn("Manual YAML event text", events[0]["fact"])

    def test_stale_generated_event_does_not_appear_in_today_schedule(self) -> None:
        stale_now = datetime(2026, 3, 30, 9, 0, tzinfo=NOW.tzinfo)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="hanall_events_today",
                source_name="hanall_events",
                source_group="company_official",
                entity="HanAll Biopharma",
                checked_at=stale_now,
                url="https://www.hanall.com/m52.php",
            )

        base = build_stage1_deterministic_base(
            official_collection=OfficialCollectionResult(generated_known_events=result.generated_known_events),
            current_now=stale_now,
        )

        self.assertEqual(base.output.today_scheduled_events, [])
        self.assertIn("time inferred", result.generated_known_events[0].status_note)
        self.assertTrue(any(entry.axis == "known_event" for entry in base.output.omission_audit))

    def test_old_page_item_is_treated_as_resurfaced_old_news(self) -> None:
        first_check = datetime(2026, 3, 29, 9, 0, tzinfo=NOW.tzinfo)
        second_check = datetime(2026, 3, 30, 9, 30, tzinfo=NOW.tzinfo)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            first = self._collect_page_check(
                html_name="immunovant_press_release_old",
                source_name="immunovant_press_releases",
                source_group="company_official",
                entity="Immunovant",
                checked_at=first_check,
                url="https://www.immunovant.com/investors/news-events/press-releases",
            )
            second = self._collect_page_check(
                html_name="immunovant_press_release_old",
                source_name="immunovant_press_releases",
                source_group="company_official",
                entity="Immunovant",
                checked_at=second_check,
                url="https://www.immunovant.com/investors/news-events/press-releases",
            )

        self.assertEqual(len(first.findings), 1)
        self.assertEqual(first.page_items[0].freshness_state, "new_item")
        self.assertEqual(second.findings, [])
        self.assertEqual(second.page_items[0].freshness_state, "resurfaced_old_news")

    def test_same_url_changed_registry_item_is_treated_as_substantive_update(self) -> None:
        first_check = datetime(2026, 3, 29, 9, 0, tzinfo=NOW.tzinfo)
        second_check = datetime(2026, 3, 29, 12, 0, tzinfo=NOW.tzinfo)
        detail_url = urljoin("https://euclinicaltrials.eu/search-for-clinical-trials/", "/trial/CTIS-2026-000123-45")
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            first = self._collect_page_check(
                html_name="ctis_registry_v1",
                source_name="ctis",
                source_group="trial_registry",
                entity="EU clinical trials",
                checked_at=first_check,
                url="https://euclinicaltrials.eu/search-for-clinical-trials/",
                detail_pages={detail_url: "ctis_registry_detail_v1"},
            )
            second = self._collect_page_check(
                html_name="ctis_registry_v1",
                source_name="ctis",
                source_group="trial_registry",
                entity="EU clinical trials",
                checked_at=second_check,
                url="https://euclinicaltrials.eu/search-for-clinical-trials/",
                detail_pages={detail_url: "ctis_registry_detail_v2"},
            )

        self.assertEqual(first.page_items[0].freshness_state, "new_item")
        self.assertEqual(second.page_items[0].freshness_state, "substantive_update")
        self.assertEqual(len(second.findings), 1)
        self.assertEqual(second.findings[0].event_action, "updated")
        self.assertEqual(second.findings[0].last_update_posted, "2026-03-29")
        self.assertIn("recruitment_status", second.findings[0].changed_fields)
        self.assertIn("enrollment", second.findings[0].changed_fields)

    def test_krx_detail_followup_populates_structured_fields_and_changed_fields(self) -> None:
        first_check = datetime(2026, 3, 29, 9, 0, tzinfo=NOW.tzinfo)
        second_check = datetime(2026, 3, 29, 10, 0, tzinfo=NOW.tzinfo)
        detail_url = urljoin("https://kind.krx.co.kr/disclosuretoday/disclosuretoday.do", "/disclosure/viewer.do?noticeNo=20260329000123")
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            self._collect_page_check(
                html_name="krx_kind_notice",
                source_name="krx",
                source_group="regulator_disclosure",
                entity="HanAll Biopharma",
                checked_at=first_check,
                url="https://kind.krx.co.kr/disclosuretoday/disclosuretoday.do",
                detail_pages={detail_url: "krx_kind_notice_detail"},
            )
            second = self._collect_page_check(
                html_name="krx_kind_notice",
                source_name="krx",
                source_group="regulator_disclosure",
                entity="HanAll Biopharma",
                checked_at=second_check,
                url="https://kind.krx.co.kr/disclosuretoday/disclosuretoday.do",
                detail_pages={detail_url: "krx_kind_notice_detail_v2"},
            )

        self.assertEqual(len(second.findings), 1)
        finding = second.findings[0]
        self.assertEqual(finding.filing_type, "shareholder_meeting_notice")
        self.assertEqual(finding.accepted_at, "2026-03-29 08:45 KST")
        self.assertEqual(finding.event_action, "accepted")
        self.assertIn("accepted_at", finding.changed_fields)
        self.assertIn("key_numbers", finding.changed_fields)

    def test_kind_detail_followup_promotes_structured_finding(self) -> None:
        detail_url = urljoin("https://kind.krx.co.kr/disclosuretoday/disclosuretoday.do", "/disclosure/viewer.do?noticeNo=20260329000123")
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="krx_kind_notice",
                source_name="kind",
                source_group="regulator_disclosure",
                entity="HanAll Biopharma",
                checked_at=NOW,
                url="https://kind.krx.co.kr/disclosuretoday/disclosuretoday.do",
                detail_pages={detail_url: "krx_kind_notice_detail"},
            )

        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.regulator, "KIND")
        self.assertEqual(finding.accepted_at, "2026-03-29 08:40 KST")
        self.assertEqual(finding.exchange, "KRX")
        self.assertTrue(finding.key_numbers)

    def test_ctis_detail_followup_parse_populates_structured_fields(self) -> None:
        detail_url = urljoin("https://euclinicaltrials.eu/search-for-clinical-trials/", "/trial/CTIS-2026-000123-45")
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="ctis_registry_v1",
                source_name="ctis",
                source_group="trial_registry",
                entity="EU clinical trials",
                checked_at=NOW,
                url="https://euclinicaltrials.eu/search-for-clinical-trials/",
                detail_pages={detail_url: "ctis_registry_detail_v1"},
            )

        finding = result.findings[0]
        self.assertEqual(finding.trial_id, "CTIS-2026-000123-45")
        self.assertEqual(finding.sponsor, "Immunovant Sciences GmbH")
        self.assertEqual(finding.phase, "Phase 2")
        self.assertEqual(finding.recruitment_status, "Recruiting")
        self.assertEqual(finding.enrollment, "120")
        self.assertEqual(finding.primary_completion_date, "2027-01-15")
        self.assertEqual(finding.last_update_posted, "2026-03-29")
        self.assertEqual(finding.target_moa, "FcRn antagonist")
        self.assertEqual(finding.site_countries, ["EU", "US"])

    def test_registry_detail_followup_populates_minimum_fields_for_jrct_chictr_and_who(self) -> None:
        cases = [
            (
                "jrct",
                "jrct_registry",
                "https://example.com/jrct",
                urljoin("https://example.com/jrct", "/study/jRCT2031260001"),
                "jrct_registry_detail",
                "jRCT2031260001",
                "Immunovant, Inc.",
            ),
            (
                "chictr",
                "chictr_registry",
                "https://example.com/chictr",
                urljoin("https://example.com/chictr", "/showprojEN.html?proj=ChiCTR2400123456"),
                "chictr_registry_detail",
                "ChiCTR2400123456",
                "Harbour BioMed",
            ),
            (
                "who_ictrp",
                "who_ictrp_registry",
                "https://example.com/who_ictrp",
                urljoin("https://example.com/who_ictrp", "/trial2.aspx?trialid=NCT99887766"),
                "who_ictrp_registry_detail",
                "NCT99887766",
                "HanAll Biopharma",
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            for source_name, listing_fixture, url, detail_url, detail_fixture, trial_id, sponsor in cases:
                result = self._collect_page_check(
                    html_name=listing_fixture,
                    source_name=source_name,
                    source_group="trial_registry",
                    entity="registry watch",
                    checked_at=NOW,
                    url=url,
                    detail_pages={detail_url: detail_fixture},
                )
                finding = result.findings[0]
                self.assertEqual(finding.trial_id, trial_id)
                self.assertEqual(finding.sponsor, sponsor)
                self.assertTrue(finding.recruitment_status)
                self.assertTrue(finding.updated_at_kst or finding.last_update_posted)

    def test_same_metadata_and_same_detail_is_unchanged_on_repeat(self) -> None:
        first_check = datetime(2026, 3, 29, 9, 0, tzinfo=NOW.tzinfo)
        second_check = datetime(2026, 3, 29, 10, 0, tzinfo=NOW.tzinfo)
        detail_url = urljoin("https://euclinicaltrials.eu/search-for-clinical-trials/", "/trial/CTIS-2026-000123-45")
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            self._collect_page_check(
                html_name="ctis_registry_v1",
                source_name="ctis",
                source_group="trial_registry",
                entity="EU clinical trials",
                checked_at=first_check,
                url="https://euclinicaltrials.eu/search-for-clinical-trials/",
                detail_pages={detail_url: "ctis_registry_detail_v1"},
            )
            second = self._collect_page_check(
                html_name="ctis_registry_v1",
                source_name="ctis",
                source_group="trial_registry",
                entity="EU clinical trials",
                checked_at=second_check,
                url="https://euclinicaltrials.eu/search-for-clinical-trials/",
                detail_pages={detail_url: "ctis_registry_detail_v1"},
            )

        self.assertEqual(second.page_items[0].freshness_state, "unchanged")
        self.assertEqual(second.findings, [])

    def test_regional_regulator_pages_promote_structured_findings(self) -> None:
        cases = [
            (
                "ema",
                "ema_listing",
                "https://www.ema.europa.eu/",
                urljoin("https://www.ema.europa.eu/", "/medicines/ema-batoclimab-cidp"),
                "ema_detail",
                "EMA",
                "approved",
            ),
            (
                "pmda_mhlw",
                "pmda_listing",
                "https://www.pmda.go.jp/english/",
                urljoin("https://www.pmda.go.jp/english/", "/review-services/pmda-batoclimab-mg"),
                "pmda_detail",
                "PMDA/MHLW",
                "updated",
            ),
            (
                "nmpa",
                "nmpa_listing",
                "https://english.nmpa.gov.cn/",
                urljoin("https://english.nmpa.gov.cn/", "/news/nmpa-hbm9161-gmg"),
                "nmpa_detail",
                "NMPA",
                "published",
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            for source_name, listing_fixture, url, detail_url, detail_fixture, regulator, action in cases:
                result = self._collect_page_check(
                    html_name=listing_fixture,
                    source_name=source_name,
                    source_group="regulator_disclosure",
                    entity="regional regulator",
                    checked_at=NOW,
                    url=url,
                    detail_pages={detail_url: detail_fixture},
                )
                finding = result.findings[0]
                self.assertEqual(finding.regulator, regulator)
                self.assertEqual(finding.event_action, action)
                self.assertTrue(finding.document_id)
                self.assertTrue(finding.regulatory_phrase)

    def test_competitor_official_detail_pages_promote_structured_findings(self) -> None:
        cases = [
            (
                "argenx_official",
                "argenx_listing",
                "https://www.argenx.com/",
                urljoin("https://www.argenx.com/", "/news/argenx-vyvgart-cidp-press-release"),
                "argenx_detail",
                "official_pr",
                "efgartigimod",
                "CIDP",
            ),
            (
                "ucb_official",
                "ucb_listing",
                "https://www.ucb.com/",
                urljoin("https://www.ucb.com/", "/investors/rystiggo-cidp-presentation"),
                "ucb_detail",
                "official_presentation",
                "rozanolixizumab",
                "CIDP",
            ),
            (
                "amgen_official",
                "amgen_listing",
                "https://www.amgen.com/",
                urljoin("https://www.amgen.com/", "/careers/tepezza-medical-lead"),
                "amgen_detail",
                "official_careers",
                "teprotumumab",
                "TED",
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            for source_name, listing_fixture, url, detail_url, detail_fixture, document_type, asset, indication in cases:
                result = self._collect_page_check(
                    html_name=listing_fixture,
                    source_name=source_name,
                    source_group="competitor_official",
                    entity="competitor watch",
                    checked_at=NOW,
                    url=url,
                    detail_pages={detail_url: detail_fixture},
                )
                finding = result.findings[0]
                self.assertEqual(finding.document_type, document_type)
                self.assertEqual(finding.asset, asset)
                self.assertEqual(finding.indication, indication)
                self.assertEqual(finding.category, "competitor_relevant")

    def test_krx_and_kind_items_are_promoted_to_structured_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            results = []
            for source_name in ("krx", "kind"):
                results.append(
                    self._collect_page_check(
                        html_name="krx_kind_notice",
                        source_name=source_name,
                        source_group="regulator_disclosure",
                        entity="HanAll Biopharma",
                        checked_at=NOW,
                        url="https://kind.krx.co.kr/disclosuretoday/disclosuretoday.do",
                    )
                )

        for result, regulator in zip(results, ("KRX", "KIND"), strict=True):
            self.assertEqual(len(result.findings), 1)
            finding = result.findings[0]
            self.assertEqual(finding.document_id, "20260329000123")
            self.assertEqual(finding.filing_type, "shareholder_meeting_notice")
            self.assertEqual(finding.regulator, regulator)
            self.assertEqual(finding.event_action, "published")
            self.assertTrue(finding.published_at_kst)

    def test_trial_registry_items_from_priority_sources_are_promoted(self) -> None:
        cases = [
            ("ctis", "ctis_registry_v1", "CTIS-2026-000123-45", "EU", "Immunovant Sciences GmbH", "CIDP"),
            ("jrct", "jrct_registry", "jRCT2031260001", "JP", "Immunovant, Inc.", "SjD"),
            ("chictr", "chictr_registry", "ChiCTR2400123456", "CN", "Harbour BioMed", "MG"),
            ("who_ictrp", "who_ictrp_registry", "NCT99887766", "US", "HanAll Biopharma", "DED"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            for source_name, fixture_name, trial_id, region, sponsor, indication in cases:
                result = self._collect_page_check(
                    html_name=fixture_name,
                    source_name=source_name,
                    source_group="trial_registry",
                    entity="registry watch",
                    checked_at=NOW,
                    url=f"https://example.com/{source_name}",
                )
                self.assertEqual(len(result.findings), 1)
                finding = result.findings[0]
                self.assertEqual(finding.trial_id, trial_id)
                self.assertEqual(finding.region, region)
                self.assertEqual(finding.sponsor, sponsor)
                self.assertEqual(finding.indication, indication)
                self.assertTrue(finding.last_update_posted)

    def test_access_restriction_page_is_logged_as_gap_without_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="login_wall",
                source_name="immunovant_analyst_coverage",
                source_group="discovery_only",
                entity="Immunovant",
                checked_at=NOW,
                url="https://www.immunovant.com/investors/analyst-coverage",
                discovery_only=True,
            )

        self.assertEqual(result.findings, [])
        self.assertTrue(any(gap.gap_type == "login_wall" for gap in result.coverage_gaps))
        self.assertEqual(result.checked_source_log[0].access_restriction, "login_wall")

    def test_page_derived_findings_feed_omission_audit_axes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="ctis_registry_v1",
                source_name="ctis",
                source_group="trial_registry",
                entity="EU clinical trials",
                checked_at=NOW,
                url="https://euclinicaltrials.eu/search-for-clinical-trials/",
            )

        base = build_stage1_deterministic_base(
            official_collection=OfficialCollectionResult(
                findings=result.findings,
                checked_source_log=result.checked_source_log,
                coverage_gaps=result.coverage_gaps,
            ),
            current_now=NOW,
        )
        axes = {entry.axis for entry in base.output.omission_audit}
        self.assertIn("source_group", axes)
        self.assertIn("indication", axes)
        self.assertIn("region", axes)
        self.assertTrue(any(entry.source_group == "trial_registry" for entry in base.output.omission_audit))
        self.assertTrue(any(entry.indication == "CIDP" for entry in base.output.omission_audit))
        self.assertTrue(any(entry.region == "EU" for entry in base.output.omission_audit))

    def test_page_derived_output_keeps_final_plain_text_section_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            event_result = self._collect_page_check(
                html_name="immunovant_calendar_today",
                source_name="immunovant_ir_calendar",
                source_group="company_official",
                entity="Immunovant",
                checked_at=NOW,
                url="https://www.immunovant.com/investors/news-events",
            )
            finding_result = self._collect_page_check(
                html_name="ctis_registry_v1",
                source_name="ctis",
                source_group="trial_registry",
                entity="EU clinical trials",
                checked_at=NOW,
                url="https://euclinicaltrials.eu/search-for-clinical-trials/",
            )

        official_collection = OfficialCollectionResult(
            findings=finding_result.findings,
            generated_known_events=event_result.generated_known_events,
            checked_source_log=event_result.checked_source_log + finding_result.checked_source_log,
            coverage_gaps=event_result.coverage_gaps + finding_result.coverage_gaps,
        )
        stage1_output = build_stage1_deterministic_base(
            official_collection=official_collection,
            current_now=NOW,
        ).output
        text = render_stage1_fallback_text(
            stage1_output=stage1_output,
            official_collection=official_collection,
            rss_collection=RSSCollectionResult(),
            current_now=NOW,
        )

        headings = [
            "요약",
            "오늘 예정 이벤트",
            "Confirmed Updates — Company Direct",
            "Confirmed Updates — Competitor Relevant",
            "Competitor Map Snapshot",
            "Checked Source Log",
            "Unverified Leads",
            "Coverage Gaps",
            "Omission Audit",
            "검증 메모",
        ]
        lines = text.splitlines()
        positions = [lines.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(_final_text_has_required_sections(text))

    def test_competitor_pipeline_page_auto_syncs_new_universe_entry_and_overrides_seed_fields(self) -> None:
        detail_pages = {
            "https://www.harbourbiomed.com/en/pipeline/hbm9161-gmg": "harbour_pipeline_gmg_detail",
            "https://www.harbourbiomed.com/en/pipeline/hbm9161-cidp": "harbour_pipeline_cidp_detail",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="harbour_pipeline_multi",
                source_name="harbour_biomed_pipeline",
                source_group="competitor_official",
                entity="Harbour BioMed",
                checked_at=NOW,
                url="https://www.harbourbiomed.com/en/pipeline",
                detail_pages=detail_pages,
                extra_source_fields={"max_items_per_page": 3},
            )
            stage1_output = build_stage1_deterministic_base(
                official_collection=OfficialCollectionResult(
                    findings=result.findings,
                    page_items=result.page_items,
                    checked_source_log=result.checked_source_log,
                    coverage_gaps=result.coverage_gaps,
                ),
                current_now=NOW,
            ).output

        cidp_entry = next(
            entry
            for entry in stage1_output.competitor_map_snapshot
            if entry.competitor == "Harbour BioMed" and entry.asset == "HBM9161" and entry.indication == "CIDP"
        )
        mg_entry = next(
            entry
            for entry in stage1_output.competitor_map_snapshot
            if entry.competitor == "Harbour BioMed" and entry.asset == "HBM9161" and entry.indication == "MG"
        )
        self.assertEqual(cidp_entry.source_type, "pipeline_program")
        self.assertGreater(cidp_entry.provenance_score or 0.0, 0.8)
        self.assertEqual(mg_entry.stage_status, "Phase 3 clinical-stage | official page checked")

    def test_multi_item_press_page_collects_multiple_items_and_promotes_each_finding(self) -> None:
        detail_pages = {
            "https://www.immunovant.com/investors/news-events/press-releases/immunovant-mg-update": "immunovant_mg_detail",
            "https://www.immunovant.com/investors/news-events/press-releases/immunovant-cidp-update": "immunovant_cidp_detail",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="immunovant_press_release_multi",
                source_name="immunovant_press_releases",
                source_group="company_official",
                entity="Immunovant",
                checked_at=NOW,
                url="https://www.immunovant.com/investors/news-events/press-releases",
                detail_pages=detail_pages,
                extra_source_fields={"max_items_per_page": 2},
            )

        self.assertEqual(len(result.page_items), 2)
        self.assertEqual(len(result.findings), 2)
        self.assertEqual({finding.asset for finding in result.findings}, {"batoclimab", "IMVT-1402"})

    def test_same_day_multiple_generated_events_are_not_over_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="immunovant_calendar_multi",
                source_name="immunovant_ir_calendar",
                source_group="company_official",
                entity="Immunovant",
                checked_at=NOW,
                url="https://www.immunovant.com/investors/news-events",
                extra_source_fields={"max_items_per_page": 3},
            )

        self.assertEqual(len(result.generated_known_events), 2)
        self.assertEqual(len({event.event_identity_key for event in result.generated_known_events}), 2)

    def test_regulatory_notice_identity_keeps_same_day_multiple_notices_separate(self) -> None:
        detail_pages = {
            "https://kind.krx.co.kr/disclosure/viewer.do?noticeNo=20260329000123": "krx_kind_notice_detail",
            "https://kind.krx.co.kr/disclosure/viewer.do?noticeNo=20260329000124": "krx_kind_notice_detail_second",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="krx_kind_notice_multi",
                source_name="kind",
                source_group="regulator_disclosure",
                entity="HanAll Biopharma",
                checked_at=NOW,
                url="https://kind.krx.co.kr/disclosuretoday/disclosuretoday.do",
                detail_pages=detail_pages,
                extra_source_fields={"max_items_per_page": 3},
            )

        self.assertEqual(len(result.findings), 2)
        self.assertEqual({finding.document_id for finding in result.findings}, {"20260329000123", "20260329000124"})
        self.assertEqual({finding.accepted_at for finding in result.findings}, {"2026-03-29 08:40 KST", "2026-03-29 09:10 KST"})

    def test_trial_registry_identity_key_uses_trial_id_and_update_date(self) -> None:
        first = OfficialPageItem(
            source_name="ctis",
            source_group="trial_registry",
            page_name="CTIS",
            page_url="https://euclinicaltrials.eu/search-for-clinical-trials/",
            item_title="CTIS batoclimab CIDP trial update",
            item_url="https://euclinicaltrials.eu/trial/CTIS-2026-000123-45",
            item_type="trial_registry_update",
            entity="Immunovant",
            asset="batoclimab",
            indication="CIDP",
            trial_id="CTIS-2026-000123-45",
            updated_at_kst="2026-03-29 09:00 KST",
            last_update_posted="2026-03-29",
        )
        second = OfficialPageItem(
            source_name="ctis",
            source_group="trial_registry",
            page_name="CTIS",
            page_url="https://euclinicaltrials.eu/search-for-clinical-trials/",
            item_title="CTIS batoclimab CIDP trial update",
            item_url="https://euclinicaltrials.eu/trial/CTIS-2026-000123-45",
            item_type="trial_registry_update",
            entity="Immunovant",
            asset="batoclimab",
            indication="CIDP",
            trial_id="CTIS-2026-000123-45",
            updated_at_kst="2026-03-30 09:00 KST",
            last_update_posted="2026-03-30",
        )

        first_key, _ = build_page_item_identity(first)
        second_key, _ = build_page_item_identity(second)

        self.assertNotEqual(first_key, second_key)
        self.assertIn("ctis", first_key)
        self.assertIn("ctis202600012345", first_key)

    def test_competitor_official_multi_item_updates_promote_distinct_findings(self) -> None:
        detail_pages = {
            "https://www.roivant.com/news/roivant-imvt1401-pr": "roivant_pr_detail",
            "https://www.roivant.com/investors/imvt1402-presentation": "roivant_presentation_detail",
            "https://www.roivant.com/careers/fcrn-medical-lead": "roivant_careers_detail",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            result = self._collect_page_check(
                html_name="roivant_updates_multi",
                source_name="roivant_official",
                source_group="competitor_official",
                entity="Roivant",
                checked_at=NOW,
                url="https://www.roivant.com/",
                detail_pages=detail_pages,
                extra_source_fields={"max_items_per_page": 3},
            )

        self.assertEqual(len(result.findings), 3)
        self.assertEqual(
            {finding.document_type for finding in result.findings},
            {"official_pr", "official_presentation", "official_careers"},
        )

    def test_no_news_day_keeps_auto_synced_competitor_snapshot_from_persistence(self) -> None:
        detail_pages = {
            "https://www.harbourbiomed.com/en/pipeline/hbm9161-gmg": "harbour_pipeline_gmg_detail",
            "https://www.harbourbiomed.com/en/pipeline/hbm9161-cidp": "harbour_pipeline_cidp_detail",
        }
        next_day = datetime(2026, 3, 30, 9, 0, tzinfo=NOW.tzinfo)
        with tempfile.TemporaryDirectory() as temp_dir:
            init_db(str(Path(temp_dir) / "page-checks.db"))
            initial = self._collect_page_check(
                html_name="harbour_pipeline_multi",
                source_name="harbour_biomed_pipeline",
                source_group="competitor_official",
                entity="Harbour BioMed",
                checked_at=NOW,
                url="https://www.harbourbiomed.com/en/pipeline",
                detail_pages=detail_pages,
                extra_source_fields={"max_items_per_page": 3},
            )
            build_stage1_deterministic_base(
                official_collection=OfficialCollectionResult(
                    findings=initial.findings,
                    page_items=initial.page_items,
                    checked_source_log=initial.checked_source_log,
                    coverage_gaps=initial.coverage_gaps,
                ),
                current_now=NOW,
            )
            empty_stage1 = build_stage1_deterministic_base(
                official_collection=OfficialCollectionResult(),
                current_now=next_day,
            ).output

        self.assertTrue(
            any(
                entry.competitor == "Harbour BioMed" and entry.asset == "HBM9161" and entry.indication == "CIDP"
                for entry in empty_stage1.competitor_map_snapshot
            )
        )


class HanallPipelineLoggingTest(unittest.TestCase):
    @patch("server.application.hanall_news_pipeline.fetch_hanall_rss_results", return_value=RSSCollectionResult())
    @patch("server.application.hanall_news_pipeline.collect_hanall_official_findings")
    @patch("server.application.hanall_news_pipeline.run_prompt_by_key_raw")
    def test_stage2_fallback_logs_reason_and_metric(
        self,
        mocked_run_prompt,
        mocked_collect,
        mocked_rss,
    ) -> None:
        mocked_collect.return_value = OfficialCollectionResult(
            findings=[
                {
                    "source_family": "clinicaltrials",
                    "source_name": "clinicaltrials",
                    "entity": "Immunovant",
                    "category": "company_direct",
                    "title": "IMVT-1401 study updated",
                }
            ],
            checked_source_log=[
                {
                    "source_family": "clinicaltrials",
                    "source_name": "clinicaltrials",
                    "status": "checked",
                    "checked_at_kst": "2026-03-29 09:00 KST",
                    "note": "items=1",
                    "endpoint": "https://clinicaltrials.gov/api/v2/studies",
                }
            ],
            coverage_gaps=[
                {
                    "source_family": "sec",
                    "source_name": "sec_api",
                    "gap_type": "http_403_forbidden",
                    "detail": "403 error",
                    "severity": "medium",
                    "endpoint": "https://api.sec-api.io/form-8k",
                    "http_status": 403,
                }
            ],
        )
        mocked_run_prompt.side_effect = [_stage1_json(), ValueError("stage2 returned invalid or incomplete sectioned text")]

        with self.assertLogs("server.application.hanall_news_pipeline", level="INFO") as logs:
            result = run_hanall_news_pipeline(current_now=NOW)

        joined_logs = "\n".join(logs.output)
        self.assertTrue(result.used_stage2_fallback)
        self.assertIn("reason=invalid_final_text", joined_logs)
        self.assertIn("source_statuses=checked=1", joined_logs)
        self.assertIn("gap_types=http_403_forbidden=1", joined_logs)
        self.assertIn("hanall metric stage_fallback_counts stage1=0 stage2=1", joined_logs)
        self.assertIn("hanall metric stage2_fallback used=True", joined_logs)
