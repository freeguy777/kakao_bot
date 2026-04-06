from __future__ import annotations

import asyncio
import inspect
import json
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


class HanallResearchService:
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
        return self._prompts.hanall_public_format.format(summary=artifact.summary_text)

    def render_admin_message(self, artifact: HanallArtifact) -> str:
        return self._prompts.hanall_admin_format.format(detail=artifact.detail_text)

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

        for _ in range(self._settings.kimi_max_iterations):
            if asyncio.get_running_loop().time() >= deadline:
                raise ExternalAPIError("Hanall research exceeded overall deadline")
            response = await client.chat.completions.create(
                model=self._settings.kimi_model,
                messages=messages,
                tools=tools or None,
                timeout=self._settings.kimi_tool_timeout_seconds,
            )
            raw_response = response.model_dump(mode="json")
            choice = response.choices[0]
            assistant_message = choice.message
            messages.append(self._assistant_message_to_context(assistant_message))
            if choice.finish_reason != "tool_calls" or not assistant_message.tool_calls:
                final_text = assistant_message.content or ""
                break
            messages.extend(await self._resolve_tool_calls(assistant_message.tool_calls))
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
        arguments = json.loads(tool_call.function.arguments or "{}")
        url = f"{self._settings.kimi_base_url.rstrip('/')}/formulas/{quote(formula_uri, safe='')}/fibers"
        response = await client.post(url, headers=headers, json=arguments)
        response.raise_for_status()
        payload = response.json()
        context = payload.get("context") or {}
        content = context.get("output") or context.get("encrypted_output")
        if content is None:
            raise ExternalAPIError(f"Formula fiber returned no content for {function_name}")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return {"role": "tool", "tool_call_id": tool_call.id, "content": content}

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
