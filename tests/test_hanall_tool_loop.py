from __future__ import annotations

from datetime import date

from app.services.hanall_research_service import HanallResearchService

PUBLIC_BRIEF = "한올/IMVT 직접 업데이트 1건\n경쟁사 중요 업데이트 0건"
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


class FakeToolFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = FakeToolFunction(name, arguments)


class FakeMessage:
    def __init__(self, content: str | None, tool_calls=None, reasoning_content: str | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = reasoning_content

    def model_dump(self, mode: str = "json", exclude_none: bool = True):
        payload = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tool.id,
                    "function": {"name": tool.function.name, "arguments": tool.function.arguments},
                    "type": "function",
                }
                for tool in self.tool_calls
            ]
        return payload


class FakeChoice:
    def __init__(self, finish_reason: str, message: FakeMessage) -> None:
        self.finish_reason = finish_reason
        self.message = message


class FakeResponse:
    def __init__(self, finish_reason: str, message: FakeMessage) -> None:
        self.choices = [FakeChoice(finish_reason, message)]

    def model_dump(self, mode: str = "json"):
        return {"choices": [{"finish_reason": self.choices[0].finish_reason}]}


class FakeCompletions:
    def __init__(self):
        self.calls = 0
        self.received_messages = []

    async def create(self, **kwargs: object):
        self.received_messages.append(kwargs["messages"])
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(
                "tool_calls",
                FakeMessage(
                    None,
                    [FakeToolCall("call_1", "web_search", '{"query":"hanall biopharma"}')],
                    reasoning_content="step-by-step reasoning",
                ),
            )
        final_text = f"<public_brief>{PUBLIC_BRIEF}</public_brief>\n<admin_report>{ADMIN_REPORT}</admin_report>"
        return FakeResponse("stop", FakeMessage(final_text))


class FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": FakeCompletions()})()


async def test_hanall_tool_loop_resolves_all_tool_calls(test_settings) -> None:
    service = HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=type("Repo", (), {"get_by_key": lambda *_: None, "save": lambda *args, **kwargs: args[1]})(),
    )
    service._client = FakeClient()
    service._load_formula_tools = lambda: [{"type": "function", "function": {"name": "web_search"}}]

    async def fake_resolve(tool_calls):
        assert tool_calls[0].id == "call_1"
        return [{"role": "tool", "tool_call_id": "call_1", "content": "search result"}]

    service._resolve_tool_calls = fake_resolve
    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 6))
    assert artifact.summary_text == PUBLIC_BRIEF
    assert artifact.detail_text == ADMIN_REPORT.strip()
    assert artifact.raw_response["parse"]["parse_ok"] is True
    assert artifact.raw_response["parse"]["missing_sections"] == []
    second_round_messages = service._client.chat.completions.received_messages[1]
    assistant_context = next(message for message in second_round_messages if message.get("role") == "assistant")
    assert assistant_context["reasoning_content"] == "step-by-step reasoning"
