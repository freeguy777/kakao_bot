from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import requests
from youtube_transcript_api import TranscriptsDisabled

from server.application.delivery import notify_admin_error
from server.application.family import build_family_morning_brief
from server.application.hanall_news_pipeline import (
    HanallNewsPipelineResult,
    _final_text_has_required_sections,
    _merge_stage1_overlay,
    _parse_stage1_output,
    _parse_stage1_overlay_with_diagnostics,
    _parse_stage1_output_with_diagnostics,
    _reset_stage2_rate_limit_state,
    _stabilize_stage1_output,
    build_stage1_deterministic_base,
    build_stage1_fallback_output,
    normalize_hanall_final_text,
    render_stage1_fallback_text,
    run_hanall_news_pipeline,
)
from server.application.hanall_research import (
    HanallResearchItem,
    HanallSourceStatus,
    build_hanall_base_prompt_replacements,
    build_hanall_known_events_context,
    build_hanall_prompt_replacements,
    build_hanall_research_packet,
    get_hanall_known_events,
)
from server.application.news import build_hanall_news_brief
from server.application.prompting import run_prompt_by_key_raw
from server.application.weather import get_room_weather_snapshot
from server.application.youtube import collect_youtube_summary_messages, split_long_message, summarize_youtube_url
from server.application.use_cases.outbox_polling import reset_polling_status
from server.application.use_cases.runtime_health import RuntimeHealthUseCase
from server.config import _warn_room_delivery_targets, get_prompt, resolve_room_policy
from server.core.hanall_news_models import (
    CheckedSourceLogEntry,
    CoverageGap,
    HanallStage1OverlayOutput,
    CoverageSummary,
    HanallStage1StructuredOutput,
    OmissionAuditEntry,
    OfficialCollectionResult,
    RSSCollectionResult,
    RawFinding,
)
from server.infra.hanall_news_collectors import MfdsCollector, _dedupe_findings, collect_hanall_official_findings
from server.infra.llm_clients import call_gemini_text, call_openai_text
from server.infra.sqlite_store import (
    ack_outbox_messages,
    count_outbox_messages,
    init_db,
    list_scheduler_events,
    pull_pending_outbox_messages,
    record_scheduler_event,
    register_admin_alert_attempt,
    register_delivery_dedupe,
)
from server.utils import now_kst


class HanallResearchPacketTest(unittest.TestCase):
    def _make_item(
        self,
        *,
        bucket: str,
        entity: str,
        source: str,
        title: str,
        published_at: datetime,
    ) -> HanallResearchItem:
        return HanallResearchItem(
            bucket=bucket,
            entity=entity,
            source=source,
            title=title,
            url=f"https://example.com/{source}/{title.replace(' ', '-').lower()}",
            published_at=published_at,
            published_text=published_at.strftime("%Y-%m-%d %H:%M KST"),
            snippet=f"{title} snippet",
        )

    @patch("server.application.hanall_research.build_hanall_known_events_context", return_value="- entity: HanAll Biopharma")
    @patch("server.application.hanall_research._collect_hanall_ir_events")
    @patch("server.application.hanall_research._collect_immunovant_press_items")
    @patch("server.application.hanall_research._collect_hanall_krx_items")
    @patch("server.application.hanall_research._collect_hanall_home_items")
    @patch("server.application.hanall_research._collect_sec_items")
    @patch("server.application.hanall_research._collect_google_news_items")
    @patch("server.application.hanall_research._build_http_session")
    def test_builds_compact_stage1_packet(
        self,
        mocked_session_factory,
        mocked_google_news,
        mocked_sec,
        mocked_hanall_home,
        mocked_hanall_krx,
        mocked_immunovant_press,
        mocked_ir_events,
        mocked_known_events,
    ) -> None:
        now = datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo)
        mocked_session = Mock()
        mocked_session_factory.return_value = mocked_session

        mocked_google_news.return_value = (
            [
                self._make_item(
                    bucket="competitor_relevant",
                    entity="argenx",
                    source="google_news_rss",
                    title="argenx lead",
                    published_at=now - timedelta(hours=2),
                )
            ],
            HanallSourceStatus(
                source="google_news_rss",
                status="새 항목 있음",
                checked_at_text="2026-03-27 09:00 KST",
                note="items=1",
            ),
        )
        mocked_sec.return_value = (
            [
                self._make_item(
                    bucket="company_direct",
                    entity="Immunovant",
                    source="sec_submissions",
                    title="SEC 8-K",
                    published_at=now - timedelta(hours=3),
                )
            ],
            HanallSourceStatus(
                source="sec_submissions",
                status="새 항목 있음",
                checked_at_text="2026-03-27 09:00 KST",
                note="items=1",
            ),
        )
        mocked_hanall_home.return_value = (
            [
                self._make_item(
                    bucket="company_direct",
                    entity="HanAll Biopharma",
                    source="hanall_home_news",
                    title="HanAll article",
                    published_at=now - timedelta(hours=4),
                )
            ],
            HanallSourceStatus(
                source="hanall_home_news",
                status="새 항목 있음",
                checked_at_text="2026-03-27 09:00 KST",
                note="items=1",
            ),
        )
        mocked_hanall_krx.return_value = (
            [],
            HanallSourceStatus(
                source="hanall_krx_filings",
                status="확인했으나 신규 없음",
                checked_at_text="2026-03-27 09:00 KST",
                note="items=0",
            ),
        )
        mocked_immunovant_press.return_value = (
            [],
            HanallSourceStatus(
                source="immunovant_press_releases",
                status="확인했으나 신규 없음",
                checked_at_text="2026-03-27 09:00 KST",
                note="items=0",
            ),
        )
        mocked_ir_events.return_value = (
            [
                self._make_item(
                    bucket="scheduled_event",
                    entity="HanAll Biopharma",
                    source="hanall_ir_events",
                    title="NDR Presentation",
                    published_at=now.replace(hour=0, minute=0),
                )
            ],
            HanallSourceStatus(
                source="hanall_ir_events",
                status="새 항목 있음",
                checked_at_text="2026-03-27 09:00 KST",
                note="today_candidates=1",
            ),
        )

        packet = build_hanall_research_packet(now)

        self.assertIn("[Stage 1 Research Packet]", packet)
        self.assertIn("today_known_events:", packet)
        self.assertIn("- entity: HanAll Biopharma", packet)
        self.assertIn("bucket=scheduled_event", packet)
        self.assertIn("bucket=company_direct", packet)
        self.assertIn("bucket=competitor_relevant", packet)
        self.assertIn("source_status:", packet)
        self.assertIn("google_news_rss: 새 항목 있음", packet)
        mocked_session.close.assert_called_once()

    @patch("server.application.hanall_research.build_hanall_known_events_context", return_value="- known event")
    @patch("server.application.hanall_research.build_hanall_scope_context", return_value="watch_scope: ...")
    @patch("server.application.hanall_research.build_hanall_research_packet", return_value="stage1 packet")
    def test_prompt_replacements_include_stage1_packet(
        self,
        mocked_packet,
        mocked_scope_context,
        mocked_known_events,
    ) -> None:
        now = datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo)

        replacements = build_hanall_prompt_replacements(now)

        self.assertEqual(replacements["__NOW_KST__"], "2026-03-27 09:00 KST")
        self.assertEqual(replacements["__TODAY_KST__"], "2026-03-27")
        self.assertEqual(replacements["__SCHEDULE_LOOKBACK_START_KST__"], "2026-03-26 09:00 KST")
        self.assertEqual(replacements["__SCHEDULE_LOOKBACK_END_KST__"], "2026-03-27 09:00 KST")
        self.assertEqual(replacements["__KNOWN_EVENTS_CONTEXT__"], "- known event")
        self.assertEqual(replacements["__HANALL_SCOPE_CONTEXT__"], "watch_scope: ...")
        self.assertEqual(replacements["__HANALL_RESEARCH_PACKET__"], "stage1 packet")
        mocked_packet.assert_called_once_with(now)
        mocked_scope_context.assert_called_once()
        mocked_known_events.assert_called_once_with("2026-03-27", current_now=now)

    @patch("server.application.hanall_research._load_hanall_known_event_overrides")
    def test_known_events_context_normalizes_past_due_event_wording(self, mocked_loader) -> None:
        mocked_loader.return_value = [
            {
                "entity": "HanAll Biopharma",
                "category": "shareholder_meeting",
                "scheduled_for_kst": "2026-03-26 09:00 KST",
                "fact": "제53기 정기주주총회가 오늘 개최 예정임",
                "basis": "fixture basis",
                "primary_source": "fixture source",
                "status_note": "오늘 예정, 결과 공시 추가 확인 필요",
            }
        ]
        now = datetime(2026, 3, 29, 9, 0, tzinfo=now_kst().tzinfo)

        events = get_hanall_known_events("2026-03-29", current_now=now)
        context = build_hanall_known_events_context("2026-03-29", current_now=now)

        self.assertEqual(events[0]["aging_status"], "past_due_without_followup")
        self.assertEqual(events[0]["fact"], "HanAll Biopharma 일정 예정일 경과, 후속 공시 확인 필요")
        self.assertIn("예정일 경과, 후속 공시 확인 필요", context)
        self.assertNotIn("오늘 개최 예정", context)


class HanallPromptLoadingTest(unittest.TestCase):
    def test_hanall_prompts_are_split_between_collect_and_finalize(self) -> None:
        collect_prompt = get_prompt("hanall_news_collect_prompt")
        finalize_prompt = get_prompt("hanall_news_finalize_prompt")
        legacy_prompt = get_prompt("hanall_news_prompt")

        self.assertEqual(collect_prompt["feature_key"], "hanall_news_brief")
        self.assertFalse(collect_prompt.get("tools"))
        self.assertIn("__STAGE1_CANDIDATE_PAYLOAD_JSON__", collect_prompt["template"])
        self.assertIn("company_direct_confirmed_ids", collect_prompt["template"])
        self.assertNotIn("today_scheduled_events\": [StageFinding]", collect_prompt["template"])
        self.assertEqual(finalize_prompt["tools"], [{"google_search": {}}])
        self.assertNotIn("tools", legacy_prompt)

    def test_base_prompt_replacements_include_shared_rule_blocks(self) -> None:
        replacements = build_hanall_base_prompt_replacements(datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))

        self.assertIn("__HANALL_COMMON_RULES_CONTEXT__", replacements)
        self.assertIn("__HANALL_FINAL_OUTPUT_CONTEXT__", replacements)
        self.assertIn("final_section_order", replacements["__HANALL_FINAL_OUTPUT_CONTEXT__"])


class HanallSchemaAndParsingTest(unittest.TestCase):
    def test_raw_finding_converts_utc_to_kst(self) -> None:
        finding = RawFinding(
            source_family="sec",
            source_name="sec_api",
            entity="Immunovant",
            category="company_direct",
            title="Form 8-K filed",
            published_at=datetime(2026, 3, 26, 0, 0, tzinfo=timezone.utc),
            raw_payload={},
        )

        self.assertEqual(finding.published_at_kst, "2026-03-26 09:00 KST")

    def test_stage1_parsing_drops_invalid_items_and_stabilizes_defaults(self) -> None:
        raw_text = """
        {
          "company_direct_confirmed": [
            {"entity": "Immunovant", "category": "company_direct", "title": "ok"},
            {"entity": "broken"}
          ],
          "coverage": {"level": "Medium"}
        }
        """
        official_collection = OfficialCollectionResult(
            findings=[],
            checked_source_log=[
                CheckedSourceLogEntry(
                    source_family="sec",
                    source_name="sec_api",
                    status="checked",
                    checked_at_kst="2026-03-27 09:00 KST",
                    note="items=0",
                    endpoint="https://api.sec-api.io",
                )
            ],
            coverage_gaps=[
                CoverageGap(
                    source_family="sec",
                    source_name="sec_api",
                    gap_type="http_403_forbidden",
                    detail="403",
                    endpoint="https://api.sec-api.io/form-8k",
                )
            ],
        )

        parsed = _parse_stage1_output(raw_text)
        stabilized = _stabilize_stage1_output(
            stage1_output=parsed,
            official_collection=official_collection,
            current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo),
        )

        self.assertEqual(len(parsed.company_direct_confirmed), 1)
        self.assertEqual(len(stabilized.checked_source_log), 1)
        self.assertEqual(len(stabilized.coverage_gaps), 1)
        self.assertGreaterEqual(len(stabilized.search_tasks), 1)
        self.assertGreaterEqual(len(stabilized.omission_audit), 1)

    def test_stage1_alias_normalization_maps_live_like_fields_to_canonical_keys(self) -> None:
        raw_text = """
        {
          "coverage": {"level": "Medium", "rationale": "alias test"},
          "today_scheduled_events": [
            {
              "fact": "HanAll Biopharma 일정 예정일 경과, 후속 공시 확인 필요",
              "scheduled_for_kst": "2026-03-26 09:00 KST",
              "entity": "HanAll Biopharma"
            }
          ],
          "checked_source_log": [
            {
              "source_name": "sec_api",
              "status": "checked",
              "checked_at_kst": "2026-03-29 09:00 KST",
              "note": "items=0",
              "endpoint": "https://api.sec-api.io"
            }
          ],
          "coverage_gaps": [
            {
              "source_name": "mfds",
              "gap_type": "disabled",
              "reason": "collector disabled by config",
              "severity": "low",
              "endpoint": "http://apis.data.go.kr/mfds"
            }
          ],
          "omission_audit": [
            {
              "check_point": "주주총회 후속 공시",
              "detail": "예정일 경과 후 후속 공시 미발견",
              "status": "open"
            }
          ]
        }
        """

        parsed, invalid_count = _parse_stage1_output_with_diagnostics(raw_text)

        self.assertEqual(invalid_count, 0)
        self.assertEqual(parsed.today_scheduled_events[0].category, "scheduled_event")
        self.assertEqual(parsed.today_scheduled_events[0].title, "HanAll Biopharma 일정 예정일 경과, 후속 공시 확인 필요")
        self.assertEqual(parsed.checked_source_log[0].source_family, "sec")
        self.assertEqual(parsed.coverage_gaps[0].source_family, "mfds")
        self.assertEqual(parsed.coverage_gaps[0].detail, "collector disabled by config")
        self.assertEqual(parsed.omission_audit[0].topic, "주주총회 후속 공시")

    def test_stage1_invalid_item_logging_uses_missing_fields_and_raw_keys_only(self) -> None:
        raw_text = """
        {
          "coverage": {"level": "Low"},
          "omission_audit": [
            {
              "check_point": "secret phrase should not leak",
              "status": "open"
            }
          ]
        }
        """

        with self.assertLogs("server.application.hanall_news_pipeline", level="WARNING") as logs:
            _, invalid_count = _parse_stage1_output_with_diagnostics(raw_text)

        joined_logs = "\n".join(logs.output)
        self.assertEqual(invalid_count, 1)
        self.assertIn("missing_required_fields=detail", joined_logs)
        self.assertIn("raw_keys=check_point,status", joined_logs)
        self.assertNotIn("secret phrase should not leak", joined_logs)


class HanallDedupeTest(unittest.TestCase):
    def test_dedupe_keeps_distinct_document_ids_from_same_endpoint(self) -> None:
        findings = [
            RawFinding(
                source_family="openfda",
                source_name="openfda",
                entity="Immunovant",
                category="company_direct",
                title="A",
                document_id="doc-1",
                primary_source_url="https://api.fda.gov/drug/label.json",
                raw_payload={},
            ),
            RawFinding(
                source_family="openfda",
                source_name="openfda",
                entity="Immunovant",
                category="company_direct",
                title="B",
                document_id="doc-2",
                primary_source_url="https://api.fda.gov/drug/label.json",
                raw_payload={},
            ),
            RawFinding(
                source_family="openfda",
                source_name="openfda",
                entity="Immunovant",
                category="company_direct",
                title="B",
                document_id="doc-2",
                primary_source_url="https://api.fda.gov/drug/label.json",
                raw_payload={},
            ),
        ]

        deduped = _dedupe_findings(findings)

        self.assertEqual(len(deduped), 2)


class HanallStage1OverlayMergeTest(unittest.TestCase):
    @staticmethod
    def _base_collection() -> OfficialCollectionResult:
        return OfficialCollectionResult(
            findings=[
                RawFinding(
                    source_family="clinicaltrials",
                    source_name="clinicaltrials",
                    entity="Immunovant",
                    entity_type="company",
                    category="company_direct",
                    title="IMVT-1401 registry update",
                    summary="registry updated",
                    published_at=datetime(2026, 3, 27, 8, 0, tzinfo=now_kst().tzinfo),
                    trial_id="NCT12345678",
                    asset="IMVT-1401",
                    indication="gMG",
                    primary_source_url="https://clinicaltrials.gov/api/v2/studies/NCT12345678",
                    confidence=0.4,
                )
            ],
            checked_source_log=[
                CheckedSourceLogEntry(
                    source_family="clinicaltrials",
                    source_name="clinicaltrials",
                    status="checked",
                    checked_at_kst="2026-03-27 09:00 KST",
                    note="items=1",
                    endpoint="https://clinicaltrials.gov/api/v2/studies",
                )
            ],
            coverage_gaps=[
                CoverageGap(
                    source_family="sec",
                    source_name="sec_api",
                    gap_type="http_503_unavailable",
                    detail="503",
                    endpoint="https://api.sec-api.io/form-8k",
                )
            ],
        )

    def test_overlay_id_merge_reclassifies_valid_candidate(self) -> None:
        base = build_stage1_deterministic_base(
            official_collection=self._base_collection(),
            current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo),
        )
        candidate_id = base.candidate_order[0]
        overlay = HanallStage1OverlayOutput(company_direct_confirmed_ids=[candidate_id])

        merged, invalid_ref_count, had_effect = _merge_stage1_overlay(
            deterministic_base=base,
            overlay=overlay,
        )

        self.assertTrue(had_effect)
        self.assertEqual(invalid_ref_count, 0)
        self.assertEqual(len(merged.company_direct_confirmed), 1)
        self.assertEqual(merged.company_direct_confirmed[0].candidate_id, candidate_id)
        self.assertEqual(merged.unverified_leads, [])

    def test_unknown_overlay_ids_are_ignored_safely(self) -> None:
        base = build_stage1_deterministic_base(
            official_collection=self._base_collection(),
            current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo),
        )
        overlay = HanallStage1OverlayOutput(company_direct_confirmed_ids=["cand_unknown"])

        merged, invalid_ref_count, had_effect = _merge_stage1_overlay(
            deterministic_base=base,
            overlay=overlay,
        )

        self.assertFalse(had_effect)
        self.assertEqual(invalid_ref_count, 1)
        self.assertEqual(len(merged.checked_source_log), len(base.output.checked_source_log))
        self.assertEqual(len(merged.coverage_gaps), len(base.output.coverage_gaps))
        self.assertEqual(len(merged.unverified_leads), 1)

    def test_overlay_cannot_overwrite_code_owned_deterministic_sections(self) -> None:
        base = build_stage1_deterministic_base(
            official_collection=self._base_collection(),
            current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo),
        )
        raw_overlay = json.dumps(
            {
                "company_direct_confirmed_ids": [base.candidate_order[0]],
                "today_scheduled_events": [{"entity": "overwrite attempt"}],
                "checked_source_log": [{"source_name": "overwrite"}],
                "coverage_gaps": [{"gap_type": "overwrite"}],
            }
        )

        overlay, invalid_item_count = _parse_stage1_overlay_with_diagnostics(raw_overlay)
        merged, _, _ = _merge_stage1_overlay(deterministic_base=base, overlay=overlay)

        self.assertEqual(invalid_item_count, 0)
        self.assertEqual(merged.today_scheduled_events, base.output.today_scheduled_events)
        self.assertEqual(merged.checked_source_log, base.output.checked_source_log)
        self.assertEqual(merged.coverage_gaps, base.output.coverage_gaps)


class BuildHanallNewsBriefTest(unittest.TestCase):
    @staticmethod
    def _build_pipeline_result(raw_text: str) -> HanallNewsPipelineResult:
        return HanallNewsPipelineResult(
            final_text=raw_text,
            raw_output_text=raw_text,
            stage1_output=HanallStage1StructuredOutput(coverage=CoverageSummary(level="Medium", rationale="test")),
            official_collection=OfficialCollectionResult(),
            rss_collection=RSSCollectionResult(),
        )

    @staticmethod
    def _build_valid_hanall_text() -> str:
        return """[한올/Immunovant 24시간 브리핑]
기준: 2026-03-26 09:00 KST
범위: 2026-03-25 09:00 KST ~ 2026-03-26 09:00 KST
커버리지: High (회사 및 경쟁사 공식 소스를 점검함)
확인 이벤트: 총 2건, 회사 1건, 경쟁사 1건
오늘 예정 이벤트: 1건

요약
- Immunovant 직접 업데이트 1건 확인
- FcRn 경쟁사 업데이트 1건 확인
- 결론: 직접 업데이트와 경쟁사 이벤트가 각각 1건 확인됨

오늘 예정 이벤트
- [HIGH] HanAll Biopharma | shareholder_meeting | 2026-03-26 09:00 KST
사실: 제53기 정기주주총회가 오늘 개최 예정임
근거: 2026-02-25 주주총회소집공고 기준 일정 확인
출처: DART 주주총회소집공고
상태: 오늘 예정, 결과 공시 추가 확인 필요

1. 회사 업데이트
- [HIGH] Immunovant | press_release | 2026-03-26 08:30 KST
사실: 신규 기업 업데이트가 게시됨
수치/날짜: 2026-03-26 08:30 KST 게시
출처: Immunovant PR

2. 경쟁사 업데이트
- [MEDIUM] argenx | efgartigimod | gMG | 2026-03-26 07:00 KST
사실: 경쟁사 관련 공식 이벤트가 확인됨
연결 자산: IMVT-1401
경쟁 구분: Direct class
수치/날짜: gMG 관련 일정 업데이트
왜 중요한가: 같은 FcRn 축 비교에 참고 가능
출처: argenx IR

Competitor Map Snapshot
- argenx | efgartigimod | gMG | FcRn read-through

Checked Source Log
- clinicaltrials/clinicaltrials | checked | 2026-03-26 09:00 KST | status_code=200

3. 미확인 단서
- 없음

Coverage Gaps
- 없음

Omission Audit
- 없음

검증 메모
- 점검 상태: 회사 IR done, 규제/거래소 done, 임상등록 done, 경쟁사 공식소스 done
- 접근 제한: 특이사항 없음
- 경쟁사 커버리지 공백: 낮음
- 확인 소스 로그: 4건
- 확인 소스 예시: Immunovant PR:new, SEC Form 8-K:no_new"""

    @patch(
        "server.application.news.run_hanall_news_pipeline",
        side_effect=TimeoutError("background 응답 polling timeout status=in_progress"),
    )
    def test_returns_fallback_message_on_timeout(self, mocked_pipeline) -> None:
        result = build_hanall_news_brief()
        self.assertIn("한올 뉴스 브리핑 생성이 아직 끝나지 않았습니다.", result)
        mocked_pipeline.assert_called_once()

    @patch("server.application.news.run_hanall_news_pipeline", side_effect=requests.RequestException("network down"))
    def test_returns_fallback_message_on_request_error(self, mocked_pipeline) -> None:
        result = build_hanall_news_brief()
        self.assertIn("한올 뉴스 브리핑을 지금 가져오지 못했습니다.", result)
        mocked_pipeline.assert_called_once()

    @patch("server.application.news.run_hanall_news_pipeline")
    def test_returns_plain_text_brief(self, mocked_pipeline) -> None:
        mocked_pipeline.return_value = self._build_pipeline_result(self._build_valid_hanall_text())

        result = build_hanall_news_brief()

        self.assertIn("[한올/Immunovant 24시간 브리핑]", result)
        self.assertIn("회사 직접 업데이트", result)
        self.assertIn("경쟁사 관련 업데이트", result)
        self.assertIn("오늘 예정 이벤트", result)
        self.assertIn("정기주주총회", result)
        self.assertIn("Immunovant", result)
        self.assertIn("argenx", result)
        self.assertNotIn("```", result)
        self.assertTrue(_final_text_has_required_sections(result))

    @patch("server.application.news.run_hanall_news_pipeline")
    def test_strips_code_fence_wrapper(self, mocked_pipeline) -> None:
        mocked_pipeline.return_value = self._build_pipeline_result(f"```text\n{self._build_valid_hanall_text()}\n```")
        result = build_hanall_news_brief()
        self.assertTrue(result.startswith("[한올/Immunovant 24시간 브리핑]"))
        self.assertNotIn("```", result)

    @patch("server.application.news.run_hanall_news_pipeline")
    def test_returns_fallback_message_on_empty_text(self, mocked_pipeline) -> None:
        mocked_pipeline.return_value = self._build_pipeline_result("   ")
        result = build_hanall_news_brief()
        self.assertIn("한올 뉴스 브리핑 텍스트 정리에 실패했습니다.", result)
        mocked_pipeline.assert_called_once()

    @patch("server.application.news.deliver_room_messages")
    @patch("server.application.news.run_hanall_news_pipeline")
    def test_enqueues_raw_payload_to_admin_before_render(self, mocked_pipeline, mocked_deliver) -> None:
        mocked_pipeline.return_value = self._build_pipeline_result(self._build_valid_hanall_text())

        result = build_hanall_news_brief(
            room_key="stock_openchat_news",
            send_raw_to_admin=True,
        )

        self.assertIn("[한올/Immunovant 24시간 브리핑]", result)
        mocked_pipeline.assert_called_once()
        mocked_deliver.assert_called_once()
        self.assertEqual(mocked_deliver.call_args.kwargs["room_key"], "admin_test_room")
        self.assertEqual(mocked_deliver.call_args.kwargs["source_type"], "admin:hanall_news_raw")
        raw_message = mocked_deliver.call_args.kwargs["message"]
        self.assertIn("[hanall_news raw]", raw_message)
        self.assertIn("payload_kind: text", raw_message)
        self.assertIn("Immunovant", raw_message)


class HanallNewsPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_stage2_rate_limit_state()

    def tearDown(self) -> None:
        _reset_stage2_rate_limit_state()

    @staticmethod
    def _official_collection() -> OfficialCollectionResult:
        return OfficialCollectionResult(
            findings=[
                RawFinding(
                    source_family="clinicaltrials",
                    source_name="clinicaltrials",
                    entity="Immunovant",
                    entity_type="company",
                    category="company_direct",
                    title="IMVT-1401 study updated",
                    summary="Clinical trial registry updated",
                    published_at=datetime(2026, 3, 26, 20, 0, tzinfo=now_kst().tzinfo),
                    trial_id="NCT12345678",
                    asset="IMVT-1401",
                    indication="gMG",
                    primary_source_url="https://clinicaltrials.gov/api/v2/studies/NCT12345678",
                    confidence=0.9,
                )
            ],
            checked_source_log=[
                CheckedSourceLogEntry(
                    source_family="clinicaltrials",
                    source_name="clinicaltrials",
                    status="checked",
                    checked_at_kst="2026-03-27 09:00 KST",
                    note="items=1",
                    endpoint="https://clinicaltrials.gov/api/v2/studies",
                )
            ],
            coverage_gaps=[
                CoverageGap(
                    source_family="sec",
                    source_name="sec_api",
                    gap_type="adapter_todo",
                    detail="sec-api adapter scaffolded",
                    severity="medium",
                    endpoint="https://api.sec-api.io/form-8k",
                )
            ],
        )

    @staticmethod
    def _rss_collection() -> RSSCollectionResult:
        return RSSCollectionResult(checked_feed_count=1)

    @staticmethod
    def _stage1_overlay_json() -> str:
        base = build_stage1_deterministic_base(
            official_collection=HanallNewsPipelineTest._official_collection(),
            current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo),
        )
        payload = {
            "company_direct_confirmed_ids": [base.candidate_order[0]],
            "coverage": {
                "level": "Medium",
                "rationale": "official finding 1건, collector gap 1건",
            },
            "search_tasks": [
                {
                    "topic": "IMVT-1401 study updated",
                    "reason": "check whether matching official company communication exists",
                    "priority": "high",
                    "recommended_queries": ["Immunovant IMVT-1401 gMG"],
                    "preferred_source_types": ["official_site", "registry"],
                    "entity": "Immunovant",
                    "asset": "IMVT-1401",
                    "indication": "gMG",
                }
            ],
        }
        return json.dumps(payload)

    @staticmethod
    def _http_429_error() -> requests.HTTPError:
        response = requests.Response()
        response.status_code = 429
        response.url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"
        response._content = b"Too Many Requests"
        response.encoding = "utf-8"
        return requests.HTTPError("429 Client Error: Too Many Requests for url: https://example.com", response=response)

    @staticmethod
    def _http_503_error() -> requests.HTTPError:
        response = requests.Response()
        response.status_code = 503
        response.url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"
        response._content = b"Service Unavailable"
        response.encoding = "utf-8"
        return requests.HTTPError("503 Server Error: Service Unavailable for url: https://example.com", response=response)

    @patch("server.application.hanall_news_pipeline.fetch_hanall_rss_results")
    @patch("server.application.hanall_news_pipeline.collect_hanall_official_findings")
    @patch("server.application.hanall_news_pipeline.run_prompt_by_key_raw")
    def test_pipeline_runs_collect_then_finalize(
        self,
        mocked_run_prompt,
        mocked_collect,
        mocked_rss,
    ) -> None:
        mocked_collect.return_value = self._official_collection()
        mocked_rss.return_value = self._rss_collection()
        valid_final_text = """[한올/Immunovant 24시간 브리핑]
기준: 2026-03-27 09:00 KST
범위: 2026-03-26 09:00 KST ~ 2026-03-27 09:00 KST
커버리지: Medium (official APIs returned some findings but coverage gaps remain)
확인 이벤트: 총 1건, 회사 1건, 경쟁사 0건
오늘 예정 이벤트: 0건

요약
- 지난 24시간 내 Confirmed 업데이트 총수: 1

오늘 예정 이벤트
- 없음

Confirmed Updates — Company Direct
- Immunovant | company_direct | 2026-03-26 20:00 KST
사실: IMVT-1401 study updated

Confirmed Updates — Competitor Relevant
- 없음

Competitor Map Snapshot
- 없음

Checked Source Log
- 없음

Unverified Leads
- 없음

Coverage Gaps
- 없음

Omission Audit
- 없음

검증 메모
- 확인 소스 로그: 1건"""
        mocked_run_prompt.side_effect = [
            self._stage1_overlay_json(),
            valid_final_text,
        ]

        result = run_hanall_news_pipeline(current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))

        self.assertFalse(result.used_stage1_fallback)
        self.assertFalse(result.used_stage2_fallback)
        self.assertEqual(result.stage1_mode, "llm_overlay_merged")
        self.assertEqual(result.stage1_attempts, 1)
        self.assertEqual(result.stage1_invalid_item_count, 0)
        self.assertEqual(result.stage1_invalid_ref_count, 0)
        self.assertEqual(mocked_run_prompt.call_args_list[0].args[0], "hanall_news_collect_prompt")
        self.assertEqual(mocked_run_prompt.call_args_list[1].args[0], "hanall_news_finalize_prompt")
        self.assertIn('"candidate_count"', mocked_run_prompt.call_args_list[0].kwargs["replacements"]["__STAGE1_CANDIDATE_PAYLOAD_JSON__"])
        self.assertIn("IMVT-1401", mocked_run_prompt.call_args_list[1].kwargs["replacements"]["__STAGE1_JSON__"])
        self.assertIn("[한올/Immunovant 24시간 브리핑]", result.final_text)

    @patch("server.application.hanall_news_pipeline.fetch_hanall_rss_results")
    @patch("server.application.hanall_news_pipeline.collect_hanall_official_findings")
    @patch("server.application.hanall_news_pipeline.run_prompt_by_key_raw")
    def test_pipeline_uses_stage2_fallback_text_when_finalize_fails(
        self,
        mocked_run_prompt,
        mocked_collect,
        mocked_rss,
    ) -> None:
        mocked_collect.return_value = self._official_collection()
        mocked_rss.return_value = self._rss_collection()
        mocked_run_prompt.side_effect = [
            self._stage1_overlay_json(),
            ValueError("stage2 failed"),
        ]

        result = run_hanall_news_pipeline(current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))

        self.assertTrue(result.used_stage2_fallback)
        self.assertIn("회사 직접 업데이트", result.final_text)
        self.assertIn("참고 메모", result.final_text)

    @patch("server.application.hanall_news_pipeline._stage1_retry_sleep")
    @patch("server.application.hanall_news_pipeline.fetch_hanall_rss_results")
    @patch("server.application.hanall_news_pipeline.collect_hanall_official_findings")
    @patch("server.application.hanall_news_pipeline.run_prompt_by_key_raw")
    def test_deterministic_base_survives_stage1_503(
        self,
        mocked_run_prompt,
        mocked_collect,
        mocked_rss,
        mocked_sleep,
    ) -> None:
        mocked_collect.return_value = self._official_collection()
        mocked_rss.return_value = self._rss_collection()
        mocked_run_prompt.side_effect = [
            self._http_503_error(),
            self._http_503_error(),
            """[한올/Immunovant 24시간 브리핑]
기준: 2026-03-27 09:00 KST
범위: 2026-03-26 09:00 KST ~ 2026-03-27 09:00 KST
커버리지: Medium (deterministic base retained)
확인 이벤트: 총 1건, 회사 1건, 경쟁사 0건
오늘 예정 이벤트: 0건

요약
- deterministic base retained

오늘 예정 이벤트
- 없음

Confirmed Updates — Company Direct
- Immunovant | company_direct | 2026-03-26 20:00 KST
사실: IMVT-1401 study updated

Confirmed Updates — Competitor Relevant
- 없음

Competitor Map Snapshot
- 없음

Checked Source Log
- 없음

Unverified Leads
- 없음

Coverage Gaps
- 없음

Omission Audit
- 없음

검증 메모
- 확인 소스 로그: 1건""",
        ]

        result = run_hanall_news_pipeline(current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))

        self.assertTrue(result.used_stage1_fallback)
        self.assertEqual(result.stage1_mode, "deterministic_base_due_to_llm_error")
        self.assertEqual(result.stage1_attempts, 2)
        self.assertGreaterEqual(result.stage1_candidate_count, 1)
        self.assertEqual(result.stage1_invalid_ref_count, 0)
        self.assertEqual(len(result.stage1_output.checked_source_log), 1)
        self.assertEqual(len(result.stage1_output.coverage_gaps), 1)
        self.assertEqual(len(result.stage1_output.company_direct_confirmed), 1)
        mocked_sleep.assert_called_once()

    @patch("server.application.hanall_news_pipeline._stage2_retry_sleep")
    @patch("server.application.hanall_news_pipeline.fetch_hanall_rss_results")
    @patch("server.application.hanall_news_pipeline.collect_hanall_official_findings")
    @patch("server.application.hanall_news_pipeline.run_prompt_by_key_raw")
    def test_stage2_429_retries_then_uses_deterministic_rate_limit_path(
        self,
        mocked_run_prompt,
        mocked_collect,
        mocked_rss,
        mocked_sleep,
    ) -> None:
        _reset_stage2_rate_limit_state()
        mocked_collect.return_value = self._official_collection()
        mocked_rss.return_value = self._rss_collection()
        mocked_run_prompt.side_effect = [
            self._stage1_overlay_json(),
            self._http_429_error(),
            self._http_429_error(),
        ]

        result = run_hanall_news_pipeline(current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))

        self.assertTrue(result.used_stage2_fallback)
        self.assertEqual(result.render_mode, "deterministic_due_to_rate_limit")
        self.assertEqual(result.stage2_attempts, 2)
        self.assertEqual(result.stage2_skipped_reason, "rate_limit_after_retry")
        self.assertIn("회사 직접 업데이트", result.final_text)
        mocked_sleep.assert_called_once()
        self.assertEqual(mocked_run_prompt.call_count, 3)
        _reset_stage2_rate_limit_state()

    @patch("server.application.hanall_news_pipeline.get_hanall_known_events")
    @patch("server.application.hanall_news_pipeline.fetch_hanall_rss_results")
    @patch("server.application.hanall_news_pipeline.collect_hanall_official_findings")
    @patch("server.application.hanall_news_pipeline.run_prompt_by_key_raw")
    def test_sparse_no_news_path_skips_stage2_and_renders_known_event_naturally(
        self,
        mocked_run_prompt,
        mocked_collect,
        mocked_rss,
        mocked_known_events,
    ) -> None:
        _reset_stage2_rate_limit_state()
        mocked_collect.return_value = OfficialCollectionResult(
            findings=[],
            checked_source_log=[
                CheckedSourceLogEntry(
                    source_family="sec",
                    source_name="sec_api",
                    status="checked",
                    checked_at_kst="2026-03-27 09:00 KST",
                    note="items=0",
                    endpoint="https://api.sec-api.io",
                )
            ],
        )
        mocked_rss.return_value = RSSCollectionResult()
        mocked_known_events.return_value = [
            {
                "entity": "HanAll Biopharma",
                "category": "shareholder_meeting",
                "fact": "HanAll Biopharma 일정 예정일 경과, 후속 공시 확인 필요",
                "basis": "fixture basis",
                "primary_source": "fixture source",
                "status_note": "예정일 경과, 후속 공시 확인 필요",
                "scheduled_for_kst": "2026-03-26 09:00 KST",
            }
        ]
        result = run_hanall_news_pipeline(current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))

        self.assertTrue(result.used_stage2_fallback)
        self.assertEqual(result.render_mode, "deterministic_due_to_sparse_stage1")
        self.assertEqual(result.stage2_attempts, 0)
        self.assertEqual(result.stage2_skipped_reason, "sparse_stage1_no_updates")
        self.assertIn("지난 24시간 내 확인된 핵심 업데이트 없음", result.final_text)
        self.assertIn("예정일 경과, 후속 공시 확인 필요", result.final_text)
        self.assertEqual(result.stage1_mode, "deterministic_base_only_sparse")
        self.assertEqual(result.stage1_attempts, 0)
        self.assertEqual(mocked_run_prompt.call_count, 0)
        _reset_stage2_rate_limit_state()

    @patch("server.application.hanall_news_pipeline._stage2_retry_sleep")
    @patch("server.application.hanall_news_pipeline.fetch_hanall_rss_results")
    @patch("server.application.hanall_news_pipeline.collect_hanall_official_findings")
    @patch("server.application.hanall_news_pipeline.run_prompt_by_key_raw")
    def test_same_input_rate_limit_cooldown_skips_repeat_stage2_calls(
        self,
        mocked_run_prompt,
        mocked_collect,
        mocked_rss,
        mocked_sleep,
    ) -> None:
        _reset_stage2_rate_limit_state()
        mocked_collect.return_value = self._official_collection()
        mocked_rss.return_value = self._rss_collection()
        mocked_run_prompt.side_effect = [
            self._stage1_overlay_json(),
            self._http_429_error(),
            self._http_429_error(),
            self._stage1_overlay_json(),
        ]

        first = run_hanall_news_pipeline(current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))
        second = run_hanall_news_pipeline(current_now=datetime(2026, 3, 27, 9, 1, tzinfo=now_kst().tzinfo))

        self.assertEqual(first.render_mode, "deterministic_due_to_rate_limit")
        self.assertEqual(second.render_mode, "deterministic_due_to_rate_limit")
        self.assertEqual(second.stage2_attempts, 0)
        self.assertEqual(second.stage2_skipped_reason, "rate_limit_cooldown_active_same_input")
        self.assertEqual(mocked_run_prompt.call_count, 4)
        mocked_sleep.assert_called_once()
        _reset_stage2_rate_limit_state()


class HanallFinalRenderAndCollectorSafetyTest(unittest.TestCase):
    def test_collect_hanall_official_findings_filters_out_of_window_and_low_precision_noise(self) -> None:
        current_now = datetime(2026, 3, 29, 9, 0, tzinfo=now_kst().tzinfo)

        class MixedCollector:
            source_name = "sec_api"
            source_family = "sec"

            def __init__(self, settings, collector_config) -> None:
                self.settings = settings
                self.collector_config = collector_config

            def collect(self, session, *, current_now=None):
                return OfficialCollectionResult(
                    findings=[
                        RawFinding(
                            source_family="sec",
                            source_name="sec_api",
                            entity="Immunovant",
                            entity_type="company",
                            category="company_direct",
                            title="Old SEC filing",
                            published_at=datetime(2025, 3, 29, 9, 0, tzinfo=now_kst().tzinfo),
                            document_id="old-sec",
                            primary_source_url="https://example.com/old-sec",
                        ),
                        RawFinding(
                            source_family="sec",
                            source_name="sec_api",
                            entity="Immunovant",
                            entity_type="company",
                            category="company_direct",
                            title="Recent SEC filing",
                            published_at=current_now - timedelta(hours=2),
                            document_id="recent-sec",
                            primary_source_url="https://example.com/recent-sec",
                        ),
                    ]
                )

        class CrossrefNoiseCollector:
            source_name = "crossref"
            source_family = "crossref"

            def __init__(self, settings, collector_config) -> None:
                self.settings = settings
                self.collector_config = collector_config

            def collect(self, session, *, current_now=None):
                return OfficialCollectionResult(
                    findings=[
                        RawFinding(
                            source_family="crossref",
                            source_name="crossref",
                            entity="Crossref",
                            entity_type="company",
                            category="company_direct",
                            title="iMVT receptor paper",
                            summary="generic acronym usage in immunology literature",
                            published_at=current_now - timedelta(hours=1),
                            document_id="10.1000/noise",
                            primary_source_url="https://doi.org/10.1000/noise",
                        ),
                        RawFinding(
                            source_family="crossref",
                            source_name="crossref",
                            entity="Crossref",
                            entity_type="company",
                            category="company_direct",
                            title="Batoclimab FcRn review",
                            summary="Immunovant program update",
                            published_at=current_now - timedelta(hours=3),
                            document_id="10.1000/relevant",
                            primary_source_url="https://doi.org/10.1000/relevant",
                        ),
                    ]
                )

        with patch(
            "server.infra.hanall_news_collectors.COLLECTOR_CLASSES",
            {
                "sec_api": MixedCollector,
                "crossref": CrossrefNoiseCollector,
            },
        ):
            result = collect_hanall_official_findings(current_now=current_now)

        titles = {finding.title for finding in result.findings}
        self.assertIn("Recent SEC filing", titles)
        self.assertIn("Batoclimab FcRn review", titles)
        self.assertNotIn("Old SEC filing", titles)
        self.assertNotIn("iMVT receptor paper", titles)

    def test_collect_hanall_official_findings_masks_secret_like_exception_text(self) -> None:
        class ExplodingCollector:
            source_name = "exploding"
            source_family = "test"

            def __init__(self, settings, collector_config) -> None:
                self.settings = settings
                self.collector_config = collector_config

            def collect(self, session, *, current_now=None):
                raise requests.RequestException(
                    "GET https://example.com?api_key=secret123&serviceKey=svc123&crtfc_key=dart123&token=tok123"
                )

        with patch("server.infra.hanall_news_collectors.COLLECTOR_CLASSES", {"exploding": ExplodingCollector}):
            result = collect_hanall_official_findings(current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))

        note = result.checked_source_log[0].note
        detail = result.coverage_gaps[0].detail
        self.assertNotIn("secret123", note)
        self.assertNotIn("svc123", detail)
        self.assertNotIn("dart123", detail)
        self.assertNotIn("tok123", detail)
        self.assertIn("api_key=***", note)
        self.assertIn("serviceKey=***", detail)
        self.assertIn("crtfc_key=***", detail)
        self.assertIn("token=***", detail)

    def test_stage1_fallback_output_routes_low_confidence_items_to_unverified_leads(self) -> None:
        current_now = datetime(2026, 3, 29, 9, 0, tzinfo=now_kst().tzinfo)
        finding = RawFinding(
            source_family="sec",
            source_name="sec_api",
            entity="Immunovant",
            entity_type="company",
            category="company_direct",
            title="Recent SEC filing",
            summary="8-K update",
            published_at=current_now - timedelta(hours=2),
            document_id="recent-sec",
            primary_source_url="https://example.com/recent-sec",
            confidence=0.6,
        )

        stage1_output = build_stage1_fallback_output(
            official_collection=OfficialCollectionResult(findings=[finding]),
            current_now=current_now,
        )

        self.assertEqual(stage1_output.company_direct_confirmed, [])
        self.assertEqual(len(stage1_output.unverified_leads), 1)

    def test_normalize_hanall_final_text_rewrites_heading_aliases(self) -> None:
        raw_text = """```text
[한올/Immunovant 24시간 브리핑]

1. 요약
- line

2. 오늘 예정 이벤트

3. 회사 업데이트
- company

4. 경쟁사 업데이트
- competitor

5. Competitor Map Snapshot

6. Checked Source Log

7. 미확인 단서

8. Coverage Gaps

9. Omission Audit

10. 검증 메모
```"""

        normalized = normalize_hanall_final_text(raw_text)

        self.assertIn("회사 직접 업데이트", normalized)
        self.assertIn("경쟁사 관련 업데이트", normalized)
        self.assertIn("추가 확인 필요", normalized)
        self.assertTrue(_final_text_has_required_sections(normalized))

    def test_fallback_renderer_emits_required_sections_in_order(self) -> None:
        stage1_output = HanallStage1StructuredOutput(
            coverage=CoverageSummary(level="Medium", rationale="test rationale"),
            company_direct_confirmed=[
                {
                    "entity": "Immunovant",
                    "category": "company_direct",
                    "title": "IMVT-1401 update",
                    "published_at_kst": "2026-03-27 08:00 KST",
                }
            ],
            competitor_map_snapshot=[
                {
                    "competitor": "argenx",
                    "asset": "efgartigimod",
                    "indication": "gMG",
                    "relevance": "FcRn read-through",
                    "evidence_titles": ["argenx update"],
                }
            ],
            checked_source_log=[
                {
                    "source_family": "clinicaltrials",
                    "source_name": "clinicaltrials",
                    "status": "checked",
                    "checked_at_kst": "2026-03-27 09:00 KST",
                    "note": "items=1",
                    "endpoint": "https://clinicaltrials.gov/api/v2/studies",
                }
            ],
            coverage_gaps=[
                {
                    "source_family": "sec",
                    "source_name": "sec_api",
                    "gap_type": "http_403_forbidden",
                    "detail": "403",
                    "endpoint": "https://api.sec-api.io/form-8k",
                }
            ],
            omission_audit=[OmissionAuditEntry(topic="audit", detail="detail", status="open")],
        )
        official_collection = OfficialCollectionResult()
        rss_collection = RSSCollectionResult()

        text = render_stage1_fallback_text(
            stage1_output=stage1_output,
            official_collection=official_collection,
            rss_collection=rss_collection,
            current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo),
        )

        self.assertTrue(_final_text_has_required_sections(text))
        self.assertIn("회사 직접 업데이트", text)
        self.assertIn("아직 확인이 필요한 부분", text)
        self.assertIn("누락 점검", text)

    def test_collector_failure_is_captured_as_gap_in_orchestration(self) -> None:
        class ExplodingCollector:
            source_name = "exploding"
            source_family = "test"

            def __init__(self, settings, collector_config) -> None:
                self.settings = settings
                self.collector_config = collector_config

            def collect(self, session, *, current_now=None):
                raise RuntimeError("boom")

        with patch("server.infra.hanall_news_collectors.COLLECTOR_CLASSES", {"exploding": ExplodingCollector}):
            result = collect_hanall_official_findings(current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))

        self.assertEqual(len(result.findings), 0)
        self.assertEqual(result.checked_source_log[0].status, "collector_exception")
        self.assertEqual(result.coverage_gaps[0].gap_type, "collector_exception")

    def test_approval_gated_mfds_services_report_disabled_status(self) -> None:
        collector = MfdsCollector(
            settings=SimpleNamespace(data_go_kr_api_key="test-key"),
            collector_config={
                "enabled": True,
                "approval_gated": True,
                "services": {
                    "drug_safe_letter": False,
                },
                "endpoints": {
                    "drug_safe_letter": "http://apis.data.go.kr/1471000/DrugSafeLetterService02",
                },
            },
        )

        result = collector.collect(Mock(), current_now=datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo))

        self.assertEqual(result.checked_source_log[0].status, "approval_gated_disabled")
        self.assertEqual(result.coverage_gaps[0].gap_type, "approval_gated_disabled")


class PromptExecutionRoutingTest(unittest.TestCase):
    @patch("server.application.prompting.call_openai_text")
    @patch("server.application.prompting.call_gemini_text", return_value="gemini result")
    @patch("server.application.prompting._build_prompt_replacements", return_value={})
    @patch("server.application.prompting.render_prompt_template", return_value="instruction")
    @patch("server.application.prompting.get_llm_config", return_value={"model": "gemini-3-flash", "temperature": 0.2})
    @patch(
        "server.application.prompting.get_settings",
        return_value=SimpleNamespace(
            llm=SimpleNamespace(
                prompt_default_max_output_tokens=800,
                hanall_news_max_output_tokens=2200,
                hanall_news_truncate_limit=2400,
                openai_timeout_seconds=180,
                openai_poll_timeout_seconds=1800,
                gemini_timeout_seconds=60,
            )
        ),
    )
    @patch(
        "server.application.prompting.get_prompt",
        side_effect=lambda prompt_key: {
            "hanall_news_collect_prompt": {
                "title": "collect",
                "feature_key": "hanall_news_brief",
                "preserve_newlines": True,
            },
            "hanall_news_finalize_prompt": {
                "title": "finalize",
                "feature_key": "hanall_news_brief",
                "preserve_newlines": True,
                "tools": [{"google_search": {}}],
            },
        }[prompt_key],
    )
    def test_run_prompt_routes_finalize_prompt_to_gemini_with_google_search(
        self,
        mocked_prompt,
        mocked_settings,
        mocked_llm_config,
        mocked_render,
        mocked_replacements,
        mocked_gemini,
        mocked_openai,
    ) -> None:
        result = run_prompt_by_key_raw("hanall_news_finalize_prompt", replacements={"__NOW_KST__": "x"})

        self.assertEqual(result, "gemini result")
        mocked_gemini.assert_called_once()
        mocked_openai.assert_not_called()
        self.assertEqual(mocked_gemini.call_args.kwargs["tools"], [{"google_search": {}}])

    @patch("server.application.prompting.call_openai_text")
    @patch("server.application.prompting.call_gemini_text", return_value="gemini result")
    @patch("server.application.prompting._build_prompt_replacements", return_value={})
    @patch("server.application.prompting.render_prompt_template", return_value="instruction")
    @patch("server.application.prompting.get_llm_config", return_value={"model": "gemini-3-flash", "temperature": 0.2})
    @patch(
        "server.application.prompting.get_settings",
        return_value=SimpleNamespace(
            llm=SimpleNamespace(
                prompt_default_max_output_tokens=800,
                hanall_news_max_output_tokens=2200,
                hanall_news_truncate_limit=2400,
                openai_timeout_seconds=180,
                openai_poll_timeout_seconds=1800,
                gemini_timeout_seconds=60,
            )
        ),
    )
    @patch(
        "server.application.prompting.get_prompt",
        return_value={
            "title": "collect",
            "feature_key": "hanall_news_brief",
            "preserve_newlines": True,
        },
    )
    def test_run_prompt_routes_collect_prompt_without_tools(
        self,
        mocked_prompt,
        mocked_settings,
        mocked_llm_config,
        mocked_render,
        mocked_replacements,
        mocked_gemini,
        mocked_openai,
    ) -> None:
        result = run_prompt_by_key_raw("hanall_news_collect_prompt", replacements={"__NOW_KST__": "x"})

        self.assertEqual(result, "gemini result")
        mocked_gemini.assert_called_once()
        mocked_openai.assert_not_called()
        self.assertIsNone(mocked_gemini.call_args.kwargs["tools"])


class OpenAIClientStructuredOutputTest(unittest.TestCase):
    @patch("server.infra.llm_clients.requests.post")
    def test_retries_when_structured_output_is_incomplete(self, mocked_post) -> None:
        text_format = {
            "type": "json_schema",
            "name": "test_schema",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                },
                "required": ["ok"],
                "additionalProperties": False,
            },
        }
        first = Mock()
        first.raise_for_status.return_value = None
        first.json.return_value = {
            "status": "incomplete",
            "output_text": "{\"partial\": true}",
        }
        second = Mock()
        second.raise_for_status.return_value = None
        second.json.return_value = {
            "status": "completed",
            "output_text": "{\"ok\": true}",
        }
        mocked_post.side_effect = [first, second]

        text = call_openai_text(
            feature_key="hanall_news_brief",
            prompt_text="JSON으로 답해라.",
            max_output_tokens=100,
            model="gpt-5-mini",
            temperature=0.4,
            tools=[{"type": "web_search_preview"}],
            text_format=text_format,
            preserve_newlines=True,
            timeout=1,
            background=False,
            poll_timeout=1,
        )

        self.assertEqual(text, "{\"ok\": true}")
        self.assertEqual(mocked_post.call_count, 2)
        first_payload = mocked_post.call_args_list[0].kwargs["json"]
        self.assertEqual(first_payload["text"]["format"]["type"], "json_schema")
        self.assertEqual(first_payload["text"]["format"]["name"], "test_schema")


class GeminiClientPayloadTest(unittest.TestCase):
    @patch("server.infra.llm_clients.requests.post")
    @patch("server.infra.llm_clients.get_llm_config", return_value={"model": "gemini-2.5-flash", "temperature": 0.2})
    @patch(
        "server.infra.llm_clients.get_settings",
        return_value=SimpleNamespace(
            google_api_key="test-key",
            llm=SimpleNamespace(gemini_timeout_seconds=30),
        ),
    )
    def test_uses_thinking_budget_for_gemini_2_5(
        self,
        mocked_settings,
        mocked_llm_config,
        mocked_post,
    ) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        mocked_post.return_value = response

        result = call_gemini_text(
            feature_key="youtube_summary",
            prompt_text="요약해라",
            max_output_tokens=100,
        )

        self.assertEqual(result, "ok")
        payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(payload["generationConfig"]["thinkingConfig"]["thinkingBudget"], 0)
        self.assertNotIn("thinkingLevel", payload["generationConfig"]["thinkingConfig"])

    @patch("server.infra.llm_clients.requests.post")
    @patch("server.infra.llm_clients.get_llm_config", return_value={"model": "gemini-3-flash", "temperature": 0.2})
    @patch(
        "server.infra.llm_clients.get_settings",
        return_value=SimpleNamespace(
            google_api_key="test-key",
            llm=SimpleNamespace(gemini_timeout_seconds=30),
        ),
    )
    def test_uses_minimal_thinking_level_for_gemini_3(
        self,
        mocked_settings,
        mocked_llm_config,
        mocked_post,
    ) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        mocked_post.return_value = response

        result = call_gemini_text(
            feature_key="youtube_summary",
            prompt_text="요약해라",
            max_output_tokens=100,
        )

        self.assertEqual(result, "ok")
        payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(payload["generationConfig"]["thinkingConfig"]["thinkingLevel"], "minimal")

    @patch("server.infra.llm_clients.requests.post")
    @patch("server.infra.llm_clients.get_llm_config", return_value={"model": "gemini-3-flash", "temperature": 0.2})
    @patch(
        "server.infra.llm_clients.get_settings",
        return_value=SimpleNamespace(
            google_api_key="test-key",
            llm=SimpleNamespace(gemini_timeout_seconds=30),
        ),
    )
    def test_includes_google_search_tool_when_requested(
        self,
        mocked_settings,
        mocked_llm_config,
        mocked_post,
    ) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        mocked_post.return_value = response

        result = call_gemini_text(
            feature_key="hanall_news_brief",
            prompt_text="최신 내용을 찾아 요약해라",
            max_output_tokens=100,
            tools=[{"google_search": {}}],
        )

        self.assertEqual(result, "ok")
        payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(payload["tools"], [{"google_search": {}}])


class RoomResolutionTest(unittest.TestCase):
    def test_resolve_room_prefers_channel_id(self) -> None:
        room = resolve_room_policy("잘못된방이름", "464174551530382")
        self.assertIsNotNone(room)
        assert room is not None
        self.assertEqual(room.room_key, "admin_test_room")


class DeliveryDedupeTest(unittest.TestCase):
    def test_register_delivery_dedupe_rejects_duplicate_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            init_db(str(db_path))
            first = register_delivery_dedupe(
                dedupe_key="schedule:test",
                room_key="admin_test_room",
                source_type="schedule:test",
                trace_id="trace-1",
                ttl_seconds=600,
            )
            second = register_delivery_dedupe(
                dedupe_key="schedule:test",
                room_key="admin_test_room",
                source_type="schedule:test",
                trace_id="trace-2",
                ttl_seconds=600,
            )
            self.assertTrue(first)
            self.assertFalse(second)


class AdminAlertThrottleTest(unittest.TestCase):
    def test_register_admin_alert_attempt_throttles_same_feature(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "alerts.db"
            init_db(str(db_path))
            first = register_admin_alert_attempt(
                room_key="stock_openchat_alpha",
                feature_key="youtube_summary",
                throttle_seconds=300,
            )
            second = register_admin_alert_attempt(
                room_key="stock_openchat_alpha",
                feature_key="youtube_summary",
                throttle_seconds=300,
            )
            other_feature = register_admin_alert_attempt(
                room_key="stock_openchat_alpha",
                feature_key="hanall_news_brief",
                throttle_seconds=300,
            )
            self.assertTrue(first.should_send)
            self.assertFalse(second.should_send)
            self.assertEqual(second.suppressed_count, 1)
            self.assertTrue(other_feature.should_send)

    def test_notify_admin_error_skips_send_when_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "alerts.db"
            init_db(str(db_path))
            with patch("server.application.delivery.deliver_room_messages") as mocked_deliver:
                notify_admin_error(
                    room_key="stock_openchat_alpha",
                    feature_key="youtube_summary",
                    trace_id="trace-1",
                    detail="first error",
                )
                notify_admin_error(
                    room_key="stock_openchat_alpha",
                    feature_key="youtube_summary",
                    trace_id="trace-2",
                    detail="second error",
                )
                self.assertEqual(mocked_deliver.call_count, 1)

    def test_notify_admin_error_reports_suppressed_count_after_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "alerts.db"
            init_db(str(db_path))
            with patch("server.application.delivery.deliver_room_messages") as mocked_deliver:
                notify_admin_error(
                    room_key="stock_openchat_alpha",
                    feature_key="youtube_summary",
                    trace_id="trace-1",
                    detail="first error",
                )
                notify_admin_error(
                    room_key="stock_openchat_alpha",
                    feature_key="youtube_summary",
                    trace_id="trace-2",
                    detail="second error",
                )
                cooldown_at = (now_kst() - timedelta(seconds=301)).isoformat()
                import sqlite3

                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "UPDATE admin_alert_states SET last_sent_at = ? WHERE throttle_key = ?",
                        (cooldown_at, "stock_openchat_alpha:youtube_summary"),
                    )
                notify_admin_error(
                    room_key="stock_openchat_alpha",
                    feature_key="youtube_summary",
                    trace_id="trace-3",
                    detail="third error",
                )
                self.assertEqual(mocked_deliver.call_count, 2)
                sent_message = mocked_deliver.call_args.kwargs["message"]
                self.assertIn("suppressed_since_last_alert: 1", sent_message)


class SplitMessageTest(unittest.TestCase):
    def test_split_long_message_keeps_multiple_chunks(self) -> None:
        chunks = split_long_message("A\n" + ("B" * 920), limit=900)
        self.assertGreaterEqual(len(chunks), 2)

    def test_delivery_split_message_splits_single_long_line(self) -> None:
        from server.application.delivery import _split_message_by_limit

        chunks = _split_message_by_limit("A" * 1200, 900)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 900 for chunk in chunks))

    def test_delivery_split_message_fills_current_chunk_before_splitting(self) -> None:
        from server.application.delivery import _split_message_by_limit

        chunks = _split_message_by_limit("AA\n" + ("B" * 12), 10)
        self.assertEqual(chunks, ["AA\n" + ("B" * 7), "B" * 5])


class PollingDeliveryTest(unittest.TestCase):
    def test_deliver_room_messages_enqueues_outbox_items(self) -> None:
        from server.application.delivery import deliver_room_messages

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "queue.db"
            init_db(str(db_path))
            reset_polling_status()

            result = deliver_room_messages(
                room_key="admin_test_room",
                message="A" * 4000,
                trace_id="trace-polling-1",
                source_type="schedule:test",
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["transport"], "polling")
            self.assertEqual(result["via"], "polling_outbox")
            self.assertTrue(result["queued"])
            self.assertFalse(result["delivered"])
            self.assertEqual(len(result["outbox_ids"]), 2)
            self.assertEqual(count_outbox_messages("pending"), 2)

            pulled = pull_pending_outbox_messages(limit=10)
            self.assertEqual(len(pulled), 2)
            self.assertTrue(all(item["trace_id"] == "trace-polling-1" for item in pulled))
            self.assertTrue(all(len(item["message"]) <= 3000 for item in pulled))

            updated = ack_outbox_messages([item["id"] for item in pulled], success=True, increment_retry=False)
            self.assertEqual(updated, 2)
            self.assertEqual(count_outbox_messages("sent"), 2)


class SchedulerEventStoreTest(unittest.TestCase):
    def test_record_scheduler_event_persists_meta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "scheduler.db"
            init_db(str(db_path))

            record_scheduler_event(
                "started",
                "scheduler started",
                trace_id="trace-scheduler-1",
                meta={"scheduled_job_count": 3},
            )

            events = list_scheduler_events(5)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "started")
            self.assertEqual(events[0]["trace_id"], "trace-scheduler-1")
            self.assertEqual(events[0]["meta"]["scheduled_job_count"], 3)

    def test_runtime_health_includes_recent_scheduler_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "scheduler.db"
            init_db(str(db_path))
            reset_polling_status()
            record_scheduler_event("started", "scheduler started", meta={"scheduled_job_count": 2})

            settings = SimpleNamespace(
                app_env="test",
                timezone="Asia/Seoul",
                api_base_path="/kakao",
                api_base_url="http://127.0.0.1:8000/kakao",
                scheduler_recent_misfire_grace_seconds=900,
                socket=SimpleNamespace(enabled=False),
            )
            use_case = RuntimeHealthUseCase(
                settings_provider=lambda: settings,
                scheduler_event_lister=list_scheduler_events,
                trace_id_factory=lambda: "trace-health",
                now_factory=now_kst,
            )

            result = use_case.build_health(None)

            self.assertEqual(result["meta"]["active_transport"], "polling_outbox")
            self.assertTrue(result["meta"]["polling_enabled"])
            self.assertEqual(result["meta"]["polling_interval_ms"], 15000)
            self.assertEqual(result["meta"]["scheduler_recent_misfire_grace_seconds"], 900)
            self.assertEqual(len(result["meta"]["scheduler_recent_events"]), 1)
            self.assertEqual(result["meta"]["scheduler_recent_events"][0]["event_type"], "started")
            self.assertEqual(result["meta"]["socket_transport"]["status"], "inactive")

    def test_room_target_warning_logs_empty_channel_id_risks(self) -> None:
        rooms = {
            "stock_openchat_alpha": SimpleNamespace(
                room_key="stock_openchat_alpha",
                display_name="한올바이오파마 주식 관련 오픈채팅방",
                channel_id="",
                schedules=SimpleNamespace(enabled=False),
                features=SimpleNamespace(news_brief=False, morning_brief=False),
                news=SimpleNamespace(enabled=False),
            ),
            "family_room_home": SimpleNamespace(
                room_key="family_room_home",
                display_name="가족 카톡방",
                channel_id="",
                schedules=SimpleNamespace(enabled=False),
                features=SimpleNamespace(news_brief=False, morning_brief=True),
                news=SimpleNamespace(enabled=False),
            ),
            "stock_openchat_news": SimpleNamespace(
                room_key="stock_openchat_news",
                display_name="주식 오픈 카톡방",
                channel_id="",
                schedules=SimpleNamespace(enabled=False),
                features=SimpleNamespace(news_brief=True, morning_brief=False),
                news=SimpleNamespace(enabled=True),
            ),
        }

        with self.assertLogs("server.config", level="WARNING") as captured:
            _warn_room_delivery_targets(rooms)

        joined = "\n".join(captured.output)
        self.assertIn("room_key=stock_openchat_alpha", joined)
        self.assertIn("display_name=한올바이오파마 주식 관련 오픈채팅방", joined)
        self.assertIn("room_key=family_room_home", joined)
        self.assertIn("room_key=stock_openchat_news", joined)
        self.assertIn("display_name_fallback_only", joined)


class YouTubeSummaryTest(unittest.TestCase):
    @patch("server.application.youtube.save_processed_video")
    @patch("server.application.youtube._call_gemini_video_summary")
    @patch("server.application.youtube._call_gemini_short_summary", return_value="자막 요약")
    @patch(
        "server.application.youtube._fetch_youtube_transcript_text",
        return_value="이 영상은 최근 반도체 업황과 실적 흐름을 정리하고 다음 분기 관전 포인트를 설명합니다.",
    )
    def test_prefers_transcript_summary_before_native_fallback(
        self,
        mocked_fetch_transcript,
        mocked_transcript_summary,
        mocked_video_summary,
        mocked_save,
    ) -> None:
        url = "https://www.youtube.com/watch?v=MXdY-SRHUBE"

        result = summarize_youtube_url("admin_test_room", url)

        self.assertEqual(result, "자막 요약")
        mocked_fetch_transcript.assert_called_once_with("MXdY-SRHUBE")
        mocked_transcript_summary.assert_called_once_with(
            "MXdY-SRHUBE",
            "이 영상은 최근 반도체 업황과 실적 흐름을 정리하고 다음 분기 관전 포인트를 설명합니다.",
        )
        mocked_video_summary.assert_not_called()
        mocked_save.assert_called_once_with("admin_test_room", "MXdY-SRHUBE", "https://www.youtube.com/watch?v=MXdY-SRHUBE")

    @patch("server.application.youtube.save_processed_video")
    @patch("server.application.youtube._fetch_youtube_transcript_text", side_effect=TranscriptsDisabled("MXdY-SRHUBE"))
    @patch("server.application.youtube._call_gemini_video_summary", return_value="요약 결과")
    def test_repeated_video_is_still_summarized(
        self,
        mocked_video_summary,
        mocked_fetch_transcript,
        mocked_save,
    ) -> None:
        url = "https://www.youtube.com/watch?v=MXdY-SRHUBE"
        first = summarize_youtube_url("admin_test_room", url)
        second = summarize_youtube_url("admin_test_room", url)

        self.assertEqual(first, "요약 결과")
        self.assertEqual(second, "요약 결과")
        self.assertEqual(mocked_fetch_transcript.call_count, 2)
        self.assertEqual(mocked_video_summary.call_count, 2)
        self.assertEqual(mocked_save.call_count, 2)

    @patch("server.application.youtube.save_processed_video")
    @patch("server.application.youtube._fetch_youtube_transcript_text", side_effect=TranscriptsDisabled("MXdY-SRHUBE"))
    @patch(
        "server.application.youtube._call_gemini_video_summary",
        side_effect=[requests.RequestException("dns down"), "요약 결과"],
    )
    def test_retries_once_after_transient_failure(
        self,
        mocked_video_summary,
        mocked_fetch_transcript,
        mocked_save,
    ) -> None:
        url = "https://www.youtube.com/watch?v=MXdY-SRHUBE"

        result = summarize_youtube_url("admin_test_room", url)

        self.assertEqual(result, "요약 결과")
        self.assertEqual(mocked_fetch_transcript.call_count, 2)
        self.assertEqual(mocked_video_summary.call_count, 2)
        mocked_save.assert_called_once()

    @patch("server.application.youtube.save_processed_video")
    @patch("server.application.youtube._fetch_youtube_transcript_text", side_effect=TranscriptsDisabled("EthTT3ys7ew"))
    @patch("server.application.youtube._call_gemini_video_summary", return_value="네이티브 요약")
    def test_uses_native_summary_for_shorts_without_transcript(
        self,
        mocked_video_summary,
        mocked_fetch_transcript,
        mocked_save,
    ) -> None:
        url = "https://youtube.com/shorts/EthTT3ys7ew?si=jfYXt_qkubAhr9gR"

        result = summarize_youtube_url("admin_test_room", url)

        self.assertEqual(result, "네이티브 요약")
        mocked_fetch_transcript.assert_called_once_with("EthTT3ys7ew")
        mocked_video_summary.assert_called_once_with("https://www.youtube.com/shorts/EthTT3ys7ew")
        mocked_save.assert_called_once_with("admin_test_room", "EthTT3ys7ew", "https://www.youtube.com/watch?v=EthTT3ys7ew")

    @patch("server.application.youtube.save_processed_video")
    @patch("server.application.youtube._call_gemini_short_summary")
    @patch("server.application.youtube._call_gemini_video_summary", return_value="네이티브 요약")
    @patch("server.application.youtube._fetch_youtube_transcript_text", return_value="Heat. Hey, Heat. [Music]")
    def test_uses_native_summary_when_transcript_is_low_signal(
        self,
        mocked_fetch_transcript,
        mocked_video_summary,
        mocked_transcript_summary,
        mocked_save,
    ) -> None:
        url = "https://youtube.com/shorts/EthTT3ys7ew?si=jfYXt_qkubAhr9gR"

        result = summarize_youtube_url("admin_test_room", url)

        self.assertEqual(result, "네이티브 요약")
        mocked_fetch_transcript.assert_called_once_with("EthTT3ys7ew")
        mocked_transcript_summary.assert_not_called()
        mocked_video_summary.assert_called_once_with("https://www.youtube.com/shorts/EthTT3ys7ew")
        mocked_save.assert_called_once_with("admin_test_room", "EthTT3ys7ew", "https://www.youtube.com/watch?v=EthTT3ys7ew")

    @patch("server.application.youtube._fetch_youtube_transcript_text", side_effect=TranscriptsDisabled("MXdY-SRHUBE"))
    @patch(
        "server.application.youtube._call_gemini_video_summary",
        side_effect=requests.RequestException("dns down"),
    )
    def test_collects_failure_reason_and_attempts_when_retry_exhausted(
        self,
        mocked_video_summary,
        mocked_fetch_transcript,
    ) -> None:
        url = "https://www.youtube.com/watch?v=MXdY-SRHUBE"

        result = collect_youtube_summary_messages("admin_test_room", [url])

        self.assertEqual(result.messages, [])
        self.assertEqual(result.failed_urls, [url])
        self.assertEqual(len(result.failure_details), 1)
        self.assertEqual(result.failure_details[0]["attempts"], 2)
        self.assertIn("dns down", result.failure_details[0]["reason"])
        self.assertEqual(mocked_fetch_transcript.call_count, 2)
        self.assertEqual(mocked_video_summary.call_count, 2)


class FamilyMorningBriefTest(unittest.TestCase):
    @patch(
        "server.application.family.get_room_weather_snapshot",
        return_value={
            "weather_summary": "맑음",
            "min_temp": 10,
            "max_temp": 20,
            "rain_chance": 0,
            "air_quality": "정보 없음",
        },
    )
    def test_missing_room_shows_default_grid_label(self, mocked_weather) -> None:
        result = build_family_morning_brief(None)
        self.assertIn("오늘 날씨[기상청 격자 60,127]", result)
        self.assertIn("👶", result)
        mocked_weather.assert_called_once_with(None)

    @patch(
        "server.application.family.get_room_weather_snapshot",
        return_value={
            "weather_summary": "맑음",
            "min_temp": 10,
            "max_temp": 20,
            "rain_chance": 0,
            "air_quality": "좋음",
        },
    )
    def test_room_without_child_config_omits_child_line(self, mocked_weather) -> None:
        from server.config import reload_settings

        reload_settings()
        result = build_family_morning_brief("openchat_test")

        self.assertNotIn("👶", result)
        self.assertIn("오늘 날씨[김해율하]", result)
        self.assertIn("📝 한 줄 메모:", result)
        mocked_weather.assert_called_once_with("openchat_test")

    @patch("server.application.family.get_room_policy")
    @patch("server.application.family.get_room_weather_snapshot")
    def test_weather_disabled_omits_weather_lines(self, mocked_weather, mocked_room_policy) -> None:
        mocked_room_policy.return_value = SimpleNamespace(
            child=SimpleNamespace(enabled=True, birth_date="2024-09-26", include_days=True),
            weather=SimpleNamespace(enabled=False, label="김해율하", grid_x=93, grid_y=75),
            raw={"child": {"enabled": True, "birth_date": "2024-09-26", "include_days": True}},
        )

        result = build_family_morning_brief("room_without_weather")

        self.assertIn("👶", result)
        self.assertNotIn("🌤", result)
        self.assertNotIn("🌡", result)
        self.assertNotIn("☔", result)
        self.assertIn("📝 한 줄 메모:", result)
        mocked_weather.assert_not_called()


class WeatherSnapshotTest(unittest.TestCase):
    @patch("server.application.weather.fetch_kma_forecast_items")
    def test_uses_0200_snapshot_for_daily_max_temperature(self, mocked_fetch) -> None:
        current_items = [
            {"category": "SKY", "fcstDate": "20260325", "fcstTime": "0800", "fcstValue": "1"},
            {"category": "PTY", "fcstDate": "20260325", "fcstTime": "0800", "fcstValue": "0"},
            {"category": "POP", "fcstDate": "20260325", "fcstTime": "0800", "fcstValue": "10"},
            {"category": "TMX", "fcstDate": "20260326", "fcstTime": "1500", "fcstValue": "18"},
        ]
        daily_items = [
            {"category": "TMN", "fcstDate": "20260325", "fcstTime": "0600", "fcstValue": "9"},
            {"category": "TMX", "fcstDate": "20260325", "fcstTime": "1500", "fcstValue": "17"},
        ]
        mocked_fetch.side_effect = [current_items, daily_items]

        with patch("server.application.weather.get_kma_base_datetime", return_value=("20260325", "0800")):
            with patch("server.application.weather.now_kst") as mocked_now:
                from datetime import datetime
                from zoneinfo import ZoneInfo

                mocked_now.return_value = datetime(2026, 3, 25, 8, 10, tzinfo=ZoneInfo("Asia/Seoul"))
                result = get_room_weather_snapshot("admin_test_room")

        self.assertEqual(result["min_temp"], 9)
        self.assertEqual(result["max_temp"], 17)

    @patch("server.application.weather.fetch_kma_forecast_items")
    def test_partial_failure_still_uses_available_daily_snapshot(self, mocked_fetch) -> None:
        daily_items = [
            {"category": "TMN", "fcstDate": "20260325", "fcstTime": "0600", "fcstValue": "9"},
            {"category": "TMX", "fcstDate": "20260325", "fcstTime": "1500", "fcstValue": "17"},
        ]
        mocked_fetch.side_effect = [TimeoutError("slow current response"), daily_items]

        with patch("server.application.weather.get_kma_base_datetime", return_value=("20260325", "0800")):
            with patch("server.application.weather.now_kst") as mocked_now:
                from datetime import datetime
                from zoneinfo import ZoneInfo

                mocked_now.return_value = datetime(2026, 3, 25, 8, 10, tzinfo=ZoneInfo("Asia/Seoul"))
                result = get_room_weather_snapshot("admin_test_room")

        self.assertEqual(result["weather_summary"], "정보 없음")
        self.assertEqual(result["min_temp"], 9)
        self.assertEqual(result["max_temp"], 17)
        self.assertEqual(result["rain_chance"], "정보 없음")


if __name__ == "__main__":
    unittest.main()
