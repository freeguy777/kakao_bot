from __future__ import annotations

import logging
from time import perf_counter, sleep
from typing import Any

import requests

from server.config import get_llm_config
from server.settings import get_settings

logger = logging.getLogger(__name__)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
GEMINI_GENERATE_CONTENT_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _supports_temperature(model: str) -> bool:
    normalized = model.strip().lower()
    return not normalized.startswith("gpt-5")


def _extract_response_text(data: dict[str, Any], preserve_newlines: bool = False) -> str:
    output_text = str(data.get("output_text", "")).strip()
    if output_text:
        if preserve_newlines:
            return output_text.strip()
        return " ".join(output_text.split())

    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            content_type = str(content.get("type", "")).strip()
            if content_type not in {"output_text", "text", ""}:
                continue
            text = str(content.get("text", "")).strip()
            if text:
                chunks.append(text)
    if preserve_newlines:
        return "\n\n".join(chunks).strip()
    return " ".join(" ".join(chunks).split())


def _poll_openai_response(
    *,
    api_key: str,
    response_id: str,
    poll_timeout: int,
    preserve_newlines: bool,
) -> dict[str, Any]:
    deadline = perf_counter() + max(10, poll_timeout)
    last_data: dict[str, Any] | None = None
    while perf_counter() < deadline:
        response = requests.get(
            f"{OPENAI_RESPONSES_URL}/{response_id}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=get_settings().llm.openai_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        last_data = data
        status = str(data.get("status", "")).strip()
        logger.info("openai response polled response_id=%s status=%s", response_id, status or "unknown")
        if status in {"completed", "failed", "cancelled", "incomplete"}:
            text = _extract_response_text(data, preserve_newlines=preserve_newlines)
            if status == "completed" or text:
                return data
            return data
        sleep(5)

    final_status = str((last_data or {}).get("status", "")).strip()
    raise TimeoutError(f"background 응답 polling timeout status={final_status}")


def call_openai_text(
    *,
    feature_key: str,
    prompt_text: str,
    max_output_tokens: int,
    model: str | None = None,
    temperature: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    text_format: dict[str, Any] | None = None,
    preserve_newlines: bool = False,
    timeout: int = 30,
    background: bool = False,
    poll_timeout: int = 600,
) -> str:
    settings = get_settings()
    api_key = settings.openai_api_key.strip()
    if not api_key or api_key == "replace_me":
        raise ValueError("OPENAI_API_KEY가 설정되지 않았습니다.")

    llm_config = get_llm_config(feature_key)
    selected_model = (model or llm_config["model"]).strip()
    selected_temperature = llm_config["temperature"] if temperature is None else float(temperature)
    text_verbosity = "medium" if "deep-research" in selected_model else "low"
    token_budgets = [
        max_output_tokens,
        max(max_output_tokens + 120, int(max_output_tokens * 1.5)),
        max_output_tokens * 2,
    ]
    last_data: dict[str, Any] | None = None
    for token_budget in token_budgets:
        payload: dict[str, Any] = {
            "model": selected_model,
            "input": prompt_text,
            "max_output_tokens": token_budget,
            "text": {"verbosity": text_verbosity},
        }
        if text_format:
            payload["text"]["format"] = text_format
        if background:
            payload["background"] = True
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        else:
            payload["reasoning"] = {"effort": "low"}
            if _supports_temperature(selected_model):
                payload["temperature"] = selected_temperature

        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        logger.info(
            "openai response accepted feature=%s model=%s background=%s token_budget=%s response_id=%s status=%s",
            feature_key,
            selected_model,
            background,
            token_budget,
            str(data.get("id", "")).strip(),
            str(data.get("status", "")).strip() or "unknown",
        )
        if background:
            response_id = str(data.get("id", "")).strip()
            if not response_id:
                raise ValueError("background 응답에서 response id를 찾지 못했습니다.")
            data = _poll_openai_response(
                api_key=api_key,
                response_id=response_id,
                poll_timeout=poll_timeout,
                preserve_newlines=preserve_newlines,
            )
        last_data = data
        status = str(data.get("status", "")).strip()
        text = _extract_response_text(data, preserve_newlines=preserve_newlines)
        if text and (status == "completed" or not text_format):
            return text
        if text_format and status and status != "completed":
            logger.warning(
                "openai structured response incomplete feature=%s model=%s token_budget=%s status=%s",
                feature_key,
                selected_model,
                token_budget,
                status,
            )

    status = str((last_data or {}).get("status", "")).strip()
    raise ValueError(f"OpenAI 응답에서 텍스트를 찾지 못했습니다. status={status}")


def _extract_gemini_text(data: dict[str, Any], preserve_newlines: bool = False) -> str:
    chunks: list[str] = []
    for candidate in data.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            text = str(part.get("text", "")).strip()
            if text:
                chunks.append(text)
    if preserve_newlines:
        return "\n\n".join(chunks).strip()
    return " ".join(" ".join(chunks).split())


def _build_gemini_generation_config(*, model: str, temperature: float, max_output_tokens: int) -> dict[str, Any]:
    config: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }

    normalized_model = model.strip().lower()
    if normalized_model.startswith("gemini-2.5"):
        # Gemini 2.5 uses thinkingBudget, not thinkingLevel.
        config["thinkingConfig"] = {"thinkingBudget": 0}
    elif normalized_model.startswith("gemini-3"):
        config["thinkingConfig"] = {"thinkingLevel": "minimal"}

    return config


def call_gemini_text(
    *,
    feature_key: str,
    prompt_text: str,
    max_output_tokens: int,
    preserve_newlines: bool = False,
    model: str | None = None,
    temperature: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    timeout: int | None = None,
) -> str:
    return call_gemini_parts(
        feature_key=feature_key,
        parts=[{"text": prompt_text}],
        max_output_tokens=max_output_tokens,
        preserve_newlines=preserve_newlines,
        model=model,
        temperature=temperature,
        tools=tools,
        timeout=timeout if timeout is not None else get_settings().llm.gemini_timeout_seconds,
    )


def _normalize_gemini_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    tool_type = str(tool.get("type", "")).strip().lower()
    if "google_search" in tool and isinstance(tool.get("google_search"), dict):
        return {"google_search": dict(tool["google_search"])}
    if tool_type in {"google_search", "web_search_preview"}:
        return {"google_search": {}}
    if "url_context" in tool and isinstance(tool.get("url_context"), dict):
        return {"url_context": dict(tool["url_context"])}
    if tool_type == "url_context":
        return {"url_context": {}}
    return None


def call_gemini_parts(
    *,
    feature_key: str,
    parts: list[dict[str, Any]],
    max_output_tokens: int,
    preserve_newlines: bool = False,
    model: str | None = None,
    temperature: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    timeout: int = 30,
) -> str:
    settings = get_settings()
    api_key = str(getattr(settings, "gemini_api_key", getattr(settings, "google_api_key", ""))).strip()
    if not api_key or api_key == "replace_me":
        raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")

    llm_config = get_llm_config(feature_key)
    resolved_model = str(model or llm_config["model"]).strip()
    resolved_temperature = float(llm_config["temperature"] if temperature is None else temperature)
    normalized_tools = [
        normalized_tool
        for normalized_tool in (_normalize_gemini_tool(tool) for tool in (tools or []))
        if normalized_tool is not None
    ]
    payload: dict[str, Any] = {
        "contents": [{"parts": parts}],
        "generationConfig": _build_gemini_generation_config(
            model=resolved_model,
            temperature=resolved_temperature,
            max_output_tokens=max_output_tokens,
        ),
    }
    if normalized_tools:
        payload["tools"] = normalized_tools
    response = requests.post(
        GEMINI_GENERATE_CONTENT_URL.format(model=resolved_model),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    text = _extract_gemini_text(response.json(), preserve_newlines=preserve_newlines)
    if not text:
        raise ValueError("Gemini 응답에서 텍스트를 찾지 못했습니다.")
    return text
