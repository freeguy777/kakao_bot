from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.errors import ExternalAPIError
from app.services.hanall_research_service import HanallResearchService

PUBLIC_BRIEF = """📅 2026-04-06 08:00 KST 기준

1. 🏢 한올/Immunovant 직접 업데이트
- 지난 24시간 내 신규 공시, 보도자료, IR/SEC 업데이트는 없었습니다.
- 배경: HanAll 공식 공시 페이지와 Immunovant Investors 기준 직전 공식 업데이트 이후 추가 변동은 확인되지 않았습니다.

2. 🧬 경쟁사/파이프라인 체크
- FcRn 경쟁축 기준 지난 24시간 내 중요 경쟁사 업데이트는 없었습니다.
- argenx, UCB 등 핵심 경쟁사 공식 채널에서 신규 승인, 임상, 보도자료 변경은 확인되지 않았습니다.

3. 🔎 이번에 확인한 범위
- HanAll 공식 웹사이트, DART/KIND, Immunovant Investors, 경쟁사 공식 채널을 점검했습니다.
- 공식 소스 기준으로만 확인 가능한 사실을 반영했습니다.

4. 👀 참고할 포인트
- 시간창 이전 자료는 이번 브리핑에 포함하지 않았습니다.
- 현재는 기존 공식 일정과 경쟁축 후속 발표 여부를 계속 추적하는 구간입니다."""
ADMIN_REPORT = """A. 요약
- 지난 24시간 내 Confirmed 업데이트 총수: 1

B. Confirmed Updates — Company Direct
| KST 시각 | 엔터티 | 분류 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 보조 출처 | 코멘트(추측 금지) |
| 2026-04-06 08:00 KST | HanAll Biopharma | 공시/재무 | 신규 공시 없음 확인 | 2026-04-06 | 공식 공시 페이지 | 없음 | 공식 페이지 확인 결과 |

C. Confirmed Updates — Competitor Relevant
| KST 시각 | 경쟁사 | 자산 | 분류(Direct class / Indication / Standard-of-care / Regional) | 관련 한올/IMVT 자산 | 적응증 | 정확한 사실 | 핵심 숫자/날짜 | 1차 출처 | 왜 중요한지(팩트 기반 1문장) |
| 해당 없음 | 없음 | 없음 | 없음 | batoclimab | MG | 지난 24시간 내 중요 경쟁사 업데이트 없음 | 2026-04-06 | 공식 소스 점검 | direct class 중요 이벤트 부재 확인 |

D. Competitor Map Snapshot
| 경쟁사 | 자산 | target/MoA | 적응증 | 단계/승인상태 | 지역 | universe 포함 근거 출처 |
| Immunovant | IMVT-1401 | FcRn | MG | 임상 | US | company pipeline |

E. Checked Source Log
| 소스 | 상태(새 항목 있음 / 확인했으나 신규 없음 / 접근 제한) | 마지막 확인 시각(KST) | 비고 |
| HanAll 공식 웹사이트 | 확인했으나 신규 없음 | 2026-04-06 08:00 KST | 정상 접근 |

F. Unverified Leads
- direct company: 없음
- competitor: 없음

G. Coverage Gaps
- 없음

H. Omission Audit
- 공식 회사/IR 확인 완료 여부: 완료
- 규제/거래소/공시 확인 완료 여부: 완료
"""


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = []
        self.reasoning_content = None

    def model_dump(self, mode: str = "json", exclude_none: bool = True):
        return {"role": "assistant", "content": self.content}


class FakeChoice:
    def __init__(self, message: FakeMessage) -> None:
        self.finish_reason = "stop"
        self.message = message


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [FakeChoice(FakeMessage(content))]

    def model_dump(self, mode: str = "json"):
        return {"choices": [{"finish_reason": "stop"}]}


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self._content = content
        self.received_messages: list[list[dict[str, str]]] = []

    async def create(self, **kwargs: object):
        self.received_messages.append(kwargs["messages"])
        return FakeResponse(self._content)


class FakeClient:
    def __init__(self, content: str) -> None:
        self.chat = type("Chat", (), {"completions": FakeCompletions(content)})()


def build_service(test_settings, tmp_path: Path, final_text: str, spec_text: str = "한올 business spec 원문") -> HanallResearchService:
    spec_path = tmp_path / "hanall_spec.md"
    spec_path.write_text(spec_text, encoding="utf-8")
    test_settings.hanall_spec_path = spec_path
    service = HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=type("Repo", (), {"get_by_key": lambda *_: None, "save": lambda *args, **kwargs: args[1]})(),
    )
    service._client = FakeClient(final_text)
    service._load_formula_tools = lambda: []
    return service


async def test_hanall_collect_prompt_includes_verbatim_spec_and_writes_parse_metadata(test_settings, tmp_path: Path) -> None:
    spec_text = "이 문장은 spec 원문으로 그대로 주입되어야 한다."
    final_text = f"<public_brief>{PUBLIC_BRIEF}</public_brief>\n<admin_report>{ADMIN_REPORT}</admin_report>"
    service = build_service(test_settings, tmp_path, final_text, spec_text=spec_text)

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 6))

    first_prompt = service._client.chat.completions.received_messages[0][1]["content"]
    assert spec_text in first_prompt
    assert "<public_brief>" in first_prompt
    assert "1. 🏢 한올/Immunovant 직접 업데이트" in first_prompt
    assert "2. 🧬 경쟁사/파이프라인 체크" in first_prompt
    assert "3. 🔎 이번에 확인한 범위" in first_prompt
    assert "4. 👀 참고할 포인트" in first_prompt
    assert artifact.summary_text == PUBLIC_BRIEF
    assert artifact.detail_text == ADMIN_REPORT.strip()
    assert artifact.raw_response["parse"]["parse_ok"] is True
    assert artifact.raw_response["parse"]["missing_sections"] == []


async def test_hanall_collect_fails_when_output_tags_are_missing(test_settings, tmp_path: Path) -> None:
    service = build_service(test_settings, tmp_path, ADMIN_REPORT)

    with pytest.raises(ExternalAPIError, match="must contain only <public_brief> and <admin_report> blocks"):
        await service.get_or_create_daily_artifact(date(2026, 4, 6))


async def test_hanall_collect_fails_when_public_brief_is_empty(test_settings, tmp_path: Path) -> None:
    final_text = f"<public_brief>   </public_brief>\n<admin_report>{ADMIN_REPORT}</admin_report>"
    service = build_service(test_settings, tmp_path, final_text)

    with pytest.raises(ExternalAPIError, match="empty <public_brief>"):
        await service.get_or_create_daily_artifact(date(2026, 4, 6))


async def test_hanall_collect_fails_when_required_admin_section_is_missing(test_settings, tmp_path: Path) -> None:
    broken_admin_report = ADMIN_REPORT.replace("G. Coverage Gaps\n- 없음\n\n", "")
    final_text = f"<public_brief>{PUBLIC_BRIEF}</public_brief>\n<admin_report>{broken_admin_report}</admin_report>"
    service = build_service(test_settings, tmp_path, final_text)

    with pytest.raises(ExternalAPIError, match="G. Coverage Gaps"):
        await service.get_or_create_daily_artifact(date(2026, 4, 6))


async def test_hanall_collect_accepts_normalized_section_heading_variants(test_settings, tmp_path: Path) -> None:
    normalized_variant_report = ADMIN_REPORT
    normalized_variant_report = normalized_variant_report.replace("A. 요약", "## A. 요약")
    normalized_variant_report = normalized_variant_report.replace(
        "B. Confirmed Updates — Company Direct",
        "B.   Confirmed Updates - Company Direct",
    )
    normalized_variant_report = normalized_variant_report.replace(
        "C. Confirmed Updates — Competitor Relevant",
        "**C. Confirmed Updates-Competitor Relevant**",
    )
    normalized_variant_report = normalized_variant_report.replace("D. Competitor Map Snapshot", "D.   Competitor Map Snapshot")
    final_text = f"<public_brief>{PUBLIC_BRIEF}</public_brief>\n<admin_report>{normalized_variant_report}</admin_report>"
    service = build_service(test_settings, tmp_path, final_text)

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 6))

    assert artifact.summary_text == PUBLIC_BRIEF
    assert artifact.detail_text == normalized_variant_report.strip()
    assert artifact.raw_response["parse"]["missing_sections"] == []
