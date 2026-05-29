from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

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


class PollingAckRequest(BaseModel):
    message_ids: list[int] = Field(default_factory=list)
    success: bool = True
    increment_retry: bool | None = None


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


class DeliveryAckPayload(BaseModel):
    message_id: str
    status: Literal["ok", "retryable_error", "fatal_error"]
    target_room: str | None = None
    package_name: str | None = None
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
    admin: bool
    package_name: str | None
    persona_path: Path | None
    features: dict[str, bool]
    hanall_publish_time: str | None
    weather: WeatherRoomConfig


class PromptLibrary(BaseModel):
    youtube_summary: dict[str, Any]
    youtube_summary_lite: dict[str, Any] | None = None
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
    air_quality_grade: str | None = None
    pm10: str | None = None
    pm2_5: str | None = None
    note: str
    fetched_at: datetime

    def to_message(self) -> str:
        lines = [
            f"🌤 오늘 날씨[{self.location_label}]: {self.summary}",
            f"🌡 기온: {self.min_temp} / {self.max_temp}",
            f"☔ 강수: {self.precipitation_probability}",
        ]
        air_quality_line = self._build_air_quality_line()
        if air_quality_line is not None:
            lines.append(air_quality_line)
        lines.append(f"📝 한 줄 메모: {self.note}")
        return "\n".join(lines)

    def _build_air_quality_line(self) -> str | None:
        if not self.air_quality_grade:
            return None

        values: list[str] = []
        if self.pm10:
            values.append(f"PM10 {self.pm10}")
        if self.pm2_5:
            values.append(f"PM2.5 {self.pm2_5}")

        suffix = f" ({' / '.join(values)})" if values else ""
        return f"😷 미세먼지: {self.air_quality_grade}{suffix}"


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


class OptionsPCRDailySummary(BaseModel):
    id: int | None = None
    date_us: date
    date_kst: date
    symbol: str
    close: float | None = None
    change_1d_pct: float | None = None
    pcr_oi_total: float | None = None
    pcr_vol_total: float | None = None
    put_oi_total: int
    call_oi_total: int
    put_vol_total: int
    call_vol_total: int
    total_option_volume: int
    total_option_oi: int
    short_dte_pcr_oi: float | None = None
    short_dte_pcr_vol: float | None = None
    data_quality_flag: str
    should_publish_public: bool
    no_publish_reason: str | None = None
    source: str
    source_environment: str
    retrieved_at_utc: datetime
    oi_effective_date: date | None = None
    by_expiry_json: Any = None
    raw_response_json: Any = None


class OptionsSentimentSnapshot(BaseModel):
    collect_status: Literal["success", "failed", "disabled"]
    symbol: str
    date_us: date | None = None
    date_kst: date | None = None
    source: str = "tradier"
    source_environment: str
    retrieved_at_utc: datetime | None = None
    data_quality_flag: str | None = None
    should_publish_public: bool = False
    no_publish_reason: str | None = None
    reason: str | None = None
    summary: dict[str, Any] | None = None


class OptionsSentimentCollectionResult(BaseModel):
    collect_status: Literal["success", "failed", "disabled"]
    summary: OptionsPCRDailySummary | None = None
    snapshot: OptionsSentimentSnapshot


class HanallRenderedOutput(BaseModel):
    public_text: str
    admin_text: str
    parse_ok: bool = True
    strict_block_parse: bool = True
    discarded_envelope_text: bool = False
    format_repair_attempted: bool = False
    required_sections: list[str] = Field(default_factory=list)
    present_sections: list[str] = Field(default_factory=list)
    missing_sections: list[str] = Field(default_factory=list)


class HanallStructuredFact(BaseModel):
    source_name: str
    source_type: Literal["filing", "clinical_registry"]
    entity: str
    category: str
    title: str
    fact_text: str
    source_id: str | None = None
    source_url: str | None = None
    observed_at: datetime | None = None
    observed_date: date | None = None
    validation_mode: Literal["hard", "soft"] = "soft"
    timestamp_parse_status: str | None = None

    def prompt_line(self) -> str:
        observed_label = self._observed_label()
        pieces = [
            f"- [{self.validation_mode}] {observed_label}",
            self.entity,
            self.source_name,
            self.title,
            self.fact_text,
        ]
        if self.source_id:
            pieces.append(f"id={self.source_id}")
        if self.source_url:
            pieces.append(self.source_url)
        return " | ".join(pieces)

    def validation_tokens(self) -> list[str]:
        tokens = [self.title]
        for title_piece in self.title.split("|"):
            normalized_piece = title_piece.strip()
            if normalized_piece:
                tokens.append(normalized_piece)
        if self.source_id:
            tokens.append(self.source_id)
        if self.source_url:
            tokens.append(self.source_url)
        observed_label = self._observed_label()
        if observed_label:
            tokens.append(observed_label)
        return [token for token in tokens if token]

    def _observed_label(self) -> str:
        if self.observed_at is not None:
            return self.observed_at.strftime("%Y-%m-%d %H:%M KST")
        if self.observed_date is not None:
            return self.observed_date.isoformat()
        return "날짜미상"


class HanallSourceStatus(BaseModel):
    source_name: str
    status: Literal["ok", "unavailable", "skipped"]
    checked_at: datetime
    detail: str
    hard_requirement: bool = False

    def prompt_line(self) -> str:
        checked_at = self.checked_at.strftime("%Y-%m-%d %H:%M KST")
        requirement = "hard" if self.hard_requirement else "soft"
        return f"- {self.source_name}: {self.status} ({requirement}) | {checked_at} | {self.detail}"


class HanallApiBundle(BaseModel):
    DIRECT_VALIDATION_SOURCES: ClassVar[frozenset[str]] = frozenset(
        {"OpenDART", "KIND/KRX", "ClinicalTrials.gov API", "SEC EDGAR API"}
    )
    facts: list[HanallStructuredFact] = Field(default_factory=list)
    source_statuses: list[HanallSourceStatus] = Field(default_factory=list)

    def prompt_block(self) -> str:
        if not self.facts and not self.source_statuses:
            return ""
        lines = [
            "[Structured API facts]",
            "- 아래 정보는 코드가 공식 API에서 선수집한 결과다.",
            "- direct company 판단은 이 블록의 사실을 우선 기준으로 삼아라.",
            "- source status가 unavailable인 범주는 direct update를 '신규 없음'으로 단정하지 말고 Coverage Gaps/Omission Audit에 반영하라.",
            "",
            "[Structured API source status]",
        ]
        if self.source_statuses:
            lines.extend(status.prompt_line() for status in self.source_statuses)
        else:
            lines.append("- 없음")
        lines.extend(["", "[Structured API direct facts]"])
        if self.facts:
            lines.extend(fact.prompt_line() for fact in self.facts)
        else:
            lines.append("- 없음")
        return "\n".join(lines)

    def hard_validation_facts(self) -> list[HanallStructuredFact]:
        return [fact for fact in self.facts if fact.validation_mode == "hard"]

    def direct_validation_facts(self) -> list[HanallStructuredFact]:
        return [fact for fact in self.facts if fact.source_name in self.DIRECT_VALIDATION_SOURCES]

    def has_unavailable_hard_source(self) -> bool:
        return any(status.hard_requirement and status.status == "unavailable" for status in self.source_statuses)


class HanallValidationResult(BaseModel):
    is_valid: bool = True
    issues: list[str] = Field(default_factory=list)
    repair_attempted: bool = False


class DeliveryQueueSnapshot(BaseModel):
    pending_count: int
    failed_count: int
    latest_failed_ids: list[str] = Field(default_factory=list)
