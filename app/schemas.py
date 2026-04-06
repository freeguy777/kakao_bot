from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuthorPayload(BaseModel):
    name: str | None = None
    hash: str | None = None


class InboundWebhookPayload(BaseModel):
    room: str
    content: str
    logId: str
    packageName: str
    author: AuthorPayload | None = None
    client_received_at: datetime | None = None
    timestamp: datetime | None = None


class NormalizedInboundEvent(BaseModel):
    room: str
    content: str
    log_id: str
    package_name: str
    author_name: str | None = None
    author_hash: str | None = None
    client_received_at: datetime | None = None
    source_timestamp: datetime | None = None
    server_received_at: datetime


class SocketControlCommand(BaseModel):
    type: Literal["send_message"] = "send_message"
    token: str
    message_id: str
    target_room: str
    text: str
    package_name: str | None = None


class MessengerBotSocketEnvelopeData(BaseModel):
    botName: str
    authorName: str
    roomName: str
    isGroupChat: bool = False
    packageName: str
    message: str


class MessengerBotSocketEnvelope(BaseModel):
    name: Literal["debugRoom"] = "debugRoom"
    data: MessengerBotSocketEnvelopeData


class DeliveryResult(BaseModel):
    message_id: str
    status: Literal["ok", "retryable_error", "fatal_error"]
    failure_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class WeatherRoomConfig(BaseModel):
    enabled: bool = False
    location_label: str | None = None
    grid_x: int | None = None
    grid_y: int | None = None
    publish_time: str | None = None


class RoomFeatureFlags(BaseModel):
    youtube_summary: bool = False
    llm_chat: bool = False
    hanall_briefing: bool = False
    weather: bool = False
    child_age: bool = False
    admin_commands: bool = False


class RoomConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    key: str
    admin: bool = False
    package_name: str | None = None
    persona_path: Path | None = None
    features: RoomFeatureFlags = Field(default_factory=RoomFeatureFlags)
    hanall_publish_time: str | None = None
    weather: WeatherRoomConfig = Field(default_factory=WeatherRoomConfig)

    @field_validator("persona_path", mode="before")
    @classmethod
    def _to_path(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        return Path(value)


class RoomRegistryConfig(BaseModel):
    rooms: list[RoomConfig]


class EffectiveRoomConfig(BaseModel):
    name: str
    key: str
    admin: bool
    package_name: str | None
    persona_path: Path | None
    features: dict[str, bool]
    hanall_publish_time: str | None
    weather: WeatherRoomConfig


class PromptLibrary(BaseModel):
    youtube_summary: dict[str, Any]
    chat_default_system: str
    hanall_runtime_wrapper: str
    hanall_public_format: str
    hanall_admin_format: str


class WeatherSnapshot(BaseModel):
    location_label: str
    summary: str
    min_temp: str
    max_temp: str
    precipitation_probability: str
    note: str
    fetched_at: datetime

    def to_message(self) -> str:
        return (
            f"🌤 오늘 날씨[{self.location_label}]: {self.summary}\n"
            f"🌡 기온: {self.min_temp} / {self.max_temp}\n"
            f"☔ 강수: {self.precipitation_probability}\n"
            f"📝 한 줄 메모: {self.note}"
        )


class ChildAgeSnapshot(BaseModel):
    birth_date: date
    as_of_date: date
    d_plus: int
    months: int
    days: int

    def to_message(self) -> str:
        return f"👶 아들 태어난 지 D+{self.d_plus} ({self.months}개월 {self.days}일)"


class HanallArtifact(BaseModel):
    artifact_key: str
    artifact_date: date
    summary_text: str
    detail_text: str
    model_name: str
    raw_response: dict[str, Any] | None = None


class HanallRenderedOutput(BaseModel):
    public_text: str
    admin_text: str
    parse_ok: bool = True
    required_sections: list[str] = Field(default_factory=list)
    present_sections: list[str] = Field(default_factory=list)
    missing_sections: list[str] = Field(default_factory=list)


class DeliveryQueueSnapshot(BaseModel):
    pending_count: int
    failed_count: int
    latest_failed_ids: list[str] = Field(default_factory=list)
