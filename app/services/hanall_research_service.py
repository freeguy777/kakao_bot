from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from openai import APITimeoutError, AsyncOpenAI

from app.config import Settings
from app.errors import ConfigurationError, ExternalAPIError
from app.repositories import ArtifactRepository
from app.schemas import (
    HanallApiBundle,
    HanallArtifact,
    HanallRenderedOutput,
    HanallStructuredFact,
    HanallValidationResult,
    PromptLibrary,
)

logger = logging.getLogger(__name__)


class HanallResearchService:
    RETRYABLE_COMPLETION_STATUS_CODES = {429, 500, 502, 503, 504}
    RETRYABLE_FORMULA_STATUS_CODES = {429, 500, 502, 503, 504}
    FORMULA_MAX_RETRIES = 2
    PUBLIC_BRIEF_BULLET_PREFIX = "- "
    REQUIRED_ADMIN_SECTIONS = (
        "A. 요약",
        "B. Confirmed Updates — Company Direct",
        "C. Confirmed Updates — Competitor Relevant",
        "D. Competitor Map Snapshot",
        "E. Checked Source Log",
        "F. Unverified Leads",
        "G. Coverage Gaps",
        "H. Omission Audit",
    )
    OUTPUT_BLOCK_PATTERN = re.compile(
        r"\A\s*<public_brief>\s*(?P<public>.*?)\s*</public_brief>\s*<admin_report>\s*(?P<admin>.*?)\s*</admin_report>\s*\Z",
        re.DOTALL | re.IGNORECASE,
    )
    PUBLIC_BLOCK_PATTERN = re.compile(r"<public_brief>\s*(?P<public>.*?)\s*</public_brief>", re.DOTALL | re.IGNORECASE)
    ADMIN_BLOCK_PATTERN = re.compile(r"<admin_report>\s*(?P<admin>.*?)\s*</admin_report>", re.DOTALL | re.IGNORECASE)
    BLOCK_PARSE_ERROR_MESSAGE = "Hanall research response must contain only <public_brief> and <admin_report> blocks"
    DEEP_RESEARCH_MAX_CANDIDATES = 4
    DEEP_RESEARCH_MAX_WEB_SEARCH_ROUNDS = 2
    DEEP_RESEARCH_TRIGGER_KEYWORDS = (
        "8-k",
        "10-k",
        "10-q",
        "earnings",
        "financial results",
        "quarterly results",
        "annual results",
        "business update",
        "corporate update",
        "press release",
        "investor presentation",
        "presentation",
        "실적",
        "사업 업데이트",
        "보도자료",
        "투자판단관련주요경영사항",
    )
    DEEP_RESEARCH_DETAIL_TOKEN_PATTERN = re.compile(
        r"\bIMVT-\d+\b|\bHL\d+[A-Z]*\b|\bACR(?:20|50|70)\b|\bD2T\s*RA\b|\bgMG\b|\bCIDP\b|\bSjD\b|"
        r"\bCLE\b|\bGD\b|\bTED\b|\bMG\b|\bRA\b|\b(?:batoclimab|imeroprubart|tanfanercept)\b|"
        r"\d+(?:\.\d+)?%|\$\s?\d+(?:\.\d+)?\s?(?:M|B|million|billion)\b|"
        r"\b\d+(?:\.\d+)?\s?(?:million|billion)\b",
        re.IGNORECASE,
    )
    DEEP_RESEARCH_8K_SOURCE_TOKENS = (
        "exhibit 99.1",
        "ex-99.1",
        "press release",
        "official pr",
        "공식 pr",
        "보도자료",
    )
    DEEP_RESEARCH_D2T_RA_RESULT_TOKENS = (
        "acr20",
        "acr50",
        "acr70",
        "72.7%",
        "54.5%",
        "35.8%",
    )
    DIRECT_NO_UPDATE_PATTERNS = (
        "한올/immunovant 직접 업데이트 : 0건",
        "한올/imvt 직접 업데이트 : 0건",
        "24시간 내 신규 공시/규제 문서/임상등록 업데이트 없음",
        "24시간 내 신규 사실 미확인",
        "직전 공식 업데이트",
    )
    SECTION_HEADING_MARKUP_PATTERN = re.compile(r"^#+\s*")
    SECTION_EMPHASIS_PATTERN = re.compile(r"^\*{1,2}(?P<body>.+?)\*{1,2}$")
    DASH_VARIANTS_PATTERN = re.compile(r"[\u2010-\u2015\u2212-]")

    def __init__(
        self,
        *,
        settings: Settings,
        prompts: PromptLibrary,
        artifact_repository: ArtifactRepository,
        hanall_prefetch_service: object | None = None,
        options_sentiment_service: object | None = None,
    ) -> None:
        self._settings = settings
        self._prompts = prompts
        self._artifact_repository = artifact_repository
        self._hanall_prefetch_service = hanall_prefetch_service
        self._options_sentiment_service = options_sentiment_service
        self._hanall_spec = settings.load_hanall_spec()
        self._client: AsyncOpenAI | None = None
        self._tools_cache: list[dict[str, Any]] | None = None
        self._tool_name_to_formula_uri: dict[str, str] = {}
        self._timezone = ZoneInfo(settings.app_timezone)

    async def get_or_create_daily_artifact(self, artifact_date: date) -> HanallArtifact:
        artifact_key = self._artifact_key(artifact_date)
        cached = self._artifact_repository.get_by_key(artifact_key)
        if cached is not None:
            return cached
        artifact = await self._run_research(artifact_date)
        artifact = await self._attach_options_sentiment_snapshot(artifact)
        return self._artifact_repository.save(artifact, artifact_type="hanall")

    async def get_existing_daily_artifact(self, artifact_date: date) -> HanallArtifact | None:
        artifact_key = self._artifact_key(artifact_date)
        return self._artifact_repository.get_by_key(artifact_key)

    def render_public_message(self, artifact: HanallArtifact) -> str:
        return self._prompts.hanall_public_format.format(summary=self._format_public_brief(artifact.summary_text))

    def render_admin_message(self, artifact: HanallArtifact) -> str:
        return self._prompts.hanall_admin_format.format(detail=artifact.detail_text)

    @classmethod
    def _format_public_brief(cls, public_text: str) -> str:
        formatted_lines: list[str] = []
        raw_lines = public_text.splitlines()
        index = 0
        while index < len(raw_lines):
            raw_line = raw_lines[index]
            stripped_line = raw_line.rstrip()
            if stripped_line.startswith(cls.PUBLIC_BRIEF_BULLET_PREFIX):
                bullet_lines = [stripped_line]
                index += 1
                while index < len(raw_lines):
                    continuation_line = raw_lines[index].rstrip()
                    if continuation_line and continuation_line[:1].isspace():
                        bullet_lines.append(continuation_line)
                        index += 1
                        continue
                    break
                body = cls._normalize_public_brief_bullet_body(bullet_lines)
                formatted_lines.append(f"{cls.PUBLIC_BRIEF_BULLET_PREFIX}{body}".rstrip())
                continue
            formatted_lines.append(stripped_line)
            index += 1
        return "\n".join(formatted_lines).strip()

    @classmethod
    def _normalize_public_brief_bullet_body(cls, lines: list[str]) -> str:
        parts: list[str] = []
        for index, line in enumerate(lines):
            if index == 0:
                body = line[len(cls.PUBLIC_BRIEF_BULLET_PREFIX) :].strip()
            else:
                body = line.strip()
            if body:
                parts.append(body)
        merged = " ".join(parts)
        merged = re.sub(r"\s+", " ", merged).strip()
        return re.sub(r"(?<=\d),\s+(?=\d{3}(?:\D|$))", ",", merged)

    async def _run_research(self, artifact_date: date) -> HanallArtifact:
        if not self._settings.kimi_api_key:
            raise ConfigurationError("KIMI_API_KEY is not configured")
        client = self._get_client()
        loaded_tools = self._load_formula_tools()
        tools = await loaded_tools if inspect.isawaitable(loaded_tools) else loaded_tools
        run_time = datetime.now(self._timezone)
        window_start = run_time - timedelta(hours=24)
        prefetch_bundle = await self._collect_prefetch_bundle(window_start=window_start, window_end=run_time)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "반드시 한국어로 답하고 최신 자료를 우선 사용하라."},
            {
                "role": "user",
                "content": self._build_collect_prompt(
                    artifact_date,
                    run_time=run_time,
                    window_start=window_start,
                    prefetch_bundle=prefetch_bundle,
                ),
            },
        ]
        deadline = asyncio.get_running_loop().time() + self._settings.kimi_overall_deadline_seconds
        final_text = ""
        raw_response: dict[str, Any] = {}
        web_search_rounds = 0

        for iteration_index in range(self._settings.kimi_max_iterations):
            if asyncio.get_running_loop().time() >= deadline:
                raise ExternalAPIError("Hanall research exceeded overall deadline")
            response = await self._request_chat_completion(
                client,
                messages=messages,
                tools=tools or None,
                request_timeout_seconds=self._settings.kimi_completion_timeout_seconds,
                deadline=deadline,
            )
            raw_response = response.model_dump(mode="json")
            choice = response.choices[0]
            assistant_message = choice.message
            if choice.finish_reason != "tool_calls" or not assistant_message.tool_calls:
                messages.append(self._assistant_message_to_context(assistant_message))
                final_text = assistant_message.content or ""
                break
            has_web_search = self._tool_calls_include_function(assistant_message.tool_calls, "web_search")
            if has_web_search and web_search_rounds >= self._settings.hanall_max_web_search_rounds:
                self._append_assistant_context_without_tool_calls(messages, assistant_message)
                break
            if iteration_index == self._settings.kimi_max_iterations - 1:
                self._append_assistant_context_without_tool_calls(messages, assistant_message)
                break
            messages.append(self._assistant_message_to_context(assistant_message))
            messages.extend(await self._resolve_tool_calls(assistant_message.tool_calls))
            if has_web_search:
                web_search_rounds += 1
                if web_search_rounds >= self._settings.hanall_max_web_search_rounds:
                    break
        if not final_text:
            response = await self._request_final_render_without_tools(client, messages, deadline)
            raw_response = response.model_dump(mode="json")
            final_text = response.choices[0].message.content or ""
        if not final_text:
            raise ExternalAPIError("Hanall research produced no final text")
        deep_research_candidates = self._select_deep_research_candidates(prefetch_bundle)
        deep_research_text = ""
        deep_research_metadata: dict[str, Any] = {
            "status": "not_applicable",
            "candidates": [self._deep_research_candidate_payload(fact) for fact in deep_research_candidates],
            "result_text": "",
        }
        if deep_research_candidates:
            deep_research_metadata["status"] = "pending"
            try:
                deep_research_text, deep_raw_response = await self._run_direct_event_deep_research(
                    client=client,
                    messages=messages,
                    initial_final_text=final_text,
                    candidates=deep_research_candidates,
                    prefetch_bundle=prefetch_bundle,
                    tools=tools,
                    deadline=deadline,
                )
                deep_research_text = deep_research_text.strip()
                if not deep_research_text:
                    deep_research_text = self._build_empty_deep_research_note(deep_research_candidates)
                    deep_research_metadata["status"] = "empty"
                else:
                    deep_research_metadata["status"] = "success"
                deep_research_metadata["completion"] = deep_raw_response
                quality_issues = self._deep_research_quality_issues(deep_research_candidates, deep_research_text)
                if quality_issues:
                    deep_research_metadata["quality_issues"] = quality_issues
                    deep_research_metadata["quality_repair_attempted"] = True
                    deep_research_text, deep_raw_response = await self._run_direct_event_deep_research(
                        client=client,
                        messages=messages,
                        initial_final_text=final_text,
                        candidates=deep_research_candidates,
                        prefetch_bundle=prefetch_bundle,
                        tools=tools,
                        deadline=deadline,
                        previous_deep_research_text=deep_research_text,
                        quality_issues=quality_issues,
                    )
                    deep_research_text = deep_research_text.strip()
                    if not deep_research_text:
                        deep_research_text = self._build_empty_deep_research_note(deep_research_candidates)
                        deep_research_metadata["status"] = "empty_after_quality_repair"
                    else:
                        repaired_quality_issues = self._deep_research_quality_issues(
                            deep_research_candidates,
                            deep_research_text,
                        )
                        deep_research_metadata["quality_repair_issues"] = repaired_quality_issues
                        deep_research_metadata["status"] = "success" if not repaired_quality_issues else "partial"
                    deep_research_metadata["quality_repair_completion"] = deep_raw_response
            except Exception as exc:  # noqa: BLE001
                deep_research_text = self._build_failed_deep_research_note(deep_research_candidates, exc)
                deep_research_metadata["status"] = "failed"
                deep_research_metadata["failure_reason"] = str(exc)

            deep_research_metadata["result_text"] = deep_research_text
            response = await self._request_final_render_with_deep_research(
                client=client,
                messages=messages,
                initial_final_text=final_text,
                prefetch_bundle=prefetch_bundle,
                candidates=deep_research_candidates,
                deep_research_text=deep_research_text,
                deadline=deadline,
            )
            raw_response = response.model_dump(mode="json")
            final_text = response.choices[0].message.content or ""
            if not final_text:
                raise ExternalAPIError("Hanall research deep final render produced no final text")
        rendered_output, repaired_raw_response, repaired_text = await self._parse_or_repair_rendered_output(
            client=client,
            messages=messages,
            final_text=final_text,
            prefetch_bundle=prefetch_bundle,
            deep_research_text=deep_research_text,
            deep_research_candidates=deep_research_candidates,
            deadline=deadline,
        )
        if repaired_raw_response is not None:
            raw_response = repaired_raw_response
        if repaired_text is not None:
            final_text = repaired_text
        validation_result = self._validate_rendered_output(
            rendered_output,
            prefetch_bundle,
            deep_research_text=deep_research_text,
        )
        if not validation_result.is_valid:
            response = await self._request_validation_repair_without_tools(
                client,
                messages=messages,
                invalid_text=final_text,
                prefetch_bundle=prefetch_bundle,
                deep_research_text=deep_research_text,
                deep_research_candidates=deep_research_candidates,
                issues=validation_result.issues,
                deadline=deadline,
            )
            raw_response = response.model_dump(mode="json")
            corrected_text = response.choices[0].message.content or ""
            if not corrected_text:
                raise ExternalAPIError("Hanall research repair produced no final text")
            rendered_output, repaired_raw_response, _repaired_text = await self._parse_or_repair_rendered_output(
                client=client,
                messages=messages,
                final_text=corrected_text,
                prefetch_bundle=prefetch_bundle,
                deep_research_text=deep_research_text,
                deep_research_candidates=deep_research_candidates,
                deadline=deadline,
            )
            if repaired_raw_response is not None:
                raw_response = repaired_raw_response
            validation_result = self._validate_rendered_output(
                rendered_output,
                prefetch_bundle,
                deep_research_text=deep_research_text,
            )
            validation_result.repair_attempted = True
            if not validation_result.is_valid:
                issue_text = "; ".join(validation_result.issues)
                raise ExternalAPIError(f"Hanall research validation failed after repair: {issue_text}")
        return HanallArtifact(
            artifact_key=self._artifact_key(artifact_date),
            artifact_date=artifact_date,
            summary_text=rendered_output.public_text,
            detail_text=rendered_output.admin_text,
            model_name=self._settings.kimi_model,
            raw_response={
                "completion": raw_response,
                "prefetch": prefetch_bundle.model_dump(mode="json"),
                "parse": {
                    "parse_ok": rendered_output.parse_ok,
                    "strict_block_parse": rendered_output.strict_block_parse,
                    "discarded_envelope_text": rendered_output.discarded_envelope_text,
                    "format_repair_attempted": rendered_output.format_repair_attempted,
                    "required_sections": rendered_output.required_sections,
                    "present_sections": rendered_output.present_sections,
                    "missing_sections": rendered_output.missing_sections,
                },
                "validation": validation_result.model_dump(mode="json"),
                "deep_research": deep_research_metadata,
            },
        )

    async def _attach_options_sentiment_snapshot(self, artifact: HanallArtifact) -> HanallArtifact:
        if self._options_sentiment_service is None:
            return artifact

        collect_daily_summary = getattr(self._options_sentiment_service, "collect_daily_summary", None)
        if not callable(collect_daily_summary):
            return artifact

        raw_response = dict(artifact.raw_response or {})
        try:
            result = await collect_daily_summary(artifact_date_kst=artifact.artifact_date)
            if result.collect_status == "success" and result.summary is not None:
                save_daily_summary = getattr(self._options_sentiment_service, "save_daily_summary", None)
                if callable(save_daily_summary):
                    save_daily_summary(result.summary)
            raw_response["options_sentiment"] = result.snapshot.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            raw_response["options_sentiment"] = {
                "collect_status": "failed",
                "reason": str(exc),
            }
        artifact.raw_response = raw_response
        return artifact

    async def _load_formula_tools(self) -> list[dict[str, Any]]:
        if self._tools_cache is not None:
            return self._tools_cache
        tools: list[dict[str, Any]] = []
        seen_function_names: set[str] = set()
        headers = {"Authorization": f"Bearer {self._settings.kimi_api_key}"}
        async with httpx.AsyncClient(timeout=self._settings.kimi_tool_timeout_seconds) as client:
            for formula_uri in self._settings.kimi_formula_uri_list:
                url = f"{self._settings.kimi_base_url.rstrip('/')}/formulas/{quote(formula_uri, safe='')}/tools"
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
                for tool in self._extract_tool_entries(payload):
                    function_name = ((tool.get("function") or {}).get("name") or "").strip()
                    if not function_name:
                        raise ExternalAPIError(f"Formula tool without function name: {formula_uri}")
                    if function_name in seen_function_names:
                        raise ExternalAPIError(f"Duplicated function.name in formula tools: {function_name}")
                    seen_function_names.add(function_name)
                    self._tool_name_to_formula_uri[function_name] = formula_uri
                    tools.append(tool)
        self._tools_cache = tools
        return tools

    @staticmethod
    def _extract_tool_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(payload.get("tools"), list):
            return payload["tools"]
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload, list):
            return payload
        raise ExternalAPIError("Unsupported formula tools payload shape")

    async def _resolve_tool_calls(self, tool_calls: list[Any]) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._settings.kimi_api_key}"}
        async with httpx.AsyncClient(timeout=self._settings.kimi_tool_timeout_seconds) as client:
            tasks = [self._invoke_formula(client, headers, tool_call) for tool_call in tool_calls]
            return await asyncio.gather(*tasks)

    async def _invoke_formula(self, client: httpx.AsyncClient, headers: dict[str, str], tool_call: Any) -> dict[str, Any]:
        function_name = tool_call.function.name
        formula_uri = self._tool_name_to_formula_uri.get(function_name)
        if not formula_uri:
            raise ExternalAPIError(f"No formula URI mapped for function.name={function_name}")
        fiber_payload = self._build_formula_fiber_payload(tool_call, function_name)
        url = f"{self._settings.kimi_base_url.rstrip('/')}/formulas/{quote(formula_uri, safe='')}/fibers"
        for attempt in range(self.FORMULA_MAX_RETRIES + 1):
            try:
                response = await client.post(url, headers=headers, json=fiber_payload)
                response.raise_for_status()
                payload = response.json()
                context = payload.get("context") or {}
                content = context.get("output") or context.get("encrypted_output")
                if content is None:
                    raise ExternalAPIError(f"Formula fiber returned no content for {function_name}")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                return {"role": "tool", "tool_call_id": tool_call.id, "content": content}
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code not in self.RETRYABLE_FORMULA_STATUS_CODES or attempt >= self.FORMULA_MAX_RETRIES:
                    raise
            except httpx.RequestError:
                if attempt >= self.FORMULA_MAX_RETRIES:
                    raise
        raise ExternalAPIError(f"Formula invocation failed after retries for {function_name}")

    async def _request_final_render_without_tools(
        self,
        client: AsyncOpenAI,
        messages: list[dict[str, Any]],
        deadline: float,
    ) -> Any:
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            raise ExternalAPIError("Hanall research exceeded overall deadline before final synthesis")
        forced_final_message = {
            "role": "user",
            "content": (
                "추가 도구 호출을 중단하고, 이미 수집한 정보만 사용해 지금 즉시 최종 답변을 작성하라.\n"
                "public_brief는 각 섹션 제목 줄 끝에 건수/개수를 붙여라. 예: `1. 🏢 한올/IMVT 직접 업데이트 : 2건`.\n"
                "건수/개수는 제목 줄에만 쓰고 bullet에서는 `직접 업데이트 1건`처럼 반복하지 말라.\n"
                "bullet 1개는 사실 1건 또는 포인트 1개만 담아라. 2건이면 bullet 2개로 나눠라.\n"
                "직접 회사 SEC/DART/KRX/Form 4 공시가 확인되면 public_brief 1번 섹션에는 filing 이름만 쓰지 말고 사건 의미를 먼저 적어라.\n"
                "가능하면 이벤트 유형(신규 RSU/stock option 부여, sell to cover 매도, 옵션 행사 등), 대상자 직책, 핵심 수량/거래일을 한 문장에 포함하라.\n"
                "B. Confirmed Updates — Company Direct 표에 넣는 direct filing fact와 핵심 숫자/날짜를 public_brief 1번 섹션에도 축약 반영하라.\n"
                "공식 API/source status가 unavailable로 표시된 direct source는 '신규 없음'으로 단정하지 말고 Coverage Gaps/Omission Audit에 반영하라.\n"
                "최상위 태그는 <public_brief>...</public_brief> 와 <admin_report>...</admin_report> 두 개만 포함하라.\n"
                "태그 밖 텍스트, 사족, 조사 계획은 금지한다."
            ),
        }
        return await self._request_chat_completion(
            client,
            messages=[*messages, forced_final_message],
            tools=None,
            request_timeout_seconds=remaining_seconds,
            deadline=deadline,
        )

    async def _run_direct_event_deep_research(
        self,
        *,
        client: AsyncOpenAI,
        messages: list[dict[str, Any]],
        initial_final_text: str,
        candidates: list[HanallStructuredFact],
        prefetch_bundle: HanallApiBundle,
        tools: list[dict[str, Any]],
        deadline: float,
        previous_deep_research_text: str = "",
        quality_issues: list[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        deep_messages = [
            *self._messages_with_initial_final(messages, initial_final_text),
            {
                "role": "user",
                "content": self._build_deep_research_prompt(
                    candidates=candidates,
                    prefetch_bundle=prefetch_bundle,
                    previous_deep_research_text=previous_deep_research_text,
                    quality_issues=quality_issues or [],
                ),
            },
        ]
        raw_response: dict[str, Any] = {}
        result_text = ""
        web_search_rounds = 0
        max_iterations = max(1, self._settings.kimi_max_iterations)

        for iteration_index in range(max_iterations):
            if asyncio.get_running_loop().time() >= deadline:
                raise ExternalAPIError("Hanall direct event deep research exceeded overall deadline")
            response = await self._request_chat_completion(
                client,
                messages=deep_messages,
                tools=tools or None,
                request_timeout_seconds=self._settings.kimi_completion_timeout_seconds,
                deadline=deadline,
            )
            raw_response = response.model_dump(mode="json")
            choice = response.choices[0]
            assistant_message = choice.message
            if choice.finish_reason != "tool_calls" or not assistant_message.tool_calls:
                deep_messages.append(self._assistant_message_to_context(assistant_message))
                result_text = assistant_message.content or ""
                break

            has_web_search = self._tool_calls_include_function(assistant_message.tool_calls, "web_search")
            if has_web_search and web_search_rounds >= self.DEEP_RESEARCH_MAX_WEB_SEARCH_ROUNDS:
                self._append_assistant_context_without_tool_calls(deep_messages, assistant_message)
                break
            if iteration_index == max_iterations - 1:
                self._append_assistant_context_without_tool_calls(deep_messages, assistant_message)
                break

            deep_messages.append(self._assistant_message_to_context(assistant_message))
            deep_messages.extend(await self._resolve_tool_calls(assistant_message.tool_calls))
            if has_web_search:
                web_search_rounds += 1
                if web_search_rounds >= self.DEEP_RESEARCH_MAX_WEB_SEARCH_ROUNDS:
                    break

        if result_text:
            return result_text, raw_response

        response = await self._request_deep_research_final_without_tools(
            client=client,
            messages=deep_messages,
            candidates=candidates,
            deadline=deadline,
        )
        return response.choices[0].message.content or "", response.model_dump(mode="json")

    async def _request_deep_research_final_without_tools(
        self,
        *,
        client: AsyncOpenAI,
        messages: list[dict[str, Any]],
        candidates: list[HanallStructuredFact],
        deadline: float,
    ) -> Any:
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            raise ExternalAPIError("Hanall research exceeded overall deadline before direct event deep synthesis")
        forced_message = {
            "role": "user",
            "content": (
                "추가 도구 호출을 중단하고, 이미 확인한 공식 출처 정보만 사용해 2차 딥 리서치 결과를 작성하라.\n"
                "최종 public_brief/admin_report 태그는 쓰지 말라.\n"
                "각 후보 이벤트별로 본문 확인 여부, 핵심 사실, public_brief에 넣어야 할 1문장, Coverage Gap을 구분하라.\n\n"
                f"{self._format_deep_research_candidates(candidates)}"
            ),
        }
        return await self._request_chat_completion(
            client,
            messages=[*messages, forced_message],
            tools=None,
            request_timeout_seconds=remaining_seconds,
            deadline=deadline,
        )

    async def _request_final_render_with_deep_research(
        self,
        *,
        client: AsyncOpenAI,
        messages: list[dict[str, Any]],
        initial_final_text: str,
        prefetch_bundle: HanallApiBundle,
        candidates: list[HanallStructuredFact],
        deep_research_text: str,
        deadline: float,
    ) -> Any:
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            raise ExternalAPIError("Hanall research exceeded overall deadline before direct event final render")
        render_message = {
            "role": "user",
            "content": (
                "2차 딥 리서치 결과를 반영해 최종 답변을 다시 작성하라.\n"
                "중요 direct company 이벤트는 filing명만 반복하지 말고 2차 확인 결과의 임상/규제/재무/runway/개발일정 핵심 사실 중 최소 1개를 public_brief 1번 섹션에 반영하라.\n"
                "Immunovant 8-K/실적발표의 2차 확인 결과에 D2T RA ACR20/50/70 또는 반응률 수치가 있으면, net loss/R&D 비용보다 그 임상 결과 수치를 public_brief 1번 첫 bullet에 우선 반영하라.\n"
                "admin_report의 B. Confirmed Updates — Company Direct에는 2차 확인 결과의 원문 기반 숫자/날짜/자산명/적응증을 반영하라.\n"
                "2차 확인이 실패했거나 본문 접근이 제한된 후보는 public_brief에 추정 내용을 쓰지 말고 admin_report의 Coverage Gaps/Omission Audit에 남겨라.\n"
                "최상위 태그는 <public_brief>...</public_brief> 와 <admin_report>...</admin_report> 두 개만 포함하라.\n"
                "태그 밖 텍스트는 금지한다.\n\n"
                f"{prefetch_bundle.prompt_block()}\n\n"
                f"{self._build_deep_research_context_block(candidates, deep_research_text)}"
            ),
        }
        return await self._request_chat_completion(
            client,
            messages=[*self._messages_with_initial_final(messages, initial_final_text), render_message],
            tools=None,
            request_timeout_seconds=remaining_seconds,
            deadline=deadline,
        )

    async def _parse_or_repair_rendered_output(
        self,
        *,
        client: AsyncOpenAI,
        messages: list[dict[str, Any]],
        final_text: str,
        prefetch_bundle: HanallApiBundle,
        deadline: float,
        deep_research_text: str = "",
        deep_research_candidates: list[HanallStructuredFact] | None = None,
    ) -> tuple[HanallRenderedOutput, dict[str, Any] | None, str | None]:
        try:
            return self._parse_rendered_output(final_text), None, None
        except ExternalAPIError as exc:
            if str(exc) != self.BLOCK_PARSE_ERROR_MESSAGE:
                raise

        response = await self._request_format_repair_without_tools(
            client,
            messages=messages,
            invalid_text=final_text,
            prefetch_bundle=prefetch_bundle,
            deep_research_text=deep_research_text,
            deep_research_candidates=deep_research_candidates or [],
            deadline=deadline,
        )
        repaired_raw_response = response.model_dump(mode="json")
        corrected_text = response.choices[0].message.content or ""
        if not corrected_text:
            raise ExternalAPIError("Hanall research format repair produced no final text")
        rendered_output = self._parse_rendered_output(corrected_text)
        rendered_output.format_repair_attempted = True
        return rendered_output, repaired_raw_response, corrected_text

    async def _request_format_repair_without_tools(
        self,
        client: AsyncOpenAI,
        *,
        messages: list[dict[str, Any]],
        invalid_text: str,
        prefetch_bundle: HanallApiBundle,
        deadline: float,
        deep_research_text: str = "",
        deep_research_candidates: list[HanallStructuredFact] | None = None,
    ) -> Any:
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            raise ExternalAPIError("Hanall research exceeded overall deadline before format repair")
        repair_message = {
            "role": "user",
            "content": (
                "직전 최종 답변은 저장 전 출력 태그 검증에 실패했다. 추가 도구 호출 없이 이미 수집한 정보만 사용해 다시 작성하라.\n"
                "최상위 태그는 정확히 <public_brief>...</public_brief> 와 <admin_report>...</admin_report> 두 개만 포함하라.\n"
                "태그 순서는 public_brief 다음 admin_report로 고정한다.\n"
                "태그 밖 텍스트, 마크다운 코드펜스, 인사말, 사족은 금지한다.\n"
                "admin_report에는 필수 섹션 제목을 그대로 유지하라.\n\n"
                f"{prefetch_bundle.prompt_block()}\n\n"
                f"{self._build_deep_research_context_block(deep_research_candidates or [], deep_research_text)}\n\n"
                "[Invalid draft to rewrap]\n"
                f"{invalid_text}"
            ),
        }
        return await self._request_chat_completion(
            client,
            messages=[*messages, {"role": "assistant", "content": invalid_text}, repair_message],
            tools=None,
            request_timeout_seconds=remaining_seconds,
            deadline=deadline,
        )

    async def _request_validation_repair_without_tools(
        self,
        client: AsyncOpenAI,
        *,
        messages: list[dict[str, Any]],
        invalid_text: str,
        prefetch_bundle: HanallApiBundle,
        issues: list[str],
        deadline: float,
        deep_research_text: str = "",
        deep_research_candidates: list[HanallStructuredFact] | None = None,
    ) -> Any:
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            raise ExternalAPIError("Hanall research exceeded overall deadline before validation repair")
        issue_lines = "\n".join(f"- {issue}" for issue in issues) if issues else "- 없음"
        repair_message = {
            "role": "user",
            "content": (
                "직전 최종 답변은 저장 전 검증에 실패했다. 추가 도구 호출 없이 이미 수집한 정보만 사용해 최종 답변을 다시 작성하라.\n"
                "아래 검증 이슈를 모두 해결해야 한다.\n"
                f"{issue_lines}\n"
                "structured API direct fact는 B. Confirmed Updates — Company Direct와 public_brief 1번 섹션에 반영하라.\n"
                "2차 딥 리서치 결과가 있으면 filing명만 쓰지 말고 그 결과의 핵심 숫자/날짜/자산명/적응증을 public_brief 1번과 admin_report B에 반영하라.\n"
                "structured API source status가 unavailable인 범주는 direct update를 '신규 없음'으로 단정하지 말고 Coverage Gaps/Omission Audit에 반영하라.\n"
                "최상위 태그는 <public_brief>...</public_brief> 와 <admin_report>...</admin_report> 두 개만 포함하라.\n"
                "태그 밖 텍스트는 금지한다.\n\n"
                f"{prefetch_bundle.prompt_block()}\n\n"
                f"{self._build_deep_research_context_block(deep_research_candidates or [], deep_research_text)}\n\n"
                "[Invalid draft to fix]\n"
                f"{invalid_text}"
            ),
        }
        return await self._request_chat_completion(
            client,
            messages=[*messages, {"role": "assistant", "content": invalid_text}, repair_message],
            tools=None,
            request_timeout_seconds=remaining_seconds,
            deadline=deadline,
        )

    def _select_deep_research_candidates(self, prefetch_bundle: HanallApiBundle) -> list[HanallStructuredFact]:
        candidates: list[HanallStructuredFact] = []
        for fact in prefetch_bundle.direct_validation_facts():
            if not self._is_deep_research_candidate(fact):
                continue
            candidates.append(fact)
            if len(candidates) >= self.DEEP_RESEARCH_MAX_CANDIDATES:
                break
        return candidates

    def _is_deep_research_candidate(self, fact: HanallStructuredFact) -> bool:
        if fact.source_type != "filing":
            return False
        haystack = self._normalize_validation_text(
            " ".join(
                item
                for item in (
                    fact.entity,
                    fact.source_name,
                    fact.category,
                    fact.title,
                    fact.fact_text,
                    fact.source_id or "",
                    fact.source_url or "",
                )
                if item
            )
        )
        return any(keyword in haystack for keyword in self.DEEP_RESEARCH_TRIGGER_KEYWORDS)

    @staticmethod
    def _deep_research_candidate_payload(fact: HanallStructuredFact) -> dict[str, Any]:
        return {
            "source_name": fact.source_name,
            "entity": fact.entity,
            "title": fact.title,
            "source_id": fact.source_id,
            "source_url": fact.source_url,
            "observed_at": fact.observed_at.isoformat() if fact.observed_at is not None else None,
            "observed_date": fact.observed_date.isoformat() if fact.observed_date is not None else None,
            "validation_mode": fact.validation_mode,
        }

    def _build_deep_research_prompt(
        self,
        *,
        candidates: list[HanallStructuredFact],
        prefetch_bundle: HanallApiBundle,
        previous_deep_research_text: str = "",
        quality_issues: list[str] | None = None,
    ) -> str:
        repair_block = ""
        if quality_issues:
            issue_lines = "\n".join(f"- {issue}" for issue in quality_issues)
            repair_block = (
                "\n[Previous deep research was incomplete]\n"
                f"{previous_deep_research_text.strip() or '- 없음'}\n\n"
                "[Issues to fix]\n"
                f"{issue_lines}\n"
                "위 이슈를 해결하기 위해 공식 8-K Exhibit 99.1 또는 Immunovant 공식 PR 원문을 다시 확인하라.\n"
            )
        return (
            "2차 딥 리서치 단계다. 아래 direct company 이벤트는 공식 API에서 감지됐지만, 최종 브리핑에 filing명만 쓰면 중요한 내용이 누락될 수 있다.\n"
            "각 후보의 공식 문서/회사 IR/SEC 원문을 우선 열어 본문 핵심을 확인하라. 2차/언론 출처는 발견용으로만 쓰고, Confirmed에는 공식 출처로 확인된 사실만 넣어라.\n"
            "SEC 8-K가 Item 2.02/earnings/financial results/business update/corporate update 성격이면 8-K cover page에서 멈추지 말고, 8-K 안의 Exhibit 99.1/EX-99.1 또는 회사 공식 Press Release/공식 PR 링크를 반드시 열어라.\n"
            "Immunovant 8-K/실적발표의 public 우선순위는 1) 새 임상 효능/탑라인 수치(예: D2T RA ACR20/50/70, 반응률, p-value), 2) 핵심 개발 일정, 3) cash/runway, 4) 순손실/R&D 비용 순서다.\n"
            "공식 PR/Exhibit에 D2T RA/IMVT-1402 임상 결과 수치가 있으면 `D2T RA`, `ACR20/50/70`, 각 퍼센트 값을 public brief required points에 반드시 포함하라. 이를 단순히 '2026년 topline 예정'으로 대체하지 말라.\n"
            "추출 대상은 고정 스키마가 아니라 원문에 실제로 있는 투자판단 핵심이다: 임상 결과, 자산/적응증, 규제 일정, 개발 일정, 중단/전략 변경, 재무 결과, 현금/runway, 가이던스.\n"
            "숫자, 날짜, 자산명, 적응증, filing id, 원문 링크를 그대로 보존하라. 확인하지 못한 내용은 추정하지 말고 Coverage Gap으로 표시하라.\n"
            "최종 public_brief/admin_report 태그는 쓰지 말고 아래 형식의 리서치 메모만 작성하라.\n\n"
            "[Output]\n"
            "- status: success | partial | failed\n"
            "- confirmed direct-event details: 후보별 원문 기반 핵심 사실 bullet\n"
            "- public brief required points: 공개 브리핑 1번 섹션에 넣을 1~2개 짧은 한국어 문장\n"
            "- admin report detail points: admin B 섹션에 보존할 숫자/날짜/링크\n"
            "- coverage gaps: 접근 제한/미확인 사항\n\n"
            f"{prefetch_bundle.prompt_block()}\n\n"
            f"{repair_block}\n"
            f"{self._format_deep_research_candidates(candidates)}"
        )

    def _format_deep_research_candidates(self, candidates: list[HanallStructuredFact]) -> str:
        if not candidates:
            return "[Deep research candidate direct events]\n- 없음"
        lines = ["[Deep research candidate direct events]"]
        for index, fact in enumerate(candidates, start=1):
            lines.append(f"{index}. {fact.prompt_line()}")
        return "\n".join(lines)

    def _build_deep_research_context_block(
        self,
        candidates: list[HanallStructuredFact],
        deep_research_text: str,
    ) -> str:
        if not candidates and not deep_research_text.strip():
            return ""
        return "\n".join(
            [
                "[Direct event deep research]",
                "- 아래 내용은 중요 direct company 이벤트를 대상으로 2차 확인한 결과다.",
                "- confirmed detail이 있으면 filing명만 반복하지 말고 public_brief 1번과 admin_report B에 핵심 사실을 반영하라.",
                "- failed/coverage gap이면 추정하지 말고 admin_report의 Coverage Gaps/Omission Audit에 남겨라.",
                "",
                self._format_deep_research_candidates(candidates),
                "",
                "[Deep research result]",
                deep_research_text.strip() or "- 없음",
            ]
        )

    @staticmethod
    def _messages_with_initial_final(messages: list[dict[str, Any]], initial_final_text: str) -> list[dict[str, Any]]:
        if messages:
            last_message = messages[-1]
            if last_message.get("role") == "assistant" and last_message.get("content") == initial_final_text:
                return list(messages)
        return [*messages, {"role": "assistant", "content": initial_final_text}]

    def _build_empty_deep_research_note(self, candidates: list[HanallStructuredFact]) -> str:
        return (
            "status: failed\n"
            "confirmed direct-event details: 없음\n"
            "public brief required points: 없음\n"
            "admin report detail points: 없음\n"
            "coverage gaps: 2차 딥 리서치가 빈 결과를 반환함. 후보는 아래와 같음.\n"
            f"{self._format_deep_research_candidates(candidates)}"
        )

    def _build_failed_deep_research_note(self, candidates: list[HanallStructuredFact], exc: Exception) -> str:
        return (
            "status: failed\n"
            "confirmed direct-event details: 없음\n"
            "public brief required points: 없음\n"
            "admin report detail points: 없음\n"
            f"coverage gaps: 2차 딥 리서치 실패: {exc}. 후보 본문 내용은 추정 금지.\n"
            f"{self._format_deep_research_candidates(candidates)}"
        )

    def _deep_research_quality_issues(
        self,
        candidates: list[HanallStructuredFact],
        deep_research_text: str,
    ) -> list[str]:
        normalized_text = self._normalize_validation_text(deep_research_text)
        if not normalized_text or "status: failed" in normalized_text:
            return []

        issues: list[str] = []
        if any(self._is_immunovant_8k_candidate(fact) for fact in candidates):
            if not any(token in normalized_text for token in self.DEEP_RESEARCH_8K_SOURCE_TOKENS):
                issues.append("Immunovant 8-K deep research가 Exhibit 99.1/official Press Release 확인 여부를 명시하지 않았다")

            has_d2t_ra_topline_without_result_numbers = (
                "d2t ra" in normalized_text
                and "topline" in normalized_text
                and not any(token in normalized_text for token in self.DEEP_RESEARCH_D2T_RA_RESULT_TOKENS)
            )
            if has_d2t_ra_topline_without_result_numbers:
                issues.append(
                    "Immunovant 8-K/PR의 D2T RA 결과가 일정 언급으로만 요약됐고 ACR20/50/70 또는 반응률 수치가 없다"
                )
        return issues

    def _is_immunovant_8k_candidate(self, fact: HanallStructuredFact) -> bool:
        normalized_text = self._normalize_validation_text(
            " ".join(
                item
                for item in (
                    fact.entity,
                    fact.source_name,
                    fact.title,
                    fact.fact_text,
                    fact.source_id or "",
                    fact.source_url or "",
                )
                if item
            )
        )
        return "immunovant" in normalized_text and "8-k" in normalized_text

    async def _collect_prefetch_bundle(self, *, window_start: datetime, window_end: datetime) -> HanallApiBundle:
        if self._hanall_prefetch_service is None:
            return HanallApiBundle()
        collect = getattr(self._hanall_prefetch_service, "collect", None)
        if not callable(collect):
            return HanallApiBundle()
        bundle = await collect(window_start=window_start, window_end=window_end)
        return bundle if isinstance(bundle, HanallApiBundle) else HanallApiBundle.model_validate(bundle)

    def _validate_rendered_output(
        self,
        rendered_output: HanallRenderedOutput,
        prefetch_bundle: HanallApiBundle,
        *,
        deep_research_text: str = "",
    ) -> HanallValidationResult:
        issues: list[str] = []
        admin_text = rendered_output.admin_text
        public_text = rendered_output.public_text

        if prefetch_bundle.has_unavailable_hard_source() and self._claims_no_direct_updates(public_text, admin_text):
            issues.append("direct source가 unavailable인데 direct company 업데이트를 '신규 없음'으로 단정했다")

        direct_public_section = self._extract_public_direct_section(public_text)
        for fact in prefetch_bundle.direct_validation_facts():
            fact_id = fact.source_id or fact.title
            if not self._fact_present(admin_text, fact.validation_tokens()):
                issues.append(f"structured direct fact 누락(admin): {fact_id}")
            if not self._fact_present(direct_public_section, fact.validation_tokens()):
                issues.append(f"structured direct fact 누락(public): {fact_id}")

        deep_research_tokens = self._extract_deep_research_detail_tokens(deep_research_text)
        if deep_research_tokens:
            token_preview = ", ".join(deep_research_tokens[:5])
            if not self._fact_present(direct_public_section, deep_research_tokens):
                issues.append(f"deep research 핵심 내용 누락(public): {token_preview}")
            if not self._fact_present(admin_text, deep_research_tokens):
                issues.append(f"deep research 핵심 내용 누락(admin): {token_preview}")

        return HanallValidationResult(is_valid=not issues, issues=issues)

    @classmethod
    def _extract_deep_research_detail_tokens(cls, deep_research_text: str) -> list[str]:
        if not deep_research_text.strip():
            return []
        normalized_text = cls._normalize_validation_text(deep_research_text)
        if "status: failed" in normalized_text or "public brief required points: 없음" in normalized_text:
            return []
        tokens: list[str] = []
        seen: set[str] = set()
        for match in cls.DEEP_RESEARCH_DETAIL_TOKEN_PATTERN.finditer(deep_research_text):
            token = re.sub(r"\s+", " ", match.group(0)).strip()
            normalized_token = cls._normalize_validation_text(token)
            if not normalized_token or normalized_token in seen:
                continue
            seen.add(normalized_token)
            tokens.append(token)
            if len(tokens) >= 12:
                break
        return tokens

    def _claims_no_direct_updates(self, public_text: str, admin_text: str) -> bool:
        normalized_text = self._normalize_validation_text(f"{public_text}\n{admin_text}")
        return any(pattern in normalized_text for pattern in self.DIRECT_NO_UPDATE_PATTERNS)

    def _fact_present(self, admin_text: str, tokens: list[str]) -> bool:
        normalized_admin = self._normalize_validation_text(admin_text)
        for token in tokens:
            normalized_token = self._normalize_validation_text(token)
            if normalized_token and normalized_token in normalized_admin:
                return True
        return False

    @staticmethod
    def _extract_public_direct_section(public_text: str) -> str:
        lines = public_text.splitlines()
        collecting = False
        collected_lines: list[str] = []
        for line in lines:
            if line.startswith("1. 🏢 한올/IMVT 직접 업데이트"):
                collecting = True
                collected_lines.append(line)
                continue
            if collecting and re.match(r"^\d+\.\s", line):
                break
            if collecting:
                collected_lines.append(line)
        return "\n".join(collected_lines).strip()

    async def _request_chat_completion(
        self,
        client: AsyncOpenAI,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        request_timeout_seconds: float,
        deadline: float,
    ) -> Any:
        retry_delays = self._settings.hanall_collect_retry_delay_list
        loop = asyncio.get_running_loop()

        for attempt_index in range(len(retry_delays) + 1):
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                raise ExternalAPIError("Hanall research exceeded overall deadline")

            try:
                return await client.chat.completions.create(
                    model=self._settings.kimi_model,
                    messages=messages,
                    tools=tools,
                    timeout=min(request_timeout_seconds, remaining_seconds),
                )
            except Exception as exc:  # noqa: BLE001
                if not self._is_retryable_completion_error(exc) or attempt_index >= len(retry_delays):
                    raise

                retry_delay = retry_delays[attempt_index]
                if deadline - loop.time() <= retry_delay:
                    raise ExternalAPIError("Hanall research exceeded overall deadline before retrying overloaded completion") from exc

                logger.warning(
                    "hanall_completion_retry_scheduled",
                    extra={
                        "attempt": attempt_index + 1,
                        "retry_in_seconds": retry_delay,
                        "status_code": self._completion_error_status_code(exc),
                        "error_type": self._completion_error_type(exc),
                    },
                )
                await self._sleep_before_retry(retry_delay)

        raise ExternalAPIError("Hanall research completion retry loop exited unexpectedly")

    async def _sleep_before_retry(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    @classmethod
    def _is_retryable_completion_error(cls, exc: Exception) -> bool:
        if isinstance(exc, (APITimeoutError, httpx.TimeoutException)):
            return True
        status_code = cls._completion_error_status_code(exc)
        if status_code in cls.RETRYABLE_COMPLETION_STATUS_CODES:
            return True
        error_type = cls._completion_error_type(exc)
        return error_type == "engine_overloaded_error"

    @staticmethod
    def _completion_error_status_code(exc: Exception) -> int | None:
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        response = getattr(exc, "response", None)
        response_status_code = getattr(response, "status_code", None)
        return response_status_code if isinstance(response_status_code, int) else None

    @staticmethod
    def _completion_error_type(exc: Exception) -> str | None:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                error_type = error.get("type")
                if isinstance(error_type, str):
                    return error_type
        message = str(exc)
        if "engine_overloaded_error" in message:
            return "engine_overloaded_error"
        return None

    @staticmethod
    def _build_formula_fiber_payload(tool_call: Any, function_name: str) -> dict[str, Any]:
        raw_arguments = tool_call.function.arguments or "{}"
        if isinstance(raw_arguments, str):
            json.loads(raw_arguments)
        else:
            raw_arguments = json.dumps(raw_arguments, ensure_ascii=False)

        return {"name": function_name, "arguments": raw_arguments}

    @staticmethod
    def _assistant_message_to_context(message: Any) -> dict[str, Any]:
        if hasattr(message, "model_dump"):
            payload = message.model_dump(mode="json", exclude_none=True)
        elif isinstance(message, dict):
            payload = dict(message)
        else:
            payload = {"role": "assistant", "content": getattr(message, "content", None)}
        payload.setdefault("role", "assistant")
        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content:
            payload["reasoning_content"] = reasoning_content
        return payload

    @staticmethod
    def _tool_calls_include_function(tool_calls: list[Any], function_name: str) -> bool:
        return any(getattr(tool_call.function, "name", None) == function_name for tool_call in tool_calls)

    @staticmethod
    def _append_assistant_context_without_tool_calls(messages: list[dict[str, Any]], message: Any) -> None:
        payload = HanallResearchService._assistant_message_to_context(message)
        payload.pop("tool_calls", None)
        if payload.get("content") is None and not payload.get("reasoning_content"):
            return
        messages.append(payload)

    @staticmethod
    def _artifact_key(artifact_date: date) -> str:
        return f"hanall:{artifact_date.isoformat()}"

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self._settings.kimi_api_key, base_url=self._settings.kimi_base_url)
        return self._client

    def _build_collect_prompt(
        self,
        artifact_date: date,
        *,
        run_time: datetime,
        window_start: datetime,
        prefetch_bundle: HanallApiBundle,
    ) -> str:
        required_sections = "\n".join(f"- {section}" for section in self.REQUIRED_ADMIN_SECTIONS)
        prompt = self._prompts.hanall_runtime_wrapper.format(
            business_spec=self._hanall_spec,
            artifact_date=artifact_date.isoformat(),
            run_time_kst=run_time.strftime("%Y-%m-%d %H:%M KST"),
            window_start_kst=window_start.strftime("%Y-%m-%d %H:%M KST"),
            window_end_kst=run_time.strftime("%Y-%m-%d %H:%M KST"),
            max_web_search_rounds=self._settings.hanall_max_web_search_rounds,
            required_sections=required_sections,
        )
        bundle_block = prefetch_bundle.prompt_block()
        if bundle_block:
            prompt = f"{prompt}\n\n{bundle_block}"
        return prompt

    def _parse_rendered_output(self, text: str) -> HanallRenderedOutput:
        match = self.OUTPUT_BLOCK_PATTERN.match(text)
        if match is None:
            recovered = self._extract_unique_output_blocks(text)
            strict_block_parse = False
            if recovered is None:
                raise ExternalAPIError(self.BLOCK_PARSE_ERROR_MESSAGE)
            public_text, admin_text = recovered
        else:
            strict_block_parse = True
            public_text = match.group("public")
            admin_text = match.group("admin")
        public_text = public_text.strip()
        admin_text = admin_text.strip()
        if not public_text:
            raise ExternalAPIError("Hanall research response contained an empty <public_brief> block")
        if not admin_text:
            raise ExternalAPIError("Hanall research response contained an empty <admin_report> block")

        present_headings = self._extract_normalized_admin_headings(admin_text)
        normalized_required_sections = {
            self._normalize_section_heading(section): section for section in self.REQUIRED_ADMIN_SECTIONS
        }
        present_sections = [
            section for normalized, section in normalized_required_sections.items() if normalized in present_headings
        ]
        missing_sections = [
            section for normalized, section in normalized_required_sections.items() if normalized not in present_headings
        ]
        if missing_sections:
            missing_joined = ", ".join(missing_sections)
            raise ExternalAPIError(f"Hanall admin report missing required sections: {missing_joined}")

        return HanallRenderedOutput(
            public_text=public_text,
            admin_text=admin_text,
            strict_block_parse=strict_block_parse,
            discarded_envelope_text=not strict_block_parse,
            required_sections=list(self.REQUIRED_ADMIN_SECTIONS),
            present_sections=present_sections,
            missing_sections=missing_sections,
        )

    def _extract_unique_output_blocks(self, text: str) -> tuple[str, str] | None:
        public_matches = list(self.PUBLIC_BLOCK_PATTERN.finditer(text))
        admin_matches = list(self.ADMIN_BLOCK_PATTERN.finditer(text))
        if len(public_matches) != 1 or len(admin_matches) != 1:
            return None
        public_match = public_matches[0]
        admin_match = admin_matches[0]
        if public_match.start() > admin_match.start():
            return None
        if public_match.end() > admin_match.start():
            return None
        return public_match.group("public"), admin_match.group("admin")

    @classmethod
    def _extract_normalized_admin_headings(cls, admin_text: str) -> set[str]:
        headings: set[str] = set()
        for raw_line in admin_text.splitlines():
            normalized = cls._normalize_section_heading(raw_line)
            if normalized:
                headings.add(normalized)
        return headings

    @classmethod
    def _normalize_section_heading(cls, heading: str) -> str:
        normalized = unicodedata.normalize("NFKC", heading).strip()
        if not normalized:
            return ""
        normalized = cls.SECTION_HEADING_MARKUP_PATTERN.sub("", normalized)
        emphasis_match = cls.SECTION_EMPHASIS_PATTERN.match(normalized)
        if emphasis_match is not None:
            normalized = emphasis_match.group("body").strip()
        normalized = normalized.strip("`").strip()
        normalized = cls.DASH_VARIANTS_PATTERN.sub("-", normalized)
        normalized = re.sub(r"\s*-\s*", " - ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip().lower()

    @classmethod
    def _normalize_validation_text(cls, text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text).strip().lower()
        normalized = cls.DASH_VARIANTS_PATTERN.sub("-", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized
