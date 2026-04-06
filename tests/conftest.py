from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    return Settings(
        inbound_bot_secret="test-secret",
        socket_shared_token="test-token",
        messengerbot_bot_name="gateway-bot",
        database_url=f"sqlite:///{db_dir / 'test.db'}",
        smartphone_host="127.0.0.1",
        smartphone_socket_port=65530,
        weather_api_key="weather-key",
        gemini_api_key="gemini-key",
        kimi_api_key="kimi-key",
        room_config_path=Path("config/rooms.yaml"),
        prompt_config_path=Path("config/prompts.yaml"),
    )


@pytest.fixture
def app(test_settings: Settings):
    return create_app(test_settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
