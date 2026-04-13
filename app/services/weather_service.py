from __future__ import annotations

import logging
import math
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.errors import ConfigurationError, ExternalAPIError
from app.schemas import EffectiveRoomConfig, WeatherSnapshot

logger = logging.getLogger(__name__)


class WeatherService:
    BASE_URL = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst"
    KMA_GRID_EARTH_RADIUS_KM = 6371.00877
    KMA_GRID_SPACING_KM = 5.0
    KMA_GRID_STANDARD_LAT1 = 30.0
    KMA_GRID_STANDARD_LAT2 = 60.0
    KMA_GRID_ORIGIN_LON = 126.0
    KMA_GRID_ORIGIN_LAT = 38.0
    KMA_GRID_ORIGIN_X = 43.0
    KMA_GRID_ORIGIN_Y = 136.0
    AIR_QUALITY_SEVERITY = {"좋음": 0, "보통": 1, "나쁨": 2, "매우나쁨": 3}

    def __init__(self, *, settings: Settings, delivery_service: object, admin_notifier: object) -> None:
        self._settings = settings
        self._delivery_service = delivery_service
        self._admin_notifier = admin_notifier
        self._timezone = ZoneInfo(settings.app_timezone)

    async def handle_on_demand(self, room: EffectiveRoomConfig) -> None:
        try:
            snapshot = await self.fetch_today_weather(room)
            await self._delivery_service.send_text(room.name, snapshot.to_message(), package_name=room.package_name)
        except Exception as exc:  # noqa: BLE001
            await self._admin_notifier.notify_feature_error(room_name=room.name, feature_name="weather", error_message=str(exc))

    async def fetch_today_weather(self, room: EffectiveRoomConfig) -> WeatherSnapshot:
        if not self._settings.weather_api_key:
            raise ConfigurationError("WEATHER_API_KEY is not configured")
        grid_x = room.weather.grid_x or self._settings.weather_grid_x
        grid_y = room.weather.grid_y or self._settings.weather_grid_y
        label = room.weather.location_label or "기본 지역"
        items = await self._fetch_with_fallback(
            grid_x=grid_x,
            grid_y=grid_y,
            candidates=self._build_base_candidates(datetime.now(self._timezone)),
        )
        snapshot = self._parse_snapshot(items=items, label=label)
        min_temp = await self._fetch_daily_min_temp(grid_x=grid_x, grid_y=grid_y)
        if min_temp is not None:
            snapshot = snapshot.model_copy(update={"min_temp": f"{min_temp}°"})
        air_quality_fields = await self._fetch_air_quality_for_grid(grid_x=grid_x, grid_y=grid_y)
        if air_quality_fields:
            snapshot = snapshot.model_copy(update=air_quality_fields)
        return snapshot

    async def _fetch_with_fallback(
        self,
        *,
        grid_x: int,
        grid_y: int,
        candidates: list[tuple[str, str]],
    ) -> list[dict]:
        last_error: Exception | None = None
        errors: list[str] = []
        for base_date, base_time in candidates:
            try:
                return await self._fetch_forecast(base_date=base_date, base_time=base_time, grid_x=grid_x, grid_y=grid_y)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                errors.append(f"{base_date} {base_time}={exc}")
                logger.warning(
                    "weather_forecast_fetch_failed",
                    extra={
                        "base_date": base_date,
                        "base_time": base_time,
                        "grid_x": grid_x,
                        "grid_y": grid_y,
                        "error": str(exc),
                    },
                )
        if last_error:
            joined_errors = "; ".join(errors)
            raise ExternalAPIError(f"KMA forecast fetch failed: {joined_errors}") from last_error
        raise ExternalAPIError("No KMA base slots available")

    async def _fetch_forecast(self, *, base_date: str, base_time: str, grid_x: int, grid_y: int) -> list[dict]:
        params = {
            "authKey": self._settings.weather_api_key,
            "pageNo": 1,
            "numOfRows": 1000,
            "dataType": "JSON",
            "base_date": base_date,
            "base_time": base_time,
            "nx": grid_x,
            "ny": grid_y,
        }
        async with httpx.AsyncClient(timeout=self._settings.kma_timeout_seconds) as client:
            response = await client.get(self.BASE_URL, params=params)
            response.raise_for_status()
        payload = response.json()
        items = (((payload.get("response") or {}).get("body") or {}).get("items") or {}).get("item") or []
        if not items:
            raise ExternalAPIError("KMA returned no forecast items")
        return items

    async def _fetch_air_quality_for_grid(self, *, grid_x: int, grid_y: int) -> dict[str, str]:
        latitude, longitude = self._grid_to_latlon(grid_x=grid_x, grid_y=grid_y)
        try:
            pm10, pm2_5 = await self._fetch_air_quality(latitude=latitude, longitude=longitude)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "weather_air_quality_fetch_failed",
                extra={
                    "grid_x": grid_x,
                    "grid_y": grid_y,
                    "latitude": round(latitude, 6),
                    "longitude": round(longitude, 6),
                    "error": str(exc),
                },
            )
            return {}

        air_quality_grade = self._resolve_air_quality_grade(pm10=pm10, pm2_5=pm2_5)
        if air_quality_grade is None:
            return {}

        fields: dict[str, str] = {"air_quality_grade": air_quality_grade}
        if pm10 is not None:
            fields["pm10"] = self._format_air_quality_value(pm10)
        if pm2_5 is not None:
            fields["pm2_5"] = self._format_air_quality_value(pm2_5)
        return fields

    async def _fetch_air_quality(self, *, latitude: float, longitude: float) -> tuple[float | None, float | None]:
        params = {
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "hourly": "pm10,pm2_5",
            "timezone": self._settings.app_timezone,
            "past_hours": 1,
            "forecast_hours": 1,
            "domains": "auto",
        }
        async with httpx.AsyncClient(timeout=self._settings.air_quality_timeout_seconds) as client:
            response = await client.get(self._settings.air_quality_api_url, params=params)
            response.raise_for_status()

        payload = response.json()
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        if not isinstance(times, list) or not times:
            raise ExternalAPIError("Air quality API returned no hourly timestamps")

        now = datetime.now(self._timezone)
        pm10 = self._pick_air_quality_value(times=times, values=hourly.get("pm10") or [], now=now)
        pm2_5 = self._pick_air_quality_value(times=times, values=hourly.get("pm2_5") or [], now=now)
        if pm10 is None and pm2_5 is None:
            raise ExternalAPIError("Air quality API returned no PM data")
        return pm10, pm2_5

    def _build_base_candidates(self, now: datetime) -> list[tuple[str, str]]:
        slots = self._settings.kma_base_slot_list
        candidates: list[tuple[str, str]] = []
        for days_back in [0, 1]:
            target_date = (now - timedelta(days=days_back)).date()
            eligible_slots: list[str] = []
            for slot in slots:
                slot_time = time(hour=int(slot[:2]), minute=int(slot[2:]))
                slot_dt = datetime.combine(target_date, slot_time, tzinfo=self._timezone)
                if slot_dt <= now:
                    eligible_slots.append(slot)
            for slot in reversed(eligible_slots):
                candidates.append((target_date.strftime("%Y%m%d"), slot))
            if candidates:
                break
        return candidates

    def _build_min_temp_candidates(self, now: datetime) -> list[tuple[str, str]]:
        today = now.date()
        yesterday = today - timedelta(days=1)
        if now.time() >= time(hour=2):
            return [
                (today.strftime("%Y%m%d"), "0200"),
                (yesterday.strftime("%Y%m%d"), "2300"),
            ]
        return [
            (yesterday.strftime("%Y%m%d"), "2300"),
            (yesterday.strftime("%Y%m%d"), "2000"),
            (yesterday.strftime("%Y%m%d"), "1700"),
        ]

    async def _fetch_daily_min_temp(self, *, grid_x: int, grid_y: int) -> str | None:
        now = datetime.now(self._timezone)
        today = now.strftime("%Y%m%d")
        for base_date, base_time in self._build_min_temp_candidates(now):
            try:
                items = await self._fetch_forecast(base_date=base_date, base_time=base_time, grid_x=grid_x, grid_y=grid_y)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "weather_daily_min_fetch_failed",
                    extra={
                        "base_date": base_date,
                        "base_time": base_time,
                        "grid_x": grid_x,
                        "grid_y": grid_y,
                        "error": str(exc),
                    },
                )
                continue

            today_items = [item for item in items if item.get("fcstDate") == today]
            min_temp = self._pick_single(today_items, "TMN")
            if min_temp is not None:
                return min_temp

            logger.info(
                "weather_daily_min_missing",
                extra={
                    "base_date": base_date,
                    "base_time": base_time,
                    "grid_x": grid_x,
                    "grid_y": grid_y,
                },
            )

        return None

    def _parse_snapshot(self, *, items: list[dict], label: str) -> WeatherSnapshot:
        now = datetime.now(self._timezone)
        today = now.strftime("%Y%m%d")
        current_hour = now.strftime("%H00")
        today_items = [item for item in items if item.get("fcstDate") == today]
        max_temp = self._pick_single(today_items, "TMX")
        min_temp = self._pick_single(today_items, "TMN")
        precipitation_probability = self._pick_pop(today_items, threshold_time=current_hour)
        sky = self._pick_latest(today_items, "SKY", threshold_time=current_hour)
        precipitation_type = self._pick_latest(today_items, "PTY", threshold_time=current_hour)
        summary = self._map_summary(sky=sky, pty=precipitation_type)
        note = self._build_note(summary=summary, precipitation_probability=precipitation_probability)
        return WeatherSnapshot(
            location_label=label,
            summary=summary,
            min_temp=f"{min_temp}°" if min_temp is not None else "-",
            max_temp=f"{max_temp}°" if max_temp is not None else "-",
            precipitation_probability=f"{precipitation_probability}%" if precipitation_probability else "-",
            note=note,
            fetched_at=now,
        )

    def _grid_to_latlon(self, *, grid_x: int, grid_y: int) -> tuple[float, float]:
        deg_to_rad = math.pi / 180.0
        rad_to_deg = 180.0 / math.pi
        re = self.KMA_GRID_EARTH_RADIUS_KM / self.KMA_GRID_SPACING_KM
        slat1 = self.KMA_GRID_STANDARD_LAT1 * deg_to_rad
        slat2 = self.KMA_GRID_STANDARD_LAT2 * deg_to_rad
        olon = self.KMA_GRID_ORIGIN_LON * deg_to_rad
        olat = self.KMA_GRID_ORIGIN_LAT * deg_to_rad

        sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
        sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
        sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
        sf = math.pow(sf, sn) * math.cos(slat1) / sn
        ro = math.tan(math.pi * 0.25 + olat * 0.5)
        ro = re * sf / math.pow(ro, sn)

        xn = float(grid_x) - self.KMA_GRID_ORIGIN_X
        yn = ro - float(grid_y) + self.KMA_GRID_ORIGIN_Y
        ra = math.sqrt(xn * xn + yn * yn)
        alat = math.pow(re * sf / ra, 1.0 / sn)
        alat = 2.0 * math.atan(alat) - math.pi * 0.5
        theta = 0.0 if xn == 0 else math.atan2(xn, yn)
        alon = theta / sn + olon
        return alat * rad_to_deg, alon * rad_to_deg

    @staticmethod
    def _pick_single(items: list[dict], category: str) -> str | None:
        for item in items:
            if item.get("category") == category:
                return str(item.get("fcstValue"))
        return None

    @staticmethod
    def _pick_pop(items: list[dict], *, threshold_time: str | None = None) -> str | None:
        candidates = sorted(
            [item for item in items if item.get("category") == "POP"],
            key=lambda item: item.get("fcstTime", ""),
        )
        if threshold_time is not None:
            for item in candidates:
                if item.get("fcstTime", "") >= threshold_time and str(item.get("fcstValue")).isdigit():
                    return str(item.get("fcstValue"))
            for item in reversed(candidates):
                if str(item.get("fcstValue")).isdigit():
                    return str(item.get("fcstValue"))
            return None
        pops = [int(item.get("fcstValue")) for item in candidates if str(item.get("fcstValue")).isdigit()]
        if not pops:
            return None
        return str(max(pops))

    @staticmethod
    def _pick_latest(items: list[dict], category: str, *, threshold_time: str | None = None) -> str | None:
        candidates = sorted(
            [item for item in items if item.get("category") == category],
            key=lambda item: item.get("fcstTime", ""),
        )
        if not candidates:
            return None
        if threshold_time is not None:
            for item in candidates:
                if item.get("fcstTime", "") >= threshold_time:
                    return str(item.get("fcstValue"))
        return str(candidates[-1].get("fcstValue"))

    def _pick_air_quality_value(self, *, times: list[object], values: list[object], now: datetime) -> float | None:
        candidates: list[tuple[datetime, float]] = []
        for raw_time, raw_value in zip(times, values):
            value = self._to_float(raw_value)
            if value is None:
                continue
            timestamp = datetime.fromisoformat(str(raw_time))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=self._timezone)
            else:
                timestamp = timestamp.astimezone(self._timezone)
            candidates.append((timestamp, value))

        if not candidates:
            return None

        past_candidates = [item for item in candidates if item[0] <= now]
        if past_candidates:
            return past_candidates[-1][1]
        return candidates[0][1]

    @staticmethod
    def _map_summary(*, sky: str | None, pty: str | None) -> str:
        precipitation_map = {"1": "비", "2": "비/눈", "3": "눈", "4": "소나기"}
        sky_map = {"1": "맑음", "3": "구름많음", "4": "흐림"}
        if pty and pty != "0":
            return precipitation_map.get(pty, "강수")
        return sky_map.get(sky or "", "날씨 정보 확인 중")

    @staticmethod
    def _build_note(*, summary: str, precipitation_probability: str | None) -> str:
        try:
            pop = int(precipitation_probability or "0")
        except ValueError:
            pop = 0
        if pop >= 60:
            return "우산 챙기면 마음이 훨씬 편해질 수 있어요."
        if "맑음" in summary:
            return "무리 없이 편안한 하루 흐름만 챙기면 충분해요."
        return "큰 변수보다 일정 흐름만 가볍게 챙기면 좋아요."

    @classmethod
    def _resolve_air_quality_grade(cls, *, pm10: float | None, pm2_5: float | None) -> str | None:
        grades = [grade for grade in (cls._grade_pm10(pm10), cls._grade_pm2_5(pm2_5)) if grade is not None]
        if not grades:
            return None
        return max(grades, key=lambda grade: cls.AIR_QUALITY_SEVERITY[grade])

    @staticmethod
    def _grade_pm10(value: float | None) -> str | None:
        return WeatherService._grade_pollutant(value, thresholds=(30.0, 80.0, 150.0))

    @staticmethod
    def _grade_pm2_5(value: float | None) -> str | None:
        return WeatherService._grade_pollutant(value, thresholds=(15.0, 35.0, 75.0))

    @staticmethod
    def _grade_pollutant(value: float | None, *, thresholds: tuple[float, float, float]) -> str | None:
        if value is None:
            return None
        if value <= thresholds[0]:
            return "좋음"
        if value <= thresholds[1]:
            return "보통"
        if value <= thresholds[2]:
            return "나쁨"
        return "매우나쁨"

    @staticmethod
    def _format_air_quality_value(value: float) -> str:
        return f"{value:.1f}".rstrip("0").rstrip(".")

    @staticmethod
    def _to_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None
