from __future__ import annotations

from datetime import timedelta
from typing import Any
from xml.etree import ElementTree

import requests

from server.settings import get_settings
from server.utils import now_kst

KMA_VILLAGE_FORECAST_URL = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst"


def get_kma_base_datetime() -> tuple[str, str]:
    settings = get_settings()
    now = now_kst()
    current_hhmm = now.strftime("%H%M")
    selected = None
    for slot in settings.weather.base_slots:
        if slot <= current_hhmm:
            selected = slot
    if selected is None:
        previous_day = now.date() - timedelta(days=1)
        fallback_slot = settings.weather.base_slots[-1] if settings.weather.base_slots else "2300"
        return previous_day.strftime("%Y%m%d"), fallback_slot
    return now.strftime("%Y%m%d"), selected


def fetch_kma_forecast_items(base_date: str, base_time: str, nx: int, ny: int, api_key: str) -> list[dict[str, Any]]:
    response = requests.get(
        KMA_VILLAGE_FORECAST_URL,
        params={
            "authKey": api_key,
            "pageNo": 1,
            "numOfRows": 300,
            "dataType": "XML",
            "base_date": base_date,
            "base_time": base_time,
            "nx": nx,
            "ny": ny,
        },
        timeout=get_settings().weather.timeout_seconds,
    )
    response.raise_for_status()
    root = ElementTree.fromstring(response.text)
    result_code = (root.findtext(".//resultCode") or "").strip()
    result_msg = (root.findtext(".//resultMsg") or "").strip()
    if result_code and result_code != "00":
        raise ValueError(f"기상청 예보 오류 code={result_code} message={result_msg}")

    items: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        items.append(
            {
                "category": (item.findtext("category") or "").strip(),
                "fcstDate": (item.findtext("fcstDate") or "").strip(),
                "fcstTime": (item.findtext("fcstTime") or "").strip(),
                "fcstValue": (item.findtext("fcstValue") or "").strip(),
            }
        )
    if not items:
        raise ValueError("기상청 예보 응답이 비어 있습니다.")
    return items


def pick_forecast_value(
    items: list[dict[str, Any]],
    category: str,
    fcst_date: str,
    preferred_time: str | None = None,
) -> str | None:
    matched = [
        item
        for item in items
        if str(item.get("category")) == category and str(item.get("fcstDate")) == fcst_date
    ]
    if not matched:
        return None
    if preferred_time is not None:
        future = [item for item in matched if str(item.get("fcstTime", "")) >= preferred_time]
        if future:
            future.sort(key=lambda item: str(item.get("fcstTime", "")))
            return str(future[0].get("fcstValue", "")).strip() or None
    matched.sort(key=lambda item: str(item.get("fcstTime", "")))
    return str(matched[0].get("fcstValue", "")).strip() or None


def map_weather_summary(sky: str | None, pty: str | None) -> str:
    precipitation_map = {
        "1": "비",
        "2": "비 또는 눈",
        "3": "눈",
        "4": "소나기",
    }
    if pty and pty in precipitation_map:
        return precipitation_map[pty]
    sky_map = {
        "1": "맑음",
        "3": "구름많음",
        "4": "흐림",
    }
    return sky_map.get(str(sky or "").strip(), "정보 없음")
