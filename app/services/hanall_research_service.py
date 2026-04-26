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
        re.DOTALL,
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
    ) -> None:
        self._settings = settings
        self._prompts = prompts
        self._artifact_repository = artifact_repository
        self._hanall_prefetch_service = hanall_prefetch_service
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
        rendered_output = self._parse_rendered_output(final_text)
        validation_result = self._validate_rendered_output(rendered_output, prefetch_bundle)
        if not validation_result.is_valid:
            response = await self._request_validation_repair_without_tools(
                client,
                messages=messages,
                invalid_text=final_text,
                prefetch_bundle=prefetch_bundle,
                issues=validation_result.issues,
                deadline=deadline,
            )
            raw_response = response.model_dump(mode="json")
            corrected_text = response.choices[0].message.content or ""
            if not corrected_text:
                raise ExternalAPIError("Hanall research repair produced no final text")
            rendered_output = self._parse_rendered_output(corrected_text)
            validation_result = self._validate_rendered_output(rendered_output, prefetch_bundle)
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
                    "required_sections": rendered_output.required_sections,
                    "present_sections": rendered_output.present_sections,
                    "missing_sections": rendered_output.missing_sections,
                },
                "validation": validation_result.model_dump(mode="json"),
            },
        )

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

    async def _request_validation_repair_without_tools(
        self,
        client: AsyncOpenAI,
        *,
        messages: list[dict[str, Any]],
        invalid_text: str,
        prefetch_bundle: HanallApiBundle,
        issues: list[str],
        deadline: float,
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
                "structured API source status가 unavailable인 범주는 direct update를 '신규 없음'으로 단정하지 말고 Coverage Gaps/Omission Audit에 반영하라.\n"
                "최상위 태그는 <public_brief>...</public_brief> 와 <admin_report>...</admin_report> 두 개만 포함하라.\n"
                "태그 밖 텍스트는 금지한다.\n\n"
                f"{prefetch_bundle.prompt_block()}\n\n"
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

        return HanallValidationResult(is_valid=not issues, issues=issues)

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
            raise ExternalAPIError("Hanall research response must contain only <public_brief> and <admin_report> blocks")
        public_text = match.group("public").strip()
        admin_text = match.group("admin").strip()
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
            required_sections=list(self.REQUIRED_ADMIN_SECTIONS),
            present_sections=present_sections,
            missing_sections=missing_sections,
        )

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
