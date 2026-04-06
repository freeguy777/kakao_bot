from __future__ import annotations

import asyncio
import json

import pytest

from app.errors import RetryableDeliveryError
from app.services.socket_client import SocketClient


class FakeWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def test_socket_client_sends_built_in_socket_envelope(monkeypatch, test_settings) -> None:
    client = SocketClient(test_settings)
    writer = FakeWriter()

    async def fake_open_connection(host: str, port: int):
        assert host == test_settings.smartphone_host
        assert port == test_settings.smartphone_socket_port
        return object(), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    await client.send_message(
        message_id="m1",
        target_room="family-room",
        text="hello built-in socket",
        package_name="custom.pkg",
    )

    payload = json.loads(writer.buffer.decode("utf-8").strip())
    assert payload["name"] == "debugRoom"
    assert payload["data"]["botName"] == test_settings.messengerbot_bot_name
    assert payload["data"]["authorName"] == test_settings.mb_socket_control_author_name
    assert payload["data"]["roomName"] == test_settings.mb_socket_control_room_name
    assert payload["data"]["packageName"] == "custom.pkg"

    command = json.loads(payload["data"]["message"])
    assert command == {
        "type": "send_message",
        "token": test_settings.socket_shared_token,
        "message_id": "m1",
        "target_room": "family-room",
        "text": "hello built-in socket",
        "package_name": "custom.pkg",
    }
    assert writer.closed is True


async def test_socket_client_raises_retryable_error_on_connect_failure(monkeypatch, test_settings) -> None:
    client = SocketClient(test_settings)

    async def fake_open_connection(host: str, port: int):
        raise OSError("socket unavailable")

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    with pytest.raises(RetryableDeliveryError, match="socket unavailable"):
        await client.send_message(message_id="m1", target_room="room", text="hello")


async def test_socket_client_probe_is_connect_only(monkeypatch, test_settings) -> None:
    client = SocketClient(test_settings)
    writer = FakeWriter()

    async def fake_open_connection(host: str, port: int):
        return object(), writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    result = await client.probe()

    assert result == {"status": "reachable", "mode": "connect_only"}
    assert writer.buffer == b""
    assert writer.closed is True
