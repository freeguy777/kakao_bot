from __future__ import annotations

import httpx
import pytest

from app.services.chat_service import ChatService


class FakeGeminiResponse:
    def __init__(self, *, status_code: int = 200, payload: dict[str, object] | None = None, text: str = "") -> None:
        self._status_code = status_code
        self._payload = payload or {}
        self._text = text

    def raise_for_status(self) -> None:
        if self._status_code < 400:
            return None
        request = httpx.Request("POST", "https://example.com")
        response = httpx.Response(self._status_code, request=request, text=self._text)
        raise httpx.HTTPStatusError("server error", request=request, response=response)

    def json(self) -> dict[str, object]:
        return self._payload


class FakeRoomRegistry:
    def load_persona_text(self, room_name: str) -> str:
        return f"{room_name} persona"


def build_service(test_settings) -> ChatService:
    return ChatService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        room_registry=FakeRoomRegistry(),
        delivery_service=object(),
        admin_notifier=object(),
    )


async def test_generate_reply_retries_once_after_503_and_succeeds(test_settings, monkeypatch) -> None:
    service = build_service(test_settings)
    responses = [
        FakeGeminiResponse(status_code=503, text="service unavailable"),
        FakeGeminiResponse(
            payload={"candidates": [{"content": {"parts": [{"text": "retried reply"}]}}]},
        ),
    ]
    captured_calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self._responses = list(responses)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, params: dict[str, str], json: dict[str, object]):
            captured_calls.append({"url": url, "params": params, "json": json})
            return self._responses.pop(0)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.services.chat_service.asyncio.sleep", fake_sleep)

    reply = await service.generate_reply("테스트하는방방방", "안녕")

    assert reply == "retried reply"
    assert len(captured_calls) == 2
    assert sleep_calls == [1.2]


async def test_generate_reply_retries_once_after_503_then_raises(test_settings, monkeypatch) -> None:
    service = build_service(test_settings)
    responses = [
        FakeGeminiResponse(status_code=503, text="service unavailable"),
        FakeGeminiResponse(status_code=503, text="service unavailable"),
    ]
    captured_calls: list[dict[str, object]] = []
    sleep_calls: list[float] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            self._responses = list(responses)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, params: dict[str, str], json: dict[str, object]):
            captured_calls.append({"url": url, "params": params, "json": json})
            return self._responses.pop(0)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.services.chat_service.asyncio.sleep", fake_sleep)

    with pytest.raises(httpx.HTTPStatusError):
        await service.generate_reply("테스트하는방방방", "안녕")

    assert len(captured_calls) == 2
    assert sleep_calls == [1.2]
