from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import requests
from youtube_transcript_api import TranscriptsDisabled

from server.application.delivery import notify_admin_error
from server.application.family import build_family_morning_brief
from server.application.hanall_research import (
    HanallResearchItem,
    HanallSourceStatus,
    build_hanall_prompt_replacements,
    build_hanall_research_packet,
)
from server.application.news import build_hanall_news_brief
from server.application.prompting import run_prompt_by_key_raw
from server.application.weather import get_room_weather_snapshot
from server.application.youtube import collect_youtube_summary_messages, split_long_message, summarize_youtube_url
from server.config import resolve_room_policy
from server.infra.llm_clients import call_gemini_text, call_openai_text
from server.infra.sqlite_store import (
    ack_outbox_messages,
    count_outbox_messages,
    init_db,
    pull_pending_outbox_messages,
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

    @patch("server.application.hanall_research.build_hanall_research_packet", return_value="stage1 packet")
    def test_prompt_replacements_include_stage1_packet(self, mocked_packet) -> None:
        now = datetime(2026, 3, 27, 9, 0, tzinfo=now_kst().tzinfo)

        replacements = build_hanall_prompt_replacements(now)

        self.assertEqual(replacements["__NOW_KST__"], "2026-03-27 09:00 KST")
        self.assertEqual(replacements["__TODAY_KST__"], "2026-03-27")
        self.assertEqual(replacements["__HANALL_RESEARCH_PACKET__"], "stage1 packet")
        mocked_packet.assert_called_once_with(now)


class BuildHanallNewsBriefTest(unittest.TestCase):
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

회사 업데이트
- [HIGH] Immunovant | press_release | 2026-03-26 08:30 KST
사실: 신규 기업 업데이트가 게시됨
수치/날짜: 2026-03-26 08:30 KST 게시
출처: Immunovant PR

경쟁사 업데이트
- [MEDIUM] argenx | efgartigimod | gMG | 2026-03-26 07:00 KST
사실: 경쟁사 관련 공식 이벤트가 확인됨
연결 자산: IMVT-1401
경쟁 구분: Direct class
수치/날짜: gMG 관련 일정 업데이트
왜 중요한가: 같은 FcRn 축 비교에 참고 가능
출처: argenx IR

미확인 단서
- 없음

검증 메모
- 점검 상태: 회사 IR done, 규제/거래소 done, 임상등록 done, 경쟁사 공식소스 done
- 접근 제한: 특이사항 없음
- 경쟁사 커버리지 공백: 낮음
- 확인 소스 로그: 4건
- 확인 소스 예시: Immunovant PR:new, SEC Form 8-K:no_new"""

    @patch("server.application.news.run_prompt_by_key_raw", side_effect=TimeoutError("background 응답 polling timeout status=in_progress"))
    def test_returns_fallback_message_on_timeout(self, mocked_run_prompt) -> None:
        result = build_hanall_news_brief()
        self.assertIn("한올 뉴스 브리핑 생성이 아직 끝나지 않았습니다.", result)
        mocked_run_prompt.assert_called_once_with("hanall_news_prompt")

    @patch("server.application.news.run_prompt_by_key_raw", side_effect=requests.RequestException("network down"))
    def test_returns_fallback_message_on_request_error(self, mocked_run_prompt) -> None:
        result = build_hanall_news_brief()
        self.assertIn("한올 뉴스 브리핑을 지금 가져오지 못했습니다.", result)
        mocked_run_prompt.assert_called_once_with("hanall_news_prompt")

    @patch("server.application.news.run_prompt_by_key_raw")
    def test_returns_plain_text_brief(self, mocked_run_prompt) -> None:
        mocked_run_prompt.return_value = self._build_valid_hanall_text()

        result = build_hanall_news_brief()

        self.assertIn("[한올/Immunovant 24시간 브리핑]", result)
        self.assertIn("회사 업데이트", result)
        self.assertIn("경쟁사 업데이트", result)
        self.assertIn("오늘 예정 이벤트", result)
        self.assertIn("정기주주총회", result)
        self.assertIn("Immunovant", result)
        self.assertIn("argenx", result)
        self.assertNotIn("```", result)

    @patch("server.application.news.run_prompt_by_key_raw")
    def test_strips_code_fence_wrapper(self, mocked_run_prompt) -> None:
        mocked_run_prompt.return_value = f"```text\n{self._build_valid_hanall_text()}\n```"
        result = build_hanall_news_brief()
        self.assertTrue(result.startswith("[한올/Immunovant 24시간 브리핑]"))
        self.assertNotIn("```", result)

    @patch("server.application.news.run_prompt_by_key_raw", return_value="   ")
    def test_returns_fallback_message_on_empty_text(self, mocked_run_prompt) -> None:
        result = build_hanall_news_brief()
        self.assertIn("한올 뉴스 브리핑 텍스트 정리에 실패했습니다.", result)
        mocked_run_prompt.assert_called_once_with("hanall_news_prompt")

    @patch("server.application.news.deliver_room_messages")
    @patch("server.application.news.run_prompt_by_key_raw")
    def test_enqueues_raw_payload_to_admin_before_render(self, mocked_run_prompt, mocked_deliver) -> None:
        mocked_run_prompt.return_value = self._build_valid_hanall_text()

        result = build_hanall_news_brief(
            room_key="stock_openchat_news",
            send_raw_to_admin=True,
        )

        self.assertIn("[한올/Immunovant 24시간 브리핑]", result)
        mocked_run_prompt.assert_called_once_with("hanall_news_prompt")
        mocked_deliver.assert_called_once()
        self.assertEqual(mocked_deliver.call_args.kwargs["room_key"], "admin_test_room")
        self.assertEqual(mocked_deliver.call_args.kwargs["source_type"], "admin:hanall_news_raw")
        raw_message = mocked_deliver.call_args.kwargs["message"]
        self.assertIn("[hanall_news raw]", raw_message)
        self.assertIn("payload_kind: text", raw_message)
        self.assertIn("Immunovant", raw_message)


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
        return_value={
            "title": "한올바이오파마 뉴스 브리핑",
            "feature_key": "hanall_news_brief",
            "preserve_newlines": True,
            "tools": [{"google_search": {}}],
        },
    )
    def test_run_prompt_routes_hanall_prompt_to_gemini(
        self,
        mocked_prompt,
        mocked_settings,
        mocked_llm_config,
        mocked_render,
        mocked_replacements,
        mocked_gemini,
        mocked_openai,
    ) -> None:
        result = run_prompt_by_key_raw("hanall_news_prompt")

        self.assertEqual(result, "gemini result")
        mocked_gemini.assert_called_once()
        mocked_openai.assert_not_called()
        self.assertEqual(mocked_gemini.call_args.kwargs["tools"], [{"google_search": {}}])


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

            result = deliver_room_messages(
                room_key="admin_test_room",
                message="A" * 4000,
                trace_id="trace-polling-1",
                source_type="schedule:test",
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["via"], "polling_outbox")
            self.assertEqual(len(result["outbox_ids"]), 2)
            self.assertEqual(count_outbox_messages("pending"), 2)

            pulled = pull_pending_outbox_messages(limit=10)
            self.assertEqual(len(pulled), 2)
            self.assertTrue(all(item["trace_id"] == "trace-polling-1" for item in pulled))
            self.assertTrue(all(len(item["message"]) <= 3000 for item in pulled))

            updated = ack_outbox_messages([item["id"] for item in pulled], success=True, increment_retry=False)
            self.assertEqual(updated, 2)
            self.assertEqual(count_outbox_messages("sent"), 2)


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
