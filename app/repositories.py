from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app import constants
from app.db import commit_with_retry, session_scope
from app.models import DeliveryAttempt, FeatureOverride, InboundEvent, OutboundMessage, ResearchArtifact, ScheduledJobRecord
from app.schemas import DeliveryQueueSnapshot, HanallArtifact, NormalizedInboundEvent


@dataclass(slots=True)
class SQLitePolicy:
    retries: int
    delay_seconds: float


@dataclass(slots=True)
class InboundEventClaim:
    duplicate: bool
    should_process: bool
    already_processed: bool


class EventRepository:
    def __init__(self, session_factory: sessionmaker[Session], sqlite_policy: SQLitePolicy) -> None:
        self._session_factory = session_factory
        self._sqlite_policy = sqlite_policy

    def claim_for_processing(self, event: NormalizedInboundEvent) -> InboundEventClaim:
        now = datetime.now(timezone.utc)
        with session_scope(self._session_factory) as session:
            record = session.scalar(select(InboundEvent).where(InboundEvent.log_id == event.log_id))
            duplicate = record is not None
            if record is None:
                record = InboundEvent(
                    log_id=event.log_id,
                    room=event.room,
                    content=event.content,
                    package_name=event.package_name,
                    author_name=event.author_name,
                    author_hash=event.author_hash,
                    client_received_at=event.client_received_at,
                    source_timestamp=event.source_timestamp,
                    server_received_at=event.server_received_at,
                    processing_status=constants.INBOUND_STATUS_PENDING,
                )
                session.add(record)
                try:
                    commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)
                except IntegrityError:
                    session.rollback()
                    record = session.scalar(select(InboundEvent).where(InboundEvent.log_id == event.log_id))
                    duplicate = True

            if record is None:
                return InboundEventClaim(duplicate=True, should_process=False, already_processed=False)
            if record.processing_status == constants.INBOUND_STATUS_PROCESSED:
                return InboundEventClaim(duplicate=duplicate, should_process=False, already_processed=True)

            record.processing_status = constants.INBOUND_STATUS_PROCESSING
            record.processing_started_at = now
            record.last_error_message = None
            try:
                commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)
            except IntegrityError:
                session.rollback()
                return InboundEventClaim(duplicate=True, should_process=False, already_processed=False)
            return InboundEventClaim(duplicate=duplicate, should_process=True, already_processed=False)

    def mark_processed(self, log_id: str) -> None:
        now = datetime.now(timezone.utc)
        with session_scope(self._session_factory) as session:
            record = session.scalar(select(InboundEvent).where(InboundEvent.log_id == log_id))
            if record is None:
                return
            record.processing_status = constants.INBOUND_STATUS_PROCESSED
            record.processing_started_at = None
            record.processed_at = now
            record.last_error_message = None
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)

    def mark_failed(self, log_id: str, error_message: str) -> None:
        with session_scope(self._session_factory) as session:
            record = session.scalar(select(InboundEvent).where(InboundEvent.log_id == log_id))
            if record is None:
                return
            record.processing_status = constants.INBOUND_STATUS_FAILED
            record.processing_started_at = None
            record.last_error_message = error_message
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)


class DeliveryRepository:
    def __init__(self, session_factory: sessionmaker[Session], sqlite_policy: SQLitePolicy) -> None:
        self._session_factory = session_factory
        self._sqlite_policy = sqlite_policy

    def create_message(
        self,
        *,
        message_id: str,
        target_room: str,
        package_name: str,
        text: str,
        chunk_index: int,
        total_chunks: int,
        correlation_key: str | None,
    ) -> OutboundMessage:
        now = datetime.now(timezone.utc)
        record = OutboundMessage(
            message_id=message_id,
            target_room=target_room,
            package_name=package_name,
            text=text,
            status=constants.OUTBOUND_STATUS_PENDING,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            correlation_key=correlation_key,
            created_at=now,
            updated_at=now,
        )
        with session_scope(self._session_factory) as session:
            session.add(record)
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)
            session.refresh(record)
            return record

    def get_message(self, message_id: str) -> OutboundMessage | None:
        with session_scope(self._session_factory) as session:
            return session.scalar(select(OutboundMessage).where(OutboundMessage.message_id == message_id))

    def get_queue_snapshot(self) -> DeliveryQueueSnapshot:
        with session_scope(self._session_factory) as session:
            pending_count = session.scalar(
                select(func.count()).select_from(OutboundMessage).where(OutboundMessage.status == constants.OUTBOUND_STATUS_PENDING)
            )
            failed_count = session.scalar(
                select(func.count()).select_from(OutboundMessage).where(OutboundMessage.status == constants.OUTBOUND_STATUS_FAILED)
            )
            latest_failed_ids = list(
                session.scalars(
                    select(OutboundMessage.message_id)
                    .where(OutboundMessage.status == constants.OUTBOUND_STATUS_FAILED)
                    .order_by(OutboundMessage.updated_at.desc())
                    .limit(5)
                )
            )
            return DeliveryQueueSnapshot(
                pending_count=int(pending_count or 0),
                failed_count=int(failed_count or 0),
                latest_failed_ids=latest_failed_ids,
            )

    def record_attempt(
        self,
        *,
        message_id: str,
        attempt_no: int,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
        ack_received_at: datetime | None = None,
    ) -> None:
        with session_scope(self._session_factory) as session:
            message = session.scalar(select(OutboundMessage).where(OutboundMessage.message_id == message_id))
            if message is None:
                return
            session.add(
                DeliveryAttempt(
                    outbound_message_id=message.id,
                    attempt_no=attempt_no,
                    status=status,
                    error_code=error_code,
                    error_message=error_message,
                    created_at=datetime.now(timezone.utc),
                    ack_received_at=ack_received_at,
                )
            )
            message.updated_at = datetime.now(timezone.utc)
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)

    def mark_success(self, message_id: str) -> None:
        with session_scope(self._session_factory) as session:
            message = session.scalar(select(OutboundMessage).where(OutboundMessage.message_id == message_id))
            if message is None:
                return
            message.status = constants.OUTBOUND_STATUS_SENT
            message.acknowledged_at = datetime.now(timezone.utc)
            message.updated_at = datetime.now(timezone.utc)
            message.last_error_code = None
            message.last_error_message = None
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)

    def mark_failed(self, message_id: str, *, failure_type: str, error_code: str | None, error_message: str | None) -> None:
        with session_scope(self._session_factory) as session:
            message = session.scalar(select(OutboundMessage).where(OutboundMessage.message_id == message_id))
            if message is None:
                return
            message.status = constants.OUTBOUND_STATUS_FAILED
            message.failure_type = failure_type
            message.last_error_code = error_code
            message.last_error_message = error_message
            message.updated_at = datetime.now(timezone.utc)
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)

    def mark_pending(self, message_id: str) -> None:
        with session_scope(self._session_factory) as session:
            message = session.scalar(select(OutboundMessage).where(OutboundMessage.message_id == message_id))
            if message is None:
                return
            message.status = constants.OUTBOUND_STATUS_PENDING
            message.updated_at = datetime.now(timezone.utc)
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)


class ArtifactRepository:
    def __init__(self, session_factory: sessionmaker[Session], sqlite_policy: SQLitePolicy) -> None:
        self._session_factory = session_factory
        self._sqlite_policy = sqlite_policy

    def get_by_key(self, artifact_key: str) -> HanallArtifact | None:
        with session_scope(self._session_factory) as session:
            record = session.scalar(select(ResearchArtifact).where(ResearchArtifact.artifact_key == artifact_key))
            if record is None:
                return None
            raw_payload = json.loads(record.raw_json) if record.raw_json else None
            return HanallArtifact(
                artifact_key=record.artifact_key,
                artifact_date=record.artifact_date,
                summary_text=record.summary_text,
                detail_text=record.detail_text,
                model_name=record.model_name,
                raw_response=raw_payload,
            )

    def save(self, artifact: HanallArtifact, artifact_type: str, room_name: str | None = None) -> HanallArtifact:
        raw_json = json.dumps(artifact.raw_response, ensure_ascii=False) if artifact.raw_response is not None else None
        now = datetime.now(timezone.utc)
        with session_scope(self._session_factory) as session:
            record = session.scalar(select(ResearchArtifact).where(ResearchArtifact.artifact_key == artifact.artifact_key))
            if record is None:
                record = ResearchArtifact(
                    artifact_key=artifact.artifact_key,
                    artifact_date=artifact.artifact_date,
                    artifact_type=artifact_type,
                    room_name=room_name,
                    model_name=artifact.model_name,
                    summary_text=artifact.summary_text,
                    detail_text=artifact.detail_text,
                    raw_json=raw_json,
                    created_at=now,
                )
                session.add(record)
            else:
                record.artifact_date = artifact.artifact_date
                record.artifact_type = artifact_type
                record.room_name = room_name
                record.model_name = artifact.model_name
                record.summary_text = artifact.summary_text
                record.detail_text = artifact.detail_text
                record.raw_json = raw_json
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)
        return artifact


class FeatureOverrideRepository:
    def __init__(self, session_factory: sessionmaker[Session], sqlite_policy: SQLitePolicy) -> None:
        self._session_factory = session_factory
        self._sqlite_policy = sqlite_policy

    def get_overrides(self, room_name: str | None = None) -> dict[str, bool] | dict[tuple[str, str], bool]:
        with session_scope(self._session_factory) as session:
            query = select(FeatureOverride)
            if room_name is not None:
                query = query.where(FeatureOverride.room_name == room_name)
                return {row.feature_name: row.enabled for row in session.scalars(query)}
            return {(row.room_name, row.feature_name): row.enabled for row in session.scalars(query)}

    def set_override(self, room_name: str, feature_name: str, enabled: bool) -> None:
        now = datetime.now(timezone.utc)
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(FeatureOverride).where(
                    FeatureOverride.room_name == room_name,
                    FeatureOverride.feature_name == feature_name,
                )
            )
            if record is None:
                session.add(
                    FeatureOverride(
                        room_name=room_name,
                        feature_name=feature_name,
                        enabled=enabled,
                        updated_at=now,
                    )
                )
            else:
                record.enabled = enabled
                record.updated_at = now
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)


class ScheduledJobRepository:
    def __init__(self, session_factory: sessionmaker[Session], sqlite_policy: SQLitePolicy) -> None:
        self._session_factory = session_factory
        self._sqlite_policy = sqlite_policy

    def is_success(self, job_key: str) -> bool:
        with session_scope(self._session_factory) as session:
            record = session.scalar(select(ScheduledJobRecord).where(ScheduledJobRecord.job_key == job_key))
            return bool(record and record.status == constants.SCHEDULED_STATUS_SUCCESS)

    def mark_status(self, job_key: str, status: str, detail: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        with session_scope(self._session_factory) as session:
            record = session.scalar(select(ScheduledJobRecord).where(ScheduledJobRecord.job_key == job_key))
            if record is None:
                record = ScheduledJobRecord(job_key=job_key, status=status, detail=detail, created_at=now, completed_at=now)
                session.add(record)
            else:
                record.status = status
                record.detail = detail
                record.completed_at = now
            commit_with_retry(session, self._sqlite_policy.retries, self._sqlite_policy.delay_seconds)


def sqlite_policy_from_settings(settings: object) -> SQLitePolicy:
    retries = int(getattr(settings, "sqlite_busy_retry_count"))
    delay_seconds = float(getattr(settings, "sqlite_busy_retry_delay_seconds"))
    return SQLitePolicy(retries=retries, delay_seconds=delay_seconds)
