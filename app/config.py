from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.constants import DEFAULT_KIMI_FORMULA_URIS, DEFAULT_KMA_BASE_SLOTS, DEFAULT_MESSAGE_CHUNK_LIMIT
from app.errors import ConfigurationError
from app.schemas import PromptLibrary, RoomRegistryConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_name: str = "kakao-bot-mvp"
    app_timezone: str = "Asia/Seoul"
    app_base_url: str = "http://127.0.0.1:8000"

    database_url: str = "sqlite:///./data/kakao_bot.db"
    sqlite_busy_retry_count: int = 3
    sqlite_busy_retry_delay_seconds: float = 0.15

    log_level: str = "INFO"

    inbound_bot_secret: str = Field("change-me", min_length=1)

    smartphone_host: str = "127.0.0.1"
    smartphone_socket_port: int = 9500
    messengerbot_bot_name: str = Field("change-me-bot", min_length=1)
    mb_socket_control_author_name: str = "__FASTAPI__"
    mb_socket_control_room_name: str = "__MB_SOCKET_CONTROL__"
    socket_shared_token: str = Field("change-me-too", min_length=1)
    socket_connect_timeout_seconds: int = 5
    socket_ack_timeout_seconds: int = 15
    socket_max_retries: int = 3
    socket_retry_backoff_seconds: float = 1.0

    message_chunk_limit: int = DEFAULT_MESSAGE_CHUNK_LIMIT
    admin_room_name: str = "김휘태"
    default_package_name: str = "com.kakao.talk"

    gemini_api_key: str | None = None
    gemini_chat_model: str = "gemini-2.5-flash"
    gemini_youtube_model: str = "gemini-2.5-flash"
    gemini_youtube_transcript_model: str = "gemini-2.5-flash"
    gemini_youtube_transcript_fallback_model: str | None = "gemini-2.5-flash-lite"
    gemini_youtube_transcript_fallback_delay_seconds: float = 1.0
    gemini_timeout_seconds: int = 60
    youtube_dynamic_routing_enabled: bool = False
    youtube_transcript_timeout_seconds: int = 3
    youtube_transcript_max_chars: int = 18000

    kimi_api_key: str | None = None
    kimi_base_url: str = "https://api.moonshot.ai/v1"
    kimi_model: str = "kimi-k2.5"
    kimi_formula_uris: str = ",".join(DEFAULT_KIMI_FORMULA_URIS)
    kimi_tool_timeout_seconds: int = 25
    kimi_completion_timeout_seconds: int = 240
    kimi_max_iterations: int = 5
    kimi_overall_deadline_seconds: int = 900
    hanall_max_web_search_rounds: int = 2
    hanall_collect_retry_delays_seconds: str = "60,180"
    opendart_api_key: str | None = None
    openfda_api_key: str | None = None
    data_go_kr_api_key: str | None = None
    ncbi_api_key: str | None = None
    opendart_base_url: str = "https://opendart.fss.or.kr/api"
    opendart_timeout_seconds: int = 15
    hanall_dart_stock_code: str = "009420"
    hanall_dart_corp_code: str | None = None
    clinicaltrials_api_base_url: str = "https://clinicaltrials.gov/api/v2"
    clinicaltrials_timeout_seconds: int = 15
    sec_submissions_base_url: str = "https://data.sec.gov/submissions"
    sec_timeout_seconds: int = 15
    sec_user_agent: str | None = None
    immunovant_sec_cik: str = "0001764013"

    weather_api_key: str | None = None
    weather_grid_x: int = 95
    weather_grid_y: int = 77
    kma_timeout_seconds: int = 10
    kma_base_slots: str = ",".join(DEFAULT_KMA_BASE_SLOTS)
    air_quality_api_url: str = "https://air-quality-api.open-meteo.com/v1/air-quality"
    air_quality_timeout_seconds: int = 10

    child_birth_date: str = "2024-09-26"

    room_config_path: Path = Path("config/rooms.yaml")
    room_config_reload_interval_seconds: int = 5
    prompt_config_path: Path = Path("config/prompts.yaml")
    hanall_spec_path: Path = Path("docs/hanall_monitoring_prompt.md")

    @cached_property
    def kimi_formula_uri_list(self) -> list[str]:
        return [item.strip() for item in self.kimi_formula_uris.split(",") if item.strip()]

    @cached_property
    def hanall_collect_retry_delay_list(self) -> list[float]:
        if not self.hanall_collect_retry_delays_seconds.strip():
            return []

        delays: list[float] = []
        for item in self.hanall_collect_retry_delays_seconds.split(","):
            normalized = item.strip()
            try:
                delay_seconds = float(normalized)
            except ValueError as exc:
                raise ConfigurationError("HANALL_COLLECT_RETRY_DELAYS_SECONDS must contain numeric seconds values") from exc
            if delay_seconds <= 0:
                raise ConfigurationError("HANALL_COLLECT_RETRY_DELAYS_SECONDS must contain positive values")
            delays.append(delay_seconds)
        return delays

    @cached_property
    def kma_base_slot_list(self) -> list[str]:
        slots = [item.strip() for item in self.kma_base_slots.split(",") if item.strip()]
        try:
            return sorted(slots, key=lambda item: int(item))
        except ValueError as exc:
            raise ConfigurationError("KMA_BASE_SLOTS must contain HHMM values") from exc

    def ensure_directory_structure(self) -> None:
        db_path = self.database_url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def validate_runtime_secrets(self) -> None:
        _validate_secret("INBOUND_BOT_SECRET", self.inbound_bot_secret, "change-me")
        _validate_secret("SOCKET_SHARED_TOKEN", self.socket_shared_token, "change-me-too")
        _validate_secret("MESSENGERBOT_BOT_NAME", self.messengerbot_bot_name, "change-me-bot")
        _validate_required("MB_SOCKET_CONTROL_AUTHOR_NAME", self.mb_socket_control_author_name)
        _validate_required("MB_SOCKET_CONTROL_ROOM_NAME", self.mb_socket_control_room_name)

    def load_rooms(self) -> RoomRegistryConfig:
        payload = _load_yaml_file(self.room_config_path)
        return RoomRegistryConfig.model_validate(payload)

    def load_prompts(self) -> PromptLibrary:
        payload = _load_yaml_file(self.prompt_config_path)
        return PromptLibrary.model_validate(payload)

    def load_hanall_spec(self) -> str:
        if not self.hanall_spec_path.exists():
            raise ConfigurationError(f"HanAll spec file not found: {self.hanall_spec_path}")
        content = self.hanall_spec_path.read_text(encoding="utf-8").strip()
        if not content:
            raise ConfigurationError(f"HanAll spec file must not be empty: {self.hanall_spec_path}")
        return content


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"Config file must contain a mapping: {path}")
    return loaded


def _validate_secret(env_name: str, value: str, placeholder: str) -> None:
    normalized = value.strip()
    if not normalized:
        raise ConfigurationError(f"{env_name} must not be empty")
    if normalized == placeholder:
        raise ConfigurationError(f"{env_name} must be replaced before startup")


def _validate_required(env_name: str, value: str) -> None:
    if not value.strip():
        raise ConfigurationError(f"{env_name} must not be empty")
