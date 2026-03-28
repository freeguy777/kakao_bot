from __future__ import annotations

import calendar
import logging
from datetime import datetime
from typing import Any

from server.application.weather import get_room_weather_snapshot
from server.config import get_room_policy
from server.settings import get_settings
from server.utils import now_kst

logger = logging.getLogger(__name__)


def calculate_child_age_text(birth_date_str: str, include_days: bool = False) -> str:
    birth_date = datetime.strptime(birth_date_str, "%Y-%m-%d").date()
    today = now_kst().date()
    months = (today.year - birth_date.year) * 12 + (today.month - birth_date.month)
    if today.day < birth_date.day:
        months -= 1
    if include_days:
        days = (today - birth_date).days
        return f"오늘 기준 {months}개월 ({days}일)"
    return f"오늘 기준 {months}개월"


def calculate_child_age_details(birth_date_str: str) -> tuple[int, int, int]:
    birth_date = datetime.strptime(birth_date_str, "%Y-%m-%d").date()
    today = now_kst().date()
    total_days = (today - birth_date).days
    months = (today.year - birth_date.year) * 12 + (today.month - birth_date.month)
    if today.day < birth_date.day:
        months -= 1

    anchor_year = birth_date.year + (birth_date.month - 1 + months) // 12
    anchor_month = (birth_date.month - 1 + months) % 12 + 1
    anchor_day = min(birth_date.day, calendar.monthrange(anchor_year, anchor_month)[1])
    month_anchor = birth_date.replace(year=anchor_year, month=anchor_month, day=anchor_day)
    extra_days = (today - month_anchor).days
    return total_days, months, max(0, extra_days)


def build_family_one_line_memo(
    *,
    weather_summary: str | None = None,
    rain_chance: int | None = None,
    air_quality: str | None = None,
    months: int | None = None,
) -> str:
    normalized_weather = str(weather_summary or "")
    normalized_rain = int(rain_chance) if rain_chance is not None else 0

    if normalized_rain >= 50:
        return "외출 전 우산만 챙기면 한결 편한 하루예요."
    if months is not None and months < 12:
        return "오늘도 아기 리듬에 맞춰 천천히 보내면 충분해요."
    if "맑음" in normalized_weather:
        return "산책하기 무난한 날씨라 가볍게 바깥 공기 쐬기 좋아요."
    if months is None and not normalized_weather:
        return "오늘 해야 할 핵심만 가볍게 챙기면 충분해요."
    return "무리 없이 편안한 하루 흐름만 챙기면 충분해요."


def _has_explicit_child_config(raw_room: dict[str, Any] | None) -> bool:
    return isinstance(raw_room, dict) and isinstance(raw_room.get("child"), dict)


def _resolve_child_context(room_key: str | None) -> tuple[str | None, bool]:
    settings = get_settings()
    room = get_room_policy(room_key) if room_key else None
    if room is None:
        return settings.son_birth_date, True
    if not room.child.enabled:
        return None, True
    if not _has_explicit_child_config(room.raw):
        return None, True
    return room.child.birth_date or settings.son_birth_date, room.child.include_days


def _resolve_weather_label(room_key: str | None) -> str | None:
    settings = get_settings()
    room = get_room_policy(room_key) if room_key else None
    if room and not room.weather.enabled:
        return None
    if room and room.weather.label and room.weather.label != "기본 지역":
        return room.weather.label
    grid_x = room.weather.grid_x if room and room.weather.grid_x is not None else settings.weather.default_grid_x
    grid_y = room.weather.grid_y if room and room.weather.grid_y is not None else settings.weather.default_grid_y
    return f"기상청 격자 {grid_x},{grid_y}"


def build_family_morning_brief(room_key: str | None = None) -> str:
    birth_date, include_days = _resolve_child_context(room_key)
    weather_label = _resolve_weather_label(room_key)
    lines: list[str] = []

    months: int | None = None
    if birth_date:
        total_days, months, extra_days = calculate_child_age_details(birth_date)
        age_line = (
            f"👶 아들 태어난 지 D+{total_days} ({months}개월 {extra_days}일)"
            if include_days
            else f"👶 아들 {calculate_child_age_text(birth_date, include_days=False)}"
        )
        lines.append(age_line)

    weather: dict[str, Any] | None = None
    if weather_label:
        try:
            weather = get_room_weather_snapshot(room_key)
        except Exception as exc:
            logger.warning("family morning weather fallback room_key=%s error=%s", room_key, exc)
            weather = {
                "weather_summary": "정보 없음",
                "min_temp": "정보 없음",
                "max_temp": "정보 없음",
                "rain_chance": "정보 없음",
                "air_quality": "정보 없음",
            }

        min_temp_text = f"{weather['min_temp']}°" if isinstance(weather["min_temp"], int) else str(weather["min_temp"])
        max_temp_text = f"{weather['max_temp']}°" if isinstance(weather["max_temp"], int) else str(weather["max_temp"])
        rain_chance_text = f"{weather['rain_chance']}%" if isinstance(weather["rain_chance"], int) else str(weather["rain_chance"])
        lines.extend(
            [
                f"🌤 오늘 날씨[{weather_label}]: {weather['weather_summary']}",
                f"🌡 기온: {min_temp_text} / {max_temp_text}",
                f"☔ 강수: {rain_chance_text}",
            ]
        )

    one_line_memo = build_family_one_line_memo(
        weather_summary=str(weather["weather_summary"]) if weather else None,
        rain_chance=int(weather["rain_chance"]) if weather and str(weather["rain_chance"]).isdigit() else None,
        air_quality=str(weather["air_quality"]) if weather else None,
        months=months,
    )
    lines.append(f"📝 한 줄 메모: {one_line_memo}")
    return "\n".join(lines)
