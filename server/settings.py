from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SERVER_DIR = Path(__file__).resolve().parent
ENV_PATH = SERVER_DIR / ".env"
HANALL_SOURCES_PATH = SERVER_DIR / "hanall_sources.yaml"
HANALL_COMPETITORS_PATH = SERVER_DIR / "hanall_competitors.yaml"
ROOMS_PATH = SERVER_DIR / "rooms.yaml"
PROMPTS_PATH = SERVER_DIR / "prompts.yaml"


@dataclass(frozen=True)
class SocketSettings:
    enabled: bool
    host: str
    port: int
    shared_secret: str
    bot_name: str
    control_room_name: str
    control_author_name: str
    package_name: str
    connect_timeout_seconds: int
    read_timeout_seconds: int
    retry_count: int
    retry_delay_ms: int
    flush_batch_size: int
    flush_interval_seconds: int
    deprecated: bool = True


@dataclass(frozen=True)
class WeatherSettings:
    api_key: str
    default_grid_x: int
    default_grid_y: int
    timeout_seconds: int
    base_slots: tuple[str, ...]


@dataclass(frozen=True)
class NewsSettings:
    api_key: str


@dataclass(frozen=True)
class LLMSettings:
    model_default: str
    temperature_default: float
    model_youtube_summary: str
    temperature_youtube_summary: float
    youtube_summary_mode: str
    model_hanall_news_brief: str
    temperature_hanall_news_brief: float
    model_family_morning_brief: str
    temperature_family_morning_brief: float
    model_test_prompt: str
    temperature_test_prompt: float
    youtube_transcript_char_limit: int
    youtube_summary_char_limit: int
    youtube_summary_max_output_tokens: int
    prompt_default_max_output_tokens: int
    hanall_news_max_output_tokens: int
    hanall_news_truncate_limit: int
    openai_timeout_seconds: int
    openai_poll_timeout_seconds: int
    gemini_timeout_seconds: int


@dataclass(frozen=True)
class AppSettings:
    app_env: str
    timezone: str
    api_base_path: str
    api_base_url: str
    sqlite_path: str
    scheduler_recent_misfire_grace_seconds: int
    son_birth_date: str
    openai_api_key: str
    gemini_api_key: str
    google_api_key: str
    sec_api_key: str
    opendart_api_key: str
    openfda_api_key: str
    data_go_kr_api_key: str
    ncbi_api_key: str
    ncbi_tool_name: str
    ncbi_email: str
    socket: SocketSettings
    weather: WeatherSettings
    news: NewsSettings
    llm: LLMSettings


_SETTINGS: AppSettings | None = None


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("invalid int env name=%s value=%s default=%s", name, raw_value, default)
        return default


def _get_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return float(raw_value)
    except ValueError:
        logger.warning("invalid float env name=%s value=%s default=%s", name, raw_value, default)
        return default


def _get_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, str(default)).strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    logger.warning("invalid bool env name=%s value=%s default=%s", name, raw_value, default)
    return default


def load_settings() -> AppSettings:
    env_values = _parse_env_file(ENV_PATH)
    for key, value in env_values.items():
        os.environ.setdefault(key, value)

    socket_settings = SocketSettings(
        enabled=_get_bool_env("PHONE_SOCKET_ENABLED", False),
        host=os.getenv("PHONE_SOCKET_HOST", "192.168.0.12").strip(),
        port=_get_int_env("PHONE_SOCKET_PORT", 9510),
        shared_secret=os.getenv("PHONE_SOCKET_SHARED_SECRET", "change_me_socket_secret").strip(),
        bot_name=os.getenv("PHONE_SOCKET_BOT_NAME", "PpiraeSocketBridge").strip(),
        control_room_name=os.getenv("PHONE_SOCKET_CONTROL_ROOM_NAME", "__MB_SOCKET_CONTROL__").strip(),
        control_author_name=os.getenv("PHONE_SOCKET_CONTROL_AUTHOR_NAME", "socket_bridge").strip(),
        package_name=os.getenv("KAKAO_PACKAGE_NAME", os.getenv("PHONE_SOCKET_PACKAGE_NAME", "com.kakao.talk")).strip(),
        connect_timeout_seconds=_get_int_env("PHONE_SOCKET_CONNECT_TIMEOUT_SECONDS", 5),
        read_timeout_seconds=_get_int_env("PHONE_SOCKET_READ_TIMEOUT_SECONDS", 2),
        retry_count=_get_int_env("PHONE_SOCKET_RETRY_COUNT", 2),
        retry_delay_ms=_get_int_env("PHONE_SOCKET_RETRY_DELAY_MS", 1000),
        flush_batch_size=_get_int_env("PHONE_SOCKET_FLUSH_BATCH_SIZE", 20),
        flush_interval_seconds=_get_int_env("PHONE_SOCKET_FLUSH_INTERVAL_SECONDS", 15),
    )
    weather_settings = WeatherSettings(
        api_key=os.getenv("WEATHER_API_KEY", "replace_me").strip(),
        default_grid_x=_get_int_env("WEATHER_GRID_X", 60),
        default_grid_y=_get_int_env("WEATHER_GRID_Y", 127),
        timeout_seconds=_get_int_env("KMA_TIMEOUT_SECONDS", 30),
        base_slots=tuple(
            slot.strip()
            for slot in os.getenv("KMA_BASE_SLOTS", "0200,0500,0800,1100,1400,1700,2000,2300").split(",")
            if slot.strip()
        ),
    )
    llm_settings = LLMSettings(
        model_default=os.getenv("LLM_MODEL_DEFAULT", "gpt-5-mini").strip(),
        temperature_default=_get_float_env("LLM_TEMPERATURE_DEFAULT", 0.3),
        model_youtube_summary=os.getenv("LLM_MODEL_YOUTUBE_SUMMARY", "gemini-2.5-flash").strip(),
        temperature_youtube_summary=_get_float_env("LLM_TEMPERATURE_YOUTUBE_SUMMARY", 0.2),
        youtube_summary_mode=os.getenv("YOUTUBE_SUMMARY_MODE", "hybrid").strip(),
        model_hanall_news_brief=os.getenv(
            "LLM_MODEL_HANALL_NEWS_BRIEF",
            os.getenv("LLM_MODEL_STOCK_NEWS_BRIEF", "gemini-3-flash-preview"),
        ).strip(),
        temperature_hanall_news_brief=_get_float_env(
            "LLM_TEMPERATURE_HANALL_NEWS_BRIEF",
            _get_float_env("LLM_TEMPERATURE_STOCK_NEWS_BRIEF", 0.2),
        ),
        model_family_morning_brief=os.getenv("LLM_MODEL_FAMILY_MORNING_BRIEF", "gpt-5-mini").strip(),
        temperature_family_morning_brief=_get_float_env("LLM_TEMPERATURE_FAMILY_MORNING_BRIEF", 0.4),
        model_test_prompt=os.getenv("LLM_MODEL_TEST_PROMPT", "gpt-5-mini").strip(),
        temperature_test_prompt=_get_float_env("LLM_TEMPERATURE_TEST_PROMPT", 0.7),
        youtube_transcript_char_limit=_get_int_env("YOUTUBE_TRANSCRIPT_CHAR_LIMIT", 2000),
        youtube_summary_char_limit=_get_int_env("YOUTUBE_SUMMARY_CHAR_LIMIT", 1500),
        youtube_summary_max_output_tokens=_get_int_env("YOUTUBE_SUMMARY_MAX_OUTPUT_TOKENS", 700),
        prompt_default_max_output_tokens=_get_int_env("PROMPT_DEFAULT_MAX_OUTPUT_TOKENS", 800),
        hanall_news_max_output_tokens=_get_int_env("HANALL_NEWS_MAX_OUTPUT_TOKENS", 3200),
        hanall_news_truncate_limit=_get_int_env("HANALL_NEWS_TRUNCATE_LIMIT", 2400),
        openai_timeout_seconds=_get_int_env("OPENAI_TIMEOUT_SECONDS", 60),
        openai_poll_timeout_seconds=_get_int_env("OPENAI_POLL_TIMEOUT_SECONDS", 600),
        gemini_timeout_seconds=_get_int_env("GEMINI_TIMEOUT_SECONDS", 60),
    )
    settings = AppSettings(
        app_env=os.getenv("APP_ENV", "dev").strip(),
        timezone=os.getenv("APP_TIMEZONE", "Asia/Seoul").strip(),
        api_base_path=os.getenv("API_BASE_PATH", "/kakao").strip(),
        api_base_url=os.getenv("API_BASE_URL", "http://192.168.0.10:8000/kakao").strip(),
        sqlite_path=os.getenv("SQLITE_PATH", "./bot.db").strip(),
        scheduler_recent_misfire_grace_seconds=_get_int_env("SCHEDULER_RECENT_MISFIRE_GRACE_SECONDS", 900),
        son_birth_date=os.getenv("SON_BIRTH_DATE", "2024-09-26").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "replace_me").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "replace_me")).strip(),
        google_api_key=os.getenv("GOOGLE_API_KEY", "replace_me").strip(),
        sec_api_key=os.getenv("SEC_API_KEY", "replace_me").strip(),
        opendart_api_key=os.getenv("OPENDART_API_KEY", "replace_me").strip(),
        openfda_api_key=os.getenv("OPENFDA_API_KEY", "replace_me").strip(),
        data_go_kr_api_key=os.getenv("DATA_GO_KR_API_KEY", "replace_me").strip(),
        ncbi_api_key=os.getenv("NCBI_API_KEY", "replace_me").strip(),
        ncbi_tool_name=os.getenv("NCBI_TOOL_NAME", "").strip(),
        ncbi_email=os.getenv("NCBI_EMAIL", "").strip(),
        socket=socket_settings,
        weather=weather_settings,
        news=NewsSettings(api_key=os.getenv("NEWS_API_KEY", "replace_me").strip()),
        llm=llm_settings,
    )
    logger.info(
        "settings loaded env=%s timezone=%s api_base_url=%s active_transport=%s socket_deprecated=%s",
        settings.app_env,
        settings.timezone,
        settings.api_base_url,
        "polling_outbox",
        settings.socket.deprecated,
    )
    return settings


def get_settings() -> AppSettings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def reload_settings() -> AppSettings:
    global _SETTINGS
    _SETTINGS = load_settings()
    return _SETTINGS


def get_api_base_path() -> str:
    return get_settings().api_base_path


def get_api_base_url() -> str:
    return get_settings().api_base_url


def get_timezone() -> str:
    return get_settings().timezone
