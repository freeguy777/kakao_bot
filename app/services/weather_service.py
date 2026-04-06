from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.errors import ConfigurationError, ExternalAPIError
from app.schemas import EffectiveRoomConfig, WeatherSnapshot


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
        items = await self._fetch_with_fallback(grid_x=grid_x, grid_y=grid_y)
        snapshot = self._parse_snapshot(items=items, label=label)
        if snapshot.min_temp == "-" or snapshot.max_temp == "-" or snapshot.precipitation_probability == "-":
            items = await self._fetch_with_fallback(grid_x=grid_x, grid_y=grid_y, force_all_slots=True)
            snapshot = self._parse_snapshot(items=items, label=label)
        return snapshot

    async def _fetch_with_fallback(self, *, grid_x: int, grid_y: int, force_all_slots: bool = False) -> list[dict]:
        now = datetime.now(self._timezone)
        candidates = self._build_base_candidates(now, force_all_slots=force_all_slots)
        last_error: Exception | None = None
        for base_date, base_time in candidates:
            try:
                return await self._fetch_forecast(base_date=base_date, base_time=base_time, grid_x=grid_x, grid_y=grid_y)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if last_error:
            raise last_error
        raise ExternalAPIError("No KMA base slots available")

    async def _fetch_forecast(self, *, base_date: str, base_time: str, grid_x: int, grid_y: int) -> list[dict]:
        params = {
            "serviceKey": self._settings.weather_api_key,
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

    def _build_base_candidates(self, now: datetime, *, force_all_slots: bool) -> list[tuple[str, str]]:
        slots = self._settings.kma_base_slot_list
        candidates: list[tuple[str, str]] = []
        for days_back in [0, 1]:
            target_date = (now - timedelta(days=days_back)).date()
            eligible_slots: list[str] = []
            for slot in slots:
                slot_time = time(hour=int(slot[:2]), minute=int(slot[2:]))
                slot_dt = datetime.combine(target_date, slot_time, tzinfo=self._timezone)
                if force_all_slots or slot_dt <= now:
                    eligible_slots.append(slot)
            for slot in reversed(eligible_slots):
                candidates.append((target_date.strftime("%Y%m%d"), slot))
            if candidates and not force_all_slots:
                break
        return candidates

    def _parse_snapshot(self, *, items: list[dict], label: str) -> WeatherSnapshot:
        now = datetime.now(self._timezone)
        today = now.strftime("%Y%m%d")
        today_items = [item for item in items if item.get("fcstDate") == today]
        max_temp = self._pick_single(today_items, "TMX")
        min_temp = self._pick_single(today_items, "TMN")
        precipitation_probability = self._pick_pop(today_items)
        sky = self._pick_latest(today_items, "SKY")
        precipitation_type = self._pick_latest(today_items, "PTY")
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
    def _pick_pop(items: list[dict]) -> str | None:
        pops = [int(item.get("fcstValue")) for item in items if item.get("category") == "POP" and str(item.get("fcstValue")).isdigit()]
        if not pops:
            return None
        return str(max(pops))

    def _pick_latest(self, items: list[dict], category: str) -> str | None:
        candidates = sorted(
            [item for item in items if item.get("category") == category],
            key=lambda item: item.get("fcstTime", ""),
        )
        if not candidates:
            return None
        current_time = datetime.now(self._timezone).strftime("%H%M")
        for item in candidates:
            if item.get("fcstTime", "") >= current_time:
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
