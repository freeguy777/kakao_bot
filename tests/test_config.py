from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.errors import ConfigurationError
from app.main import create_app


def test_app_startup_rejects_placeholder_secrets(tmp_path: Path) -> None:
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    settings = Settings(
        inbound_bot_secret="change-me",
        socket_shared_token="change-me-too",
        messengerbot_bot_name="change-me-bot",
        database_url=f"sqlite:///{db_dir / 'test.db'}",
        room_config_path=Path("config/rooms.yaml"),
        prompt_config_path=Path("config/prompts.yaml"),
    )
    app = create_app(settings)

    with pytest.raises(ConfigurationError, match="must be replaced before startup"):
        with TestClient(app):
            pass


def test_load_hanall_spec_reads_utf8_file(tmp_path: Path) -> None:
    spec_path = tmp_path / "hanall_spec.md"
    spec_text = "한올 스펙 본문"
    spec_path.write_text(spec_text, encoding="utf-8")
    settings = Settings(
        inbound_bot_secret="test-secret",
        socket_shared_token="test-token",
        messengerbot_bot_name="gateway-bot",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        hanall_spec_path=spec_path,
    )

    assert settings.load_hanall_spec() == spec_text


def test_load_hanall_spec_rejects_missing_file(tmp_path: Path) -> None:
    settings = Settings(
        inbound_bot_secret="test-secret",
        socket_shared_token="test-token",
        messengerbot_bot_name="gateway-bot",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        hanall_spec_path=tmp_path / "missing.md",
    )

    with pytest.raises(ConfigurationError, match="HanAll spec file not found"):
        settings.load_hanall_spec()


def test_load_hanall_spec_rejects_empty_file(tmp_path: Path) -> None:
    spec_path = tmp_path / "empty.md"
    spec_path.write_text("   ", encoding="utf-8")
    settings = Settings(
        inbound_bot_secret="test-secret",
        socket_shared_token="test-token",
        messengerbot_bot_name="gateway-bot",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        hanall_spec_path=spec_path,
    )

    with pytest.raises(ConfigurationError, match="HanAll spec file must not be empty"):
        settings.load_hanall_spec()


def test_load_prompts_requires_hanall_runtime_wrapper(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompts.yaml"
    prompt_path.write_text(
        "\n".join(
            [
                'chat_default_system: "기본 시스템"',
                "youtube_summary:",
                '  title: "요약"',
                '  template: "템플릿"',
                'hanall_public_format: "[한올]\\n{summary}"',
                'hanall_admin_format: "[한올 상세]\\n{detail}"',
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(
        inbound_bot_secret="test-secret",
        socket_shared_token="test-token",
        messengerbot_bot_name="gateway-bot",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        prompt_config_path=prompt_path,
    )

    with pytest.raises(ValidationError):
        settings.load_prompts()


def test_runtime_validation_requires_messengerbot_bot_name(tmp_path: Path) -> None:
    settings = Settings(
        inbound_bot_secret="test-secret",
        socket_shared_token="test-token",
        messengerbot_bot_name="change-me-bot",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
    )

    with pytest.raises(ConfigurationError, match="MESSENGERBOT_BOT_NAME must be replaced before startup"):
        settings.validate_runtime_secrets()


def test_control_channel_defaults_are_loaded(tmp_path: Path) -> None:
    settings = Settings(
        inbound_bot_secret="test-secret",
        socket_shared_token="test-token",
        messengerbot_bot_name="gateway-bot",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
    )

    assert settings.mb_socket_control_author_name == "__FASTAPI__"
    assert settings.mb_socket_control_room_name == "__MB_SOCKET_CONTROL__"
