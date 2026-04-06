from __future__ import annotations

from unittest.mock import AsyncMock


async def test_weather_refetches_when_data_missing(app) -> None:
    service = app.state.weather_service
    room = app.state.room_registry.resolve_room("가족 카톡방")
    assert room is not None

    missing = [
        {"category": "SKY", "fcstDate": "20260406", "fcstTime": "1200", "fcstValue": "4"},
    ]
    full = [
        {"category": "SKY", "fcstDate": "20260406", "fcstTime": "1200", "fcstValue": "4"},
        {"category": "TMN", "fcstDate": "20260406", "fcstTime": "0600", "fcstValue": "8"},
        {"category": "TMX", "fcstDate": "20260406", "fcstTime": "1500", "fcstValue": "17"},
        {"category": "POP", "fcstDate": "20260406", "fcstTime": "1200", "fcstValue": "30"},
        {"category": "PTY", "fcstDate": "20260406", "fcstTime": "1200", "fcstValue": "0"},
    ]
    service._fetch_with_fallback = AsyncMock(side_effect=[missing, full])
    snapshot = await service.fetch_today_weather(room)
    assert snapshot.location_label == "김해율하"
    assert snapshot.max_temp == "17°"
    assert snapshot.precipitation_probability == "30%"
