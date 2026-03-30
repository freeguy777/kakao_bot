from __future__ import annotations

import io
import json
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import requests

from server.application.hanall_news_pipeline import build_stage1_deterministic_base, run_hanall_news_pipeline
from server.core.hanall_news_models import OfficialCollectionResult, RSSCollectionResult
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
)
from server.utils import now_kst

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "hanall"
NOW = datetime(2026, 3, 29, 9, 0, tzinfo=now_kst().tzinfo)


def _fixture_json(source: str, name: str) -> dict | list:
    return json.loads((FIXTURES_DIR / source / f"{name}.json").read_text(encoding="utf-8"))


def _fixture_text(source: str, name: str, suffix: str) -> str:
    return (FIXTURES_DIR / source / f"{name}.{suffix}").read_text(encoding="utf-8")


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
