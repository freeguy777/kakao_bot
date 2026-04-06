from __future__ import annotations

from app.schemas import EffectiveRoomConfig


class FamilyBriefService:
    def __init__(self, *, weather_service: object, child_age_service: object) -> None:
        self._weather_service = weather_service
        self._child_age_service = child_age_service

    async def build_daily_message(self, room: EffectiveRoomConfig) -> str:
        sections: list[str] = []
        if room.features.get("weather"):
            weather = await self._weather_service.fetch_today_weather(room)
            sections.append(weather.to_message())
        if room.features.get("child_age"):
            age = self._child_age_service.calculate()
            sections.append(age.to_message())
        return "\n\n".join(section for section in sections if section).strip()
