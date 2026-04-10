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
from openai import AsyncOpenAI

from app.config import Settings
from app.errors import ConfigurationError, ExternalAPIError
from app.repositories import ArtifactRepository
from app.schemas import HanallArtifact, HanallRenderedOutput, PromptLibrary

logger = logging.getLogger(__name__)


class HanallResearchService:
    RETRYABLE_COMPLETION_STATUS_CODES = {429, 500, 502, 503, 504}
    RETRYABLE_FORMULA_STATUS_CODES = {429, 500, 502, 503, 504}
    FORMULA_MAX_RETRIES = 2
    PUBLIC_BRIEF_BULLET_PREFIX = "- "
    PUBLIC_BRIEF_CONTINUATION_PREFIX = "  "
    PUBLIC_BRIEF_BODY_DISPLAY_WIDTH = 64
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
    SECTION_HEADING_MARKUP_PATTERN = re.compile(r"^#+\s*")
    SECTION_EMPHASIS_PATTERN = re.compile(r"^\*{1,2}(?P<body>.+?)\*{1,2}$")
    DASH_VARIANTS_PATTERN = re.compile(r"[\u2010-\u2015\u2212-]")

    def __init__(self, *, settings: Settings, prompts: PromptLibrary, artifact_repository: ArtifactRepository) -> None:
        self._settings = settings
        self._prompts = prompts
        self._artifact_repository = artifact_repository
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
        for raw_line in public_text.splitlines():
            stripped_line = raw_line.rstrip()
            if stripped_line.startswith(cls.PUBLIC_BRIEF_BULLET_PREFIX):
                formatted_lines.extend(cls._wrap_public_brief_bullet(stripped_line))
                continue
            formatted_lines.append(stripped_line)
        return "\n".join(formatted_lines).strip()

    @classmethod
    def _wrap_public_brief_bullet(cls, line: str) -> list[str]:
        body = line[len(cls.PUBLIC_BRIEF_BULLET_PREFIX) :].strip()
        if not body or cls._display_width(body) <= cls.PUBLIC_BRIEF_BODY_DISPLAY_WIDTH:
            return [line]

        comma_segments = cls._split_top_level_comma_segments(body)
        logical_lines = (
            cls._pack_comma_segments(comma_segments, cls.PUBLIC_BRIEF_BODY_DISPLAY_WIDTH)
            if len(comma_segments) > 1
            else [body]
        )

        wrapped_lines: list[str] = []
        for logical_line in logical_lines:
            wrapped_lines.extend(cls._wrap_body_text(logical_line, cls.PUBLIC_BRIEF_BODY_DISPLAY_WIDTH))

        if len(wrapped_lines) <= 1:
            return [line]
        return cls._prefix_bullet_lines(wrapped_lines)

    @classmethod
    def _split_top_level_comma_segments(cls, text: str) -> list[str]:
        segments: list[str] = []
        buffer: list[str] = []
        depth = 0
        openers = "([{"
        closers = ")]}"

        for character in text:
            if character in openers:
                depth += 1
                buffer.append(character)
                continue
            if character in closers:
                depth = max(depth - 1, 0)
                buffer.append(character)
                continue
            if character == "," and depth == 0:
                segment = "".join(buffer).strip()
                if segment:
                    segments.append(segment)
                buffer = []
                continue
            buffer.append(character)

        tail = "".join(buffer).strip()
        if tail:
            segments.append(tail)
        return segments

    @classmethod
    def _pack_comma_segments(cls, segments: list[str], width: int) -> list[str]:
        packed_lines: list[str] = []
        current = ""

        for index, segment in enumerate(segments):
            suffix = "," if index < len(segments) - 1 else ""
            piece = f"{segment}{suffix}"
            candidate = piece if not current else f"{current} {piece}"
            if current and cls._display_width(candidate) > width:
                packed_lines.append(current)
                current = piece
                continue
            current = candidate

        if current:
            packed_lines.append(current)
        return packed_lines

    @classmethod
    def _wrap_body_text(cls, text: str, width: int) -> list[str]:
        words = text.split()
        if not words:
            return [text]

        wrapped_lines: list[str] = []
        current = ""
        for word in words:
            if cls._display_width(word) > width:
                if current:
                    wrapped_lines.append(current)
                    current = ""
                split_words = cls._split_token_by_display_width(word, width)
                wrapped_lines.extend(split_words[:-1])
                current = split_words[-1]
                continue

            candidate = word if not current else f"{current} {word}"
            if current and cls._display_width(candidate) > width:
                wrapped_lines.append(current)
                current = word
                continue
            current = candidate

        if current:
            wrapped_lines.append(current)
        return wrapped_lines

    @classmethod
    def _split_token_by_display_width(cls, token: str, width: int) -> list[str]:
        parts: list[str] = []
        current = ""
        for character in token:
            if current and cls._display_width(current + character) > width:
                parts.append(current)
                current = character
                continue
            current += character
        if current:
            parts.append(current)
        return parts or [token]

    @classmethod
    def _prefix_bullet_lines(cls, lines: list[str]) -> list[str]:
        prefixed_lines: list[str] = []
        for index, line in enumerate(lines):
            prefix = cls.PUBLIC_BRIEF_BULLET_PREFIX if index == 0 else cls.PUBLIC_BRIEF_CONTINUATION_PREFIX
            prefixed_lines.append(f"{prefix}{line}")
        return prefixed_lines

    @staticmethod
    def _display_width(text: str) -> int:
        width = 0
        for character in text:
            width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        return width

    async def _run_research(self, artifact_date: date) -> HanallArtifact:
        if not self._settings.kimi_api_key:
            raise ConfigurationError("KIMI_API_KEY is not configured")
        client = self._get_client()
        loaded_tools = self._load_formula_tools()
        tools = await loaded_tools if inspect.isawaitable(loaded_tools) else loaded_tools
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "반드시 한국어로 답하고 최신 자료를 우선 사용하라."},
            {"role": "user", "content": self._build_collect_prompt(artifact_date)},
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
                request_timeout_seconds=self._settings.kimi_tool_timeout_seconds,
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
        return HanallArtifact(
            artifact_key=self._artifact_key(artifact_date),
            artifact_date=artifact_date,
            summary_text=rendered_output.public_text,
            detail_text=rendered_output.admin_text,
            model_name=self._settings.kimi_model,
            raw_response={
                "completion": raw_response,
                "parse": {
                    "parse_ok": rendered_output.parse_ok,
                    "required_sections": rendered_output.required_sections,
                    "present_sections": rendered_output.present_sections,
                    "missing_sections": rendered_output.missing_sections,
                },
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

        payload: dict[str, Any] = {
            "tool_call": {
                "type": "function",
                "function": {
                    "name": function_name,
                    "arguments": raw_arguments,
                },
            }
        }
        if getattr(tool_call, "id", None):
            payload["tool_call"]["id"] = tool_call.id
        return payload

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

    def _build_collect_prompt(self, artifact_date: date) -> str:
        run_time = datetime.now(self._timezone)
        window_start = run_time - timedelta(hours=24)
        required_sections = "\n".join(f"- {section}" for section in self.REQUIRED_ADMIN_SECTIONS)
        return self._prompts.hanall_runtime_wrapper.format(
            business_spec=self._hanall_spec,
            artifact_date=artifact_date.isoformat(),
            run_time_kst=run_time.strftime("%Y-%m-%d %H:%M KST"),
            window_start_kst=window_start.strftime("%Y-%m-%d %H:%M KST"),
            window_end_kst=run_time.strftime("%Y-%m-%d %H:%M KST"),
            required_sections=required_sections,
        )

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
