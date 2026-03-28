from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

from server.settings import (
    AppSettings,
    PROMPTS_PATH,
    ROOMS_PATH,
    get_api_base_path,
    get_api_base_url,
    get_settings,
    get_timezone,
    load_settings,
    reload_settings as reload_app_settings,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoomFeatureFlags:
    youtube_summary: bool = False
    news_brief: bool = False
    morning_brief: bool = False
    weather_brief: bool = False
    child_age: bool = False
    server_push: bool = True


@dataclass(frozen=True)
class DeliveryPolicy:
    admin_room_key: str | None = None
    notify_admin_on_error: bool = True
    admin_alert_throttle_seconds: int = 300
    message_length_limit: int = 3000
    dedupe_ttl_seconds: int = 600


@dataclass(frozen=True)
class WeatherPolicy:
    enabled: bool = False
    label: str = "기본 지역"
    grid_x: int | None = None
    grid_y: int | None = None


@dataclass(frozen=True)
class ChildPolicy:
    enabled: bool = False
    birth_date: str | None = None
    include_days: bool = True


@dataclass(frozen=True)
class NewsPolicy:
    enabled: bool = False
    prompt_key: str | None = None
    max_items: int = 5


@dataclass(frozen=True)
class RoomScheduledJob:
    name: str
    builder: str
    triggers: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RoomScheduleConfig:
    enabled: bool = False
    jobs: tuple[RoomScheduledJob, ...] = ()


@dataclass(frozen=True)
class RoomConfig:
    room_key: str
    display_name: str
    channel_id: str
    features: RoomFeatureFlags
    schedules: RoomScheduleConfig
    delivery: DeliveryPolicy
    weather: WeatherPolicy
    child: ChildPolicy
    news: NewsPolicy
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["features"] = asdict(self.features)
        payload["schedules"] = {
            "enabled": self.schedules.enabled,
            "jobs": [
                {
                    "name": job.name,
                    "builder": job.builder,
                    "triggers": [dict(trigger) for trigger in job.triggers],
                }
                for job in self.schedules.jobs
            ],
        }
        payload["delivery"] = asdict(self.delivery)
        payload["weather"] = asdict(self.weather)
        payload["child"] = asdict(self.child)
        payload["news"] = asdict(self.news)
        return payload


@dataclass(frozen=True)
class RoomsConfig:
    admin_room_key: str | None
    defaults: DeliveryPolicy
    scheduled_jobs: dict[str, Any]
    rooms: dict[str, RoomConfig]
    raw: dict[str, Any] = field(default_factory=dict)


_ROOMS_CONFIG: RoomsConfig | None = None
_PROMPTS_CONFIG: dict[str, Any] | None = None


def _load_yaml(path: Any) -> dict[str, Any]:
    if not path.exists():
        logger.warning("yaml file not found path=%s", path)
        return {}
    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"invalid yaml structure: {path}")
    return loaded


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_trigger_list(raw_triggers: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw_triggers, list):
        return ()
    return tuple(trigger for trigger in raw_triggers if isinstance(trigger, dict))


def _normalize_schedule_jobs(raw_jobs: Any, scheduled_jobs: dict[str, Any]) -> tuple[RoomScheduledJob, ...]:
    if not isinstance(raw_jobs, list):
        return ()

    normalized_jobs: list[RoomScheduledJob] = []
    for raw_job in raw_jobs:
        if isinstance(raw_job, str):
            job_name = raw_job.strip()
            if not job_name:
                continue
            template = scheduled_jobs.get(job_name, {}) if isinstance(scheduled_jobs.get(job_name), dict) else {}
            builder = str(template.get("builder", job_name)).strip() or job_name
            normalized_jobs.append(
                RoomScheduledJob(
                    name=job_name,
                    builder=builder,
                    triggers=_normalize_trigger_list(template.get("triggers")),
                )
            )
            continue

        if not isinstance(raw_job, dict):
            continue

        job_name = str(raw_job.get("name", raw_job.get("job_name", ""))).strip()
        if not job_name:
            continue

        template = scheduled_jobs.get(job_name, {}) if isinstance(scheduled_jobs.get(job_name), dict) else {}
        builder = str(raw_job.get("builder", template.get("builder", job_name))).strip() or job_name
        triggers = _normalize_trigger_list(raw_job.get("triggers", template.get("triggers")))
        normalized_jobs.append(
            RoomScheduledJob(
                name=job_name,
                builder=builder,
                triggers=triggers,
            )
        )
    return tuple(normalized_jobs)


def _normalize_room_policy(
    room_key: str,
    raw_room: dict[str, Any],
    defaults: DeliveryPolicy,
    scheduled_jobs: dict[str, Any],
) -> RoomConfig:
    raw_features = raw_room.get("features", {}) if isinstance(raw_room.get("features"), dict) else {}
    raw_delivery = raw_room.get("delivery", {}) if isinstance(raw_room.get("delivery"), dict) else {}
    raw_schedules = raw_room.get("schedules", {}) if isinstance(raw_room.get("schedules"), dict) else {}
    raw_weather = raw_room.get("weather", {}) if isinstance(raw_room.get("weather"), dict) else {}
    raw_child = raw_room.get("child", {}) if isinstance(raw_room.get("child"), dict) else {}
    raw_news = raw_room.get("news", {}) if isinstance(raw_room.get("news"), dict) else {}

    features = RoomFeatureFlags(
        youtube_summary=_as_bool(raw_features.get("youtube_summary"), False),
        news_brief=_as_bool(raw_features.get("news_brief"), False),
        morning_brief=_as_bool(raw_features.get("morning_brief"), False),
        weather_brief=_as_bool(raw_features.get("weather_brief"), _as_bool(raw_features.get("morning_brief"), False)),
        child_age=_as_bool(raw_features.get("child_age"), _as_bool(raw_features.get("morning_brief"), False)),
        server_push=_as_bool(raw_features.get("server_push"), True),
    )
    delivery = DeliveryPolicy(
        admin_room_key=str(raw_delivery.get("admin_room_key", "")).strip() or defaults.admin_room_key,
        notify_admin_on_error=_as_bool(raw_delivery.get("notify_admin_on_error"), defaults.notify_admin_on_error),
        admin_alert_throttle_seconds=_as_int(
            raw_delivery.get("admin_alert_throttle_seconds"),
            defaults.admin_alert_throttle_seconds,
        ),
        message_length_limit=_as_int(raw_delivery.get("message_length_limit"), defaults.message_length_limit),
        dedupe_ttl_seconds=_as_int(raw_delivery.get("dedupe_ttl_seconds"), defaults.dedupe_ttl_seconds),
    )
    schedules = RoomScheduleConfig(
        enabled=_as_bool(raw_schedules.get("enabled"), False),
        jobs=_normalize_schedule_jobs(raw_schedules.get("jobs", []), scheduled_jobs),
    )
    weather = WeatherPolicy(
        enabled=_as_bool(raw_weather.get("enabled"), features.weather_brief),
        label=str(raw_weather.get("label", "기본 지역")).strip() or "기본 지역",
        grid_x=_as_int(raw_weather.get("grid_x"), 0) or None,
        grid_y=_as_int(raw_weather.get("grid_y"), 0) or None,
    )
    child = ChildPolicy(
        enabled=_as_bool(raw_child.get("enabled"), features.child_age),
        birth_date=str(raw_child.get("birth_date", "")).strip() or None,
        include_days=_as_bool(raw_child.get("include_days"), True),
    )
    news = NewsPolicy(
        enabled=_as_bool(raw_news.get("enabled"), features.news_brief),
        prompt_key=str(raw_news.get("prompt_key", "")).strip() or None,
        max_items=_as_int(raw_news.get("max_items"), 5),
    )
    return RoomConfig(
        room_key=room_key,
        display_name=str(raw_room.get("display_name", "")).strip(),
        channel_id=str(raw_room.get("channel_id", "")).strip(),
        features=features,
        schedules=schedules,
        delivery=delivery,
        weather=weather,
        child=child,
        news=news,
        raw=raw_room,
    )


def _load_rooms_config() -> RoomsConfig:
    raw_config = _load_yaml(ROOMS_PATH)
    defaults_raw = raw_config.get("defaults", {}) if isinstance(raw_config.get("defaults"), dict) else {}
    delivery_defaults_raw = defaults_raw.get("delivery", {}) if isinstance(defaults_raw.get("delivery"), dict) else defaults_raw
    admin_room_key = str(raw_config.get("admin_room_key", "")).strip() or None
    defaults = DeliveryPolicy(
        admin_room_key=admin_room_key,
        notify_admin_on_error=_as_bool(delivery_defaults_raw.get("notify_admin_on_error"), True),
        admin_alert_throttle_seconds=_as_int(delivery_defaults_raw.get("admin_alert_throttle_seconds"), 300),
        message_length_limit=_as_int(delivery_defaults_raw.get("message_length_limit"), 3000),
        dedupe_ttl_seconds=_as_int(delivery_defaults_raw.get("dedupe_ttl_seconds"), 600),
    )

    raw_rooms = raw_config.get("rooms", {})
    if not isinstance(raw_rooms, dict):
        raise ValueError("rooms config is invalid")

    scheduled_jobs = raw_config.get("scheduled_jobs", {})
    if not isinstance(scheduled_jobs, dict):
        scheduled_jobs = {}

    rooms: dict[str, RoomConfig] = {}
    for room_key, raw_room in raw_rooms.items():
        if not isinstance(raw_room, dict):
            logger.warning("room config is invalid room_key=%s", room_key)
            continue
        normalized_key = str(room_key).strip()
        if not normalized_key:
            continue
        rooms[normalized_key] = _normalize_room_policy(normalized_key, raw_room, defaults, scheduled_jobs)

    logger.info("rooms config loaded room_count=%s admin_room_key=%s", len(rooms), admin_room_key)
    return RoomsConfig(
        admin_room_key=admin_room_key,
        defaults=defaults,
        scheduled_jobs=scheduled_jobs,
        rooms=rooms,
        raw=raw_config,
    )


def _load_prompts_config() -> dict[str, Any]:
    raw_prompts = _load_yaml(PROMPTS_PATH)
    prompts = raw_prompts.get("prompts", {})
    if not isinstance(prompts, dict):
        raise ValueError("prompts config is invalid")
    logger.info("prompts config loaded prompt_count=%s", len(prompts))
    return prompts


def get_rooms_registry() -> RoomsConfig:
    global _ROOMS_CONFIG
    if _ROOMS_CONFIG is None:
        _ROOMS_CONFIG = _load_rooms_config()
    return _ROOMS_CONFIG


def get_prompts_registry() -> dict[str, Any]:
    global _PROMPTS_CONFIG
    if _PROMPTS_CONFIG is None:
        _PROMPTS_CONFIG = _load_prompts_config()
    return _PROMPTS_CONFIG


def reload_runtime_config() -> None:
    global _ROOMS_CONFIG, _PROMPTS_CONFIG
    _ROOMS_CONFIG = _load_rooms_config()
    _PROMPTS_CONFIG = _load_prompts_config()


def load_settings() -> AppSettings:
    return get_settings()


def reload_settings() -> AppSettings:
    settings = reload_app_settings()
    reload_runtime_config()
    return settings


def get_rooms_config() -> dict[str, Any]:
    return {room_key: room.to_dict() for room_key, room in get_rooms_registry().rooms.items()}


def get_room_policy(room_key: str) -> RoomConfig | None:
    normalized_key = str(room_key).strip()
    if not normalized_key:
        return None
    return get_rooms_registry().rooms.get(normalized_key)


def get_room_config(room_key: str) -> dict[str, Any]:
    room = get_room_policy(room_key)
    return room.to_dict() if room else {}


def get_admin_room_key(room_key: str | None = None) -> str | None:
    if room_key:
        room = get_room_policy(room_key)
        if room and room.delivery.admin_room_key:
            return room.delivery.admin_room_key
    return get_rooms_registry().admin_room_key


def get_scheduled_jobs_config() -> dict[str, Any]:
    return dict(get_rooms_registry().scheduled_jobs)


def get_prompts_config() -> dict[str, Any]:
    return dict(get_prompts_registry())


def find_room_by_channel(channel_id: str | None) -> tuple[str | None, dict[str, Any] | None]:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None, None
    for room_key, room in get_rooms_registry().rooms.items():
        if normalized_channel_id == room.channel_id:
            return room_key, room.to_dict()
    return None, None


def find_room_policy_by_channel(channel_id: str | None) -> RoomConfig | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    for room in get_rooms_registry().rooms.values():
        if room.channel_id == normalized_channel_id:
            return room
    return None


def find_room_by_name(room_name: str | None) -> tuple[str | None, dict[str, Any] | None]:
    normalized_room_name = str(room_name or "").strip()
    if not normalized_room_name:
        return None, None
    for room_key, room in get_rooms_registry().rooms.items():
        if normalized_room_name == room.display_name:
            return room_key, room.to_dict()
    return None, None


def resolve_room(room_name: str | None, channel_id: str | None) -> tuple[str | None, dict[str, Any] | None]:
    room = find_room_policy_by_channel(channel_id)
    if room is not None:
        return room.room_key, room.to_dict()
    return find_room_by_name(room_name)


def resolve_room_policy(room_name: str | None, channel_id: str | None) -> RoomConfig | None:
    room = find_room_policy_by_channel(channel_id)
    if room is not None:
        return room
    normalized_room_name = str(room_name or "").strip()
    if not normalized_room_name:
        return None
    for candidate in get_rooms_registry().rooms.values():
        if candidate.display_name == normalized_room_name:
            return candidate
    return None


def get_prompt(prompt_key: str) -> dict[str, Any]:
    prompt = get_prompts_registry().get(prompt_key, {})
    return prompt if isinstance(prompt, dict) else {}


def get_llm_config(feature_key: str) -> dict[str, Any]:
    settings = get_settings()
    llm_map = {
        "youtube_summary": {
            "model": settings.llm.model_youtube_summary,
            "temperature": settings.llm.temperature_youtube_summary,
        },
        "hanall_news_brief": {
            "model": settings.llm.model_hanall_news_brief,
            "temperature": settings.llm.temperature_hanall_news_brief,
        },
        "family_morning_brief": {
            "model": settings.llm.model_family_morning_brief,
            "temperature": settings.llm.temperature_family_morning_brief,
        },
        "test_prompt": {
            "model": settings.llm.model_test_prompt,
            "temperature": settings.llm.temperature_test_prompt,
        },
    }
    selected = llm_map.get(
        feature_key,
        {
            "model": settings.llm.model_default,
            "temperature": settings.llm.temperature_default,
        },
    )
    return {
        "model": str(selected["model"]).strip(),
        "temperature": float(selected["temperature"]),
    }
