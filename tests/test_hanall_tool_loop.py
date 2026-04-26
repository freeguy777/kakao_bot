from __future__ import annotations

from datetime import date

import httpx
from openai import APITimeoutError

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
        self.received_tools = []

    async def create(self, **kwargs: object):
        self.received_messages.append(kwargs["messages"])
        self.received_tools.append(kwargs.get("tools"))
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


class RetryableCompletionError(Exception):
    def __init__(self, status_code: int, error_type: str) -> None:
        super().__init__(f"status={status_code} type={error_type}")
        self.status_code = status_code
        self.body = {"error": {"type": error_type}}


class RetryingCompletions:
    def __init__(self, failures_before_success: int, final_text: str) -> None:
        self._failures_before_success = failures_before_success
        self._final_text = final_text
        self.calls = 0
        self.received_messages = []
        self.received_tools = []

    async def create(self, **kwargs: object):
        self.calls += 1
        self.received_messages.append(kwargs["messages"])
        self.received_tools.append(kwargs.get("tools"))
        if self.calls <= self._failures_before_success:
            raise RetryableCompletionError(429, "engine_overloaded_error")
        return FakeResponse("stop", FakeMessage(self._final_text))


class RetryingClient:
    def __init__(self, failures_before_success: int, final_text: str) -> None:
        self.chat = type("Chat", (), {"completions": RetryingCompletions(failures_before_success, final_text)})()


class TimeoutRetryingCompletions:
    def __init__(self, failures_before_success: int, final_text: str) -> None:
        self._failures_before_success = failures_before_success
        self._final_text = final_text
        self.calls = 0
        self.received_messages = []
        self.received_tools = []

    async def create(self, **kwargs: object):
        self.calls += 1
        self.received_messages.append(kwargs["messages"])
        self.received_tools.append(kwargs.get("tools"))
        if self.calls <= self._failures_before_success:
            raise APITimeoutError(request=httpx.Request("POST", "https://api.moonshot.ai/v1/chat/completions"))
        return FakeResponse("stop", FakeMessage(self._final_text))


class TimeoutRetryingClient:
    def __init__(self, failures_before_success: int, final_text: str) -> None:
        self.chat = type("Chat", (), {"completions": TimeoutRetryingCompletions(failures_before_success, final_text)})()


class ExhaustedLoopCompletions:
    def __init__(self, final_text: str) -> None:
        self._final_text = final_text
        self.calls = 0
        self.received_messages = []
        self.received_tools = []

    async def create(self, **kwargs: object):
        self.calls += 1
        self.received_messages.append(kwargs["messages"])
        self.received_tools.append(kwargs.get("tools"))
        if kwargs.get("tools") is None:
            return FakeResponse("stop", FakeMessage(self._final_text))
        return FakeResponse(
            "tool_calls",
            FakeMessage(None, [FakeToolCall(f"call_{self.calls}", "web_search", '{"query":"hanall biopharma"}')]),
        )


class ExhaustedLoopClient:
    def __init__(self, final_text: str) -> None:
        self.chat = type("Chat", (), {"completions": ExhaustedLoopCompletions(final_text)})()


class WebSearchLimitedCompletions:
    def __init__(self, final_text: str) -> None:
        self._final_text = final_text
        self.calls = 0
        self.received_messages = []
        self.received_tools = []

    async def create(self, **kwargs: object):
        self.calls += 1
        self.received_messages.append(kwargs["messages"])
        self.received_tools.append(kwargs.get("tools"))
        if kwargs.get("tools") is None:
            return FakeResponse("stop", FakeMessage(self._final_text))
        return FakeResponse(
            "tool_calls",
            FakeMessage(None, [FakeToolCall(f"call_{self.calls}", "web_search", '{"query":"hanall biopharma"}')]),
        )


class WebSearchLimitedClient:
    def __init__(self, final_text: str) -> None:
        self.chat = type("Chat", (), {"completions": WebSearchLimitedCompletions(final_text)})()


class FakeFiberResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class RecordingHttpClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.calls: list[dict[str, object]] = []
        self._payload = payload

    async def post(self, url: str, headers: dict[str, str], json: dict[str, object]):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeFiberResponse(self._payload)


class FlakyHttpClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.calls: list[dict[str, object]] = []
        self._payload = payload

    async def post(self, url: str, headers: dict[str, str], json: dict[str, object]):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if len(self.calls) == 1:
            request = httpx.Request("POST", url)
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)
        return FakeFiberResponse(self._payload)


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


async def test_hanall_tool_loop_requests_final_render_without_tools_when_iterations_are_exhausted(test_settings) -> None:
    final_text = f"<public_brief>{PUBLIC_BRIEF}</public_brief>\n<admin_report>{ADMIN_REPORT}</admin_report>"
    service = HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=type("Repo", (), {"get_by_key": lambda *_: None, "save": lambda *args, **kwargs: args[1]})(),
    )
    test_settings.kimi_max_iterations = 2
    service._client = ExhaustedLoopClient(final_text)
    service._load_formula_tools = lambda: [{"type": "function", "function": {"name": "web_search"}}]

    async def fake_resolve(tool_calls):
        return [{"role": "tool", "tool_call_id": tool_calls[0].id, "content": "search result"}]

    service._resolve_tool_calls = fake_resolve

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 6))

    assert artifact.summary_text == PUBLIC_BRIEF
    assert artifact.detail_text == ADMIN_REPORT.strip()
    assert service._client.chat.completions.received_tools == [
        [{"type": "function", "function": {"name": "web_search"}}],
        [{"type": "function", "function": {"name": "web_search"}}],
        None,
    ]
    final_request_messages = service._client.chat.completions.received_messages[-1]
    assert final_request_messages[-1]["role"] == "user"
    assert "추가 도구 호출을 중단" in final_request_messages[-1]["content"]
    assert "각 섹션 제목 줄 끝에 건수/개수를 붙여라" in final_request_messages[-1]["content"]
    assert "건수/개수는 제목 줄에만 쓰고 bullet에서는" in final_request_messages[-1]["content"]
    assert "bullet 1개는 사실 1건 또는 포인트 1개만 담아라" in final_request_messages[-1]["content"]
    assert "filing 이름만 쓰지 말고 사건 의미를 먼저 적어라" in final_request_messages[-1]["content"]
    assert "핵심 수량/거래일" in final_request_messages[-1]["content"]
    assert "public_brief 1번 섹션에도 축약 반영하라" in final_request_messages[-1]["content"]


async def test_hanall_tool_loop_limits_web_search_rounds_before_final_render(test_settings) -> None:
    final_text = f"<public_brief>{PUBLIC_BRIEF}</public_brief>\n<admin_report>{ADMIN_REPORT}</admin_report>"
    service = HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=type("Repo", (), {"get_by_key": lambda *_: None, "save": lambda *args, **kwargs: args[1]})(),
    )
    service._client = WebSearchLimitedClient(final_text)
    service._load_formula_tools = lambda: [{"type": "function", "function": {"name": "web_search"}}]

    resolve_calls = 0

    async def fake_resolve(tool_calls):
        nonlocal resolve_calls
        resolve_calls += 1
        return [{"role": "tool", "tool_call_id": tool_calls[0].id, "content": "search result"}]

    service._resolve_tool_calls = fake_resolve

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 6))

    assert artifact.summary_text == PUBLIC_BRIEF
    assert resolve_calls == 2
    assert service._client.chat.completions.received_tools == [
        [{"type": "function", "function": {"name": "web_search"}}],
        [{"type": "function", "function": {"name": "web_search"}}],
        None,
    ]
    final_request_messages = service._client.chat.completions.received_messages[-1]
    assert final_request_messages[-1]["role"] == "user"
    previous_message = final_request_messages[-2]
    assert previous_message["role"] == "tool"


async def test_hanall_collect_retries_overloaded_chat_completion_with_backoff(test_settings) -> None:
    final_text = f"<public_brief>{PUBLIC_BRIEF}</public_brief>\n<admin_report>{ADMIN_REPORT}</admin_report>"
    service = HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=type("Repo", (), {"get_by_key": lambda *_: None, "save": lambda *args, **kwargs: args[1]})(),
    )
    test_settings.hanall_collect_retry_delays_seconds = "60,180"
    service._client = RetryingClient(2, final_text)
    service._load_formula_tools = lambda: []
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    service._sleep_before_retry = fake_sleep

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 6))

    assert artifact.summary_text == PUBLIC_BRIEF
    assert artifact.detail_text == ADMIN_REPORT.strip()
    assert service._client.chat.completions.calls == 3
    assert sleep_calls == [60.0, 180.0]


async def test_hanall_collect_retries_timed_out_chat_completion_with_backoff(test_settings) -> None:
    final_text = f"<public_brief>{PUBLIC_BRIEF}</public_brief>\n<admin_report>{ADMIN_REPORT}</admin_report>"
    service = HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=type("Repo", (), {"get_by_key": lambda *_: None, "save": lambda *args, **kwargs: args[1]})(),
    )
    test_settings.hanall_collect_retry_delays_seconds = "60,180"
    service._client = TimeoutRetryingClient(2, final_text)
    service._load_formula_tools = lambda: []
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    service._sleep_before_retry = fake_sleep

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 6))

    assert artifact.summary_text == PUBLIC_BRIEF
    assert artifact.detail_text == ADMIN_REPORT.strip()
    assert service._client.chat.completions.calls == 3
    assert sleep_calls == [60.0, 180.0]


async def test_invoke_formula_wraps_tool_call_for_fiber_request(test_settings) -> None:
    service = HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=type("Repo", (), {"get_by_key": lambda *_: None, "save": lambda *args, **kwargs: args[1]})(),
    )
    service._tool_name_to_formula_uri["date"] = "moonshot/date:latest"
    client = RecordingHttpClient({"context": {"output": "2026-04-07 12:24:40"}})

    result = await service._invoke_formula(
        client,
        {"Authorization": "Bearer test"},
        FakeToolCall("call_1", "date", '{"operation":"time","zone":"Asia/Seoul"}'),
    )

    assert result == {"role": "tool", "tool_call_id": "call_1", "content": "2026-04-07 12:24:40"}
    assert client.calls == [
        {
            "url": "https://api.moonshot.ai/v1/formulas/moonshot%2Fdate%3Alatest/fibers",
            "headers": {"Authorization": "Bearer test"},
            "json": {
                "name": "date",
                "arguments": '{"operation":"time","zone":"Asia/Seoul"}',
            },
        }
    ]


async def test_invoke_formula_retries_retryable_http_status(test_settings) -> None:
    service = HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=type("Repo", (), {"get_by_key": lambda *_: None, "save": lambda *args, **kwargs: args[1]})(),
    )
    service._tool_name_to_formula_uri["date"] = "moonshot/date:latest"
    client = FlakyHttpClient({"context": {"output": "2026-04-07 12:24:40"}})

    result = await service._invoke_formula(
        client,
        {"Authorization": "Bearer test"},
        FakeToolCall("call_1", "date", '{"operation":"time","zone":"Asia/Seoul"}'),
    )

    assert result == {"role": "tool", "tool_call_id": "call_1", "content": "2026-04-07 12:24:40"}
    assert len(client.calls) == 2
