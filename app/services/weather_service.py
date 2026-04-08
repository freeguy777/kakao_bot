from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.errors import ConfigurationError, ExternalAPIError
from app.schemas import EffectiveRoomConfig, WeatherSnapshot

logger = logging.getLogger(__name__)


class WeatherService:
    BASE_URL = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst"

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
