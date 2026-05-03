from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class InboundEvent(Base):
    __tablename__ = "inbound_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    log_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    room: Mapped[str] = mapped_column(String(255), index=True)
    content: Mapped[str] = mapped_column(Text)
    package_name: Mapped[str] = mapped_column(String(255))
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    server_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    processing_status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    target_room: Mapped[str] = mapped_column(String(255), index=True)
    package_name: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    failure_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=1)
    total_chunks: Mapped[int] = mapped_column(Integer, default=1)
    correlation_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    attempts: Mapped[list["DeliveryAttempt"]] = relationship(back_populates="message", cascade="all, delete-orphan")


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outbound_message_id: Mapped[int] = mapped_column(ForeignKey("outbound_messages.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ack_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    message: Mapped[OutboundMessage] = relationship(back_populates="attempts")


class ResearchArtifact(Base):
    __tablename__ = "research_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    artifact_date: Mapped[date] = mapped_column(Date, index=True)
    artifact_type: Mapped[str] = mapped_column(String(64), index=True)
    room_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_name: Mapped[str] = mapped_column(String(128))
    summary_text: Mapped[str] = mapped_column(Text)
    detail_text: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OptionsPCRDailySummary(Base):
    __tablename__ = "options_pcr_daily_summary"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "date_us",
            "source_environment",
            name="uq_options_pcr_daily_summary_symbol_date_env",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date_us: Mapped[date] = mapped_column(Date, index=True)
    date_kst: Mapped[date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_1d_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    pcr_oi_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    pcr_vol_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_oi_total: Mapped[int] = mapped_column(Integer)
    call_oi_total: Mapped[int] = mapped_column(Integer)
    put_vol_total: Mapped[int] = mapped_column(Integer)
    call_vol_total: Mapped[int] = mapped_column(Integer)
    total_option_volume: Mapped[int] = mapped_column(Integer)
    total_option_oi: Mapped[int] = mapped_column(Integer)
    short_dte_pcr_oi: Mapped[float | None] = mapped_column(Float, nullable=True)
    short_dte_pcr_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_quality_flag: Mapped[str] = mapped_column(String(64))
    should_publish_public: Mapped[bool] = mapped_column(Boolean)
    no_publish_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(64))
    source_environment: Mapped[str] = mapped_column(String(32))
    retrieved_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    oi_effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    by_expiry_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class FeatureOverride(Base):
    __tablename__ = "feature_overrides"
    __table_args__ = (UniqueConstraint("room_name", "feature_name", name="uq_feature_override"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_name: Mapped[str] = mapped_column(String(255), index=True)
    feature_name: Mapped[str] = mapped_column(String(128), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ScheduledJobRecord(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
