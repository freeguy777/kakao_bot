from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from server.config import get_llm_config, get_prompt
from server.infra.llm_clients import call_gemini_text, call_openai_text
from server.settings import get_settings
from server.utils import now_kst, safe_truncate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptExecutionOptions:
    feature_key: str
    model: str | None
    temperature: float | None
    tools: list[dict[str, Any]] | None
    text_format: dict[str, Any] | None
    max_output_tokens: int
    truncate_limit: int
    preserve_newlines: bool
    timeout: int
    poll_timeout: int
    background: bool


def _get_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return float(text)


def _get_optional_int(value: Any, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    return int(text)


def _get_prompt_text_format(prompt_key: str) -> dict[str, Any] | None:
    _ = prompt_key
    return None


def _is_gemini_model(model: str) -> bool:
    return model.strip().lower().startswith("gemini")


def _is_hanall_news_prompt_key(prompt_key: str) -> bool:
    return prompt_key in {
        "hanall_news_prompt",
        "hanall_news_collect_prompt",
        "hanall_news_finalize_prompt",
    }


def render_prompt_template(prompt_key: str, replacements: dict[str, str] | None = None) -> str:
    prompt = get_prompt(prompt_key)
    template = str(prompt.get("template", "")).strip()
    if not template:
        raise ValueError(f"prompt template missing: {prompt_key}")

    rendered = template
    for key, value in (replacements or {}).items():
        rendered = rendered.replace(key, value)
    return rendered


def _build_prompt_replacements(prompt_key: str) -> dict[str, str]:
    if prompt_key != "hanall_news_prompt":
        return {}
    from server.application.hanall_research import build_hanall_prompt_replacements

    return build_hanall_prompt_replacements(now_kst())


def _get_prompt_execution_options(prompt_key: str) -> PromptExecutionOptions:
    settings = get_settings()
    prompt = get_prompt(prompt_key)
    feature_key = str(prompt.get("feature_key", "")).strip() or "hanall_news_brief"
    tools = prompt.get("tools")
    if not isinstance(tools, list):
        tools = None

    default_max_output_tokens = settings.llm.prompt_default_max_output_tokens
    truncate_limit = 1500
    if _is_hanall_news_prompt_key(prompt_key):
        default_max_output_tokens = settings.llm.hanall_news_max_output_tokens
        truncate_limit = settings.llm.hanall_news_truncate_limit

    return PromptExecutionOptions(
        feature_key=feature_key,
        model=str(prompt.get("model", "")).strip() or None,
        temperature=_get_optional_float(prompt.get("temperature")),
        tools=tools,
        text_format=_get_prompt_text_format(prompt_key),
        max_output_tokens=_get_optional_int(prompt.get("max_output_tokens"), default_max_output_tokens),
        truncate_limit=_get_optional_int(prompt.get("truncate_limit"), truncate_limit),
        preserve_newlines=bool(prompt.get("preserve_newlines", True)),
        timeout=settings.llm.openai_timeout_seconds,
        poll_timeout=settings.llm.openai_poll_timeout_seconds,
        background=bool(prompt.get("background", False)),
    )


def _execute_prompt_by_key(
    prompt_key: str,
    *,
    replacements: dict[str, str] | None = None,
) -> tuple[str, PromptExecutionOptions]:
    settings = get_settings()
    prompt = get_prompt(prompt_key)
    title = prompt.get("title", prompt_key)
    merged_replacements = _build_prompt_replacements(prompt_key)
    if replacements:
        merged_replacements.update(replacements)
    body = render_prompt_template(prompt_key, replacements=merged_replacements)
    execution_options = _get_prompt_execution_options(prompt_key)
    llm_config = get_llm_config(execution_options.feature_key)
    if execution_options.model:
        llm_config["model"] = execution_options.model
    if execution_options.temperature is not None:
        llm_config["temperature"] = execution_options.temperature

    prompt_text = (
        "다음 지시에 따라 한국어로 간결하게 작성해라.\n"
        f"title: {title}\n"
        f"instruction:\n{body}"
    )
    logger.info(
        "prompt execution started prompt_key=%s feature=%s model=%s max_output_tokens=%s truncate_limit=%s background=%s",
        prompt_key,
        execution_options.feature_key,
        llm_config["model"],
        execution_options.max_output_tokens,
        execution_options.truncate_limit,
        execution_options.background,
    )
    if _is_gemini_model(llm_config["model"]):
        if execution_options.text_format:
            logger.warning(
                "gemini prompt execution ignores text_format prompt_key=%s feature=%s",
                prompt_key,
                execution_options.feature_key,
            )
        if execution_options.background:
            logger.warning(
                "gemini prompt execution ignores background prompt_key=%s feature=%s",
                prompt_key,
                execution_options.feature_key,
            )
        result_text = call_gemini_text(
            feature_key=execution_options.feature_key,
            prompt_text=prompt_text,
            max_output_tokens=execution_options.max_output_tokens,
            preserve_newlines=execution_options.preserve_newlines,
            model=llm_config["model"],
            temperature=llm_config["temperature"],
            tools=execution_options.tools,
            timeout=settings.llm.gemini_timeout_seconds,
        )
    else:
        result_text = call_openai_text(
            feature_key=execution_options.feature_key,
            prompt_text=prompt_text,
            max_output_tokens=execution_options.max_output_tokens,
            model=llm_config["model"],
            temperature=llm_config["temperature"],
            tools=execution_options.tools,
            text_format=execution_options.text_format,
            preserve_newlines=execution_options.preserve_newlines,
            timeout=execution_options.timeout,
            background=execution_options.background,
            poll_timeout=execution_options.poll_timeout,
        )
    logger.info("prompt execution completed prompt_key=%s output_chars=%s", prompt_key, len(result_text))
    return result_text, execution_options


def run_prompt_by_key(prompt_key: str, *, replacements: dict[str, str] | None = None) -> str:
    result_text, execution_options = _execute_prompt_by_key(prompt_key, replacements=replacements)
    return safe_truncate(result_text, execution_options.truncate_limit)


def run_prompt_by_key_raw(prompt_key: str, *, replacements: dict[str, str] | None = None) -> str:
    result_text, _ = _execute_prompt_by_key(prompt_key, replacements=replacements)
    return result_text
