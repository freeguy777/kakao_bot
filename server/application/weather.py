from __future__ import annotations

import logging
from typing import Any

from server.config import get_room_policy
from server.infra.weather_api import (
    fetch_kma_forecast_items,
    get_kma_base_datetime,
    map_weather_summary,
    pick_forecast_value,
)
from server.settings import get_settings
from server.utils import now_kst

logger = logging.getLogger(__name__)


def _to_number_or_text(value: str | None) -> str | int:
    if value is None:
        return "정보 없음"
    normalized = str(value).strip()
    if normalized.replace(".", "", 1).isdigit():
        return int(float(normalized))
    return normalized or "정보 없음"


def _pick_daily_extreme(items: list[dict[str, Any]], fallback_items: list[dict[str, Any]], category: str, fcst_date: str) -> str | None:
    return pick_forecast_value(items, category, fcst_date) or pick_forecast_value(fallback_items, category, fcst_date)


def get_room_weather_snapshot(room_key: str | None = None) -> dict[str, str | int]:
    settings = get_settings()
    api_key = settings.weather.api_key.strip()
    if not api_key or api_key == "replace_me":
        raise ValueError("WEATHER_API_KEY가 설정되지 않았습니다.")

    room = get_room_policy(room_key) if room_key else None
    grid_x = room.weather.grid_x if room and room.weather.grid_x is not None else settings.weather.default_grid_x
    grid_y = room.weather.grid_y if room and room.weather.grid_y is not None else settings.weather.default_grid_y

    current_now = now_kst()
    base_date, base_time = get_kma_base_datetime()
    today = current_now.strftime("%Y%m%d")
    current_time = current_now.strftime("%H00")

    items: list[dict[str, Any]] = []
    min_items: list[dict[str, Any]] = []
    fetch_errors: list[str] = []

    try:
        items = fetch_kma_forecast_items(
            base_date=base_date,
            base_time=base_time,
            nx=grid_x,
            ny=grid_y,
            api_key=api_key,
        )
    except Exception as exc:
        fetch_errors.append(f"current({base_date} {base_time})={exc}")
        logger.warning(
            "weather current forecast fetch failed room_key=%s base_date=%s base_time=%s nx=%s ny=%s error=%s",
            room_key,
            base_date,
            base_time,
            grid_x,
            grid_y,
            exc,
        )

    try:
        min_items = fetch_kma_forecast_items(
            base_date=today,
            base_time="0200",
            nx=grid_x,
            ny=grid_y,
            api_key=api_key,
        )
    except Exception as exc:
        fetch_errors.append(f"daily({today} 0200)={exc}")
        logger.warning(
            "weather daily forecast fetch failed room_key=%s base_date=%s base_time=0200 nx=%s ny=%s error=%s",
            room_key,
            today,
            grid_x,
            grid_y,
            exc,
        )

    if not items and not min_items:
        raise RuntimeError("기상청 예보 조회 실패: " + "; ".join(fetch_errors))

    weather_summary = map_weather_summary(
        pick_forecast_value(items, "SKY", today, current_time),
        pick_forecast_value(items, "PTY", today, current_time),
    )
    min_temp = _pick_daily_extreme(min_items, items, "TMN", today)
    max_temp = _pick_daily_extreme(min_items, items, "TMX", today)
    rain_chance = pick_forecast_value(items, "POP", today, current_time) or pick_forecast_value(min_items, "POP", today) or None

    return {
        "weather_summary": weather_summary,
        "min_temp": _to_number_or_text(min_temp),
        "max_temp": _to_number_or_text(max_temp),
        "rain_chance": _to_number_or_text(rain_chance),
        "air_quality": "정보 없음",
    }
