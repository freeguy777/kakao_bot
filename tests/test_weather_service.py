from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

import app.services.weather_service as weather_service_module
from app.errors import ExternalAPIError


async def test_weather_forecast_uses_auth_key_param(app, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "response": {
                    "body": {
                        "items": {
                            "item": [
                                {"category": "SKY", "fcstDate": "20260406", "fcstTime": "1200", "fcstValue": "1"}
                            ]
                        }
                    }
                }
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, params: dict[str, object]):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    service = app.state.weather_service
    await service._fetch_forecast(base_date="20260406", base_time="0200", grid_x=62, grid_y=125)

    assert captured["params"]["authKey"] == app.state.settings.weather_api_key
    assert "serviceKey" not in captured["params"]


def test_kma_base_slots_are_sorted_chronologically(app) -> None:
    service = app.state.weather_service
    now = datetime(2026, 4, 6, 23, 31, tzinfo=service._timezone)

    candidates = service._build_base_candidates(now)

    assert service._settings.kma_base_slot_list == ["0200", "0500", "0800", "1100", "1400", "1700", "2000", "2300"]
    assert candidates[0] == ("20260406", "2300")
    assert ("20260406", "0200") in candidates


def test_weather_snapshot_uses_current_hour_boundary_for_selection(app, monkeypatch) -> None:
    service = app.state.weather_service

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 6, 22, 31, tzinfo=tz)

    monkeypatch.setattr(weather_service_module, "datetime", FrozenDateTime)

    snapshot = service._parse_snapshot(
        items=[
            {"category": "SKY", "fcstDate": "20260406", "fcstTime": "2200", "fcstValue": "1"},
            {"category": "SKY", "fcstDate": "20260406", "fcstTime": "2300", "fcstValue": "4"},
            {"category": "PTY", "fcstDate": "20260406", "fcstTime": "2200", "fcstValue": "0"},
            {"category": "PTY", "fcstDate": "20260406", "fcstTime": "2300", "fcstValue": "1"},
            {"category": "POP", "fcstDate": "20260406", "fcstTime": "2200", "fcstValue": "10"},
            {"category": "POP", "fcstDate": "20260406", "fcstTime": "2300", "fcstValue": "90"},
            {"category": "TMN", "fcstDate": "20260406", "fcstTime": "0600", "fcstValue": "8"},
            {"category": "TMX", "fcstDate": "20260406", "fcstTime": "1500", "fcstValue": "17"},
        ],
        label="서울강남",
    )

    assert snapshot.summary == "맑음"
    assert snapshot.precipitation_probability == "10%"


async def test_weather_fallback_aggregates_candidate_errors(app) -> None:
    service = app.state.weather_service
    service._fetch_forecast = AsyncMock(side_effect=[RuntimeError("bad current"), RuntimeError("bad fallback")])

    with pytest.raises(ExternalAPIError, match=r"20260406 2000=bad current; 20260406 1700=bad fallback"):
        await service._fetch_with_fallback(
            grid_x=62,
            grid_y=125,
            candidates=[("20260406", "2000"), ("20260406", "1700")],
        )


def test_daily_min_candidates_switch_after_two_am(app) -> None:
    service = app.state.weather_service

    before_two = datetime(2026, 4, 6, 1, 30, tzinfo=service._timezone)
    after_two = datetime(2026, 4, 6, 8, 10, tzinfo=service._timezone)

    assert service._build_min_temp_candidates(before_two) == [
        ("20260405", "2300"),
        ("20260405", "2000"),
        ("20260405", "1700"),
    ]
    assert service._build_min_temp_candidates(after_two) == [
        ("20260406", "0200"),
        ("20260405", "2300"),
    ]


async def test_weather_fetches_daily_min_from_fixed_slot(app, monkeypatch) -> None:
    service = app.state.weather_service
    room = app.state.room_registry.resolve_room("가족방")
    assert room is not None

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 6, 8, 10, tzinfo=tz)

    missing = [
        {"category": "SKY", "fcstDate": "20260406", "fcstTime": "0900", "fcstValue": "4"},
        {"category": "POP", "fcstDate": "20260406", "fcstTime": "0900", "fcstValue": "30"},
        {"category": "PTY", "fcstDate": "20260406", "fcstTime": "0900", "fcstValue": "0"},
        {"category": "TMX", "fcstDate": "20260406", "fcstTime": "1500", "fcstValue": "17"},
    ]
    full = missing + [
        {"category": "TMN", "fcstDate": "20260406", "fcstTime": "0600", "fcstValue": "8"},
    ]

    monkeypatch.setattr(weather_service_module, "datetime", FrozenDateTime)
    service._build_base_candidates = lambda now: [("20260406", "0800")]
    service._build_min_temp_candidates = lambda now: [("20260406", "0200")]
    service._fetch_forecast = AsyncMock(side_effect=[missing, full])

    snapshot = await service.fetch_today_weather(room)

    assert snapshot.location_label == "김해율하"
    assert snapshot.min_temp == "8°"
    assert snapshot.max_temp == "17°"
    assert snapshot.precipitation_probability == "30%"
    assert service._fetch_forecast.await_count == 2
