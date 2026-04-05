from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.application.hanall_page_items import compute_changed_fields
from server.config import get_room_policy, resolve_room
from server.settings import get_settings
from server.utils import now_kst

logger = logging.getLogger(__name__)

_DB_PATH: str | None = None
DB_TIMEOUT_SECONDS = 30
OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_INFLIGHT = "inflight"
OUTBOX_STATUS_SENT = "sent"
OUTBOX_STALE_INFLIGHT_MINUTES = 5
POLLING_HEARTBEAT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class AdminAlertThrottleDecision:
    should_send: bool
    suppressed_count: int


@dataclass(frozen=True)
class PageObservationDecision:
    freshness_state: str
    is_new_item: bool
    is_substantive_update: bool
    is_resurfaced_old_news: bool
    changed_fields: list[str] | None = None
    first_seen_at_kst: str | None = None
    last_seen_at_kst: str | None = None
    previous_fingerprint: str | None = None
    previous_item_title: str | None = None
    title_change_observed_at_kst: str | None = None


def get_db_path() -> str:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = get_settings().sqlite_path
    return _DB_PATH


def _get_connection() -> sqlite3.Connection:
    db_path = Path(get_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=DB_TIMEOUT_SECONDS, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def _get_room_channel_id(room_key: str) -> str | None:
    snapshot = get_room_target_snapshot(room_key) or {}
    snapshot_channel_id = str(snapshot.get("channel_id", "")).strip()
    if snapshot_channel_id:
        return snapshot_channel_id
    room = get_room_policy(room_key)
    if room is None:
        return None
    return room.channel_id or None


def _get_room_target_snapshot_by_channel(channel_id: str | None) -> dict[str, Any] | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    with _get_connection() as conn:
        row = conn.execute(
            """
            SELECT room_key, channel_id, room_name, updated_at
            FROM room_targets
            WHERE channel_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (normalized_channel_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "room_key": str(row["room_key"]).strip(),
        "channel_id": str(row["channel_id"]).strip() if row["channel_id"] else "",
        "room_name": str(row["room_name"]).strip() if row["room_name"] else "",
        "updated_at": str(row["updated_at"]).strip(),
    }


def _serialize_outbox_row(row: sqlite3.Row) -> dict[str, Any]:
    room_key = str(row["room_key"]).strip()
    row_channel_id = str(row["channel_id"] or "").strip()
    snapshot = get_room_target_snapshot(room_key) or {}
    channel_snapshot = _get_room_target_snapshot_by_channel(row_channel_id) or {}
    room = get_room_policy(room_key)
    room_name = (
        str(snapshot.get("room_name", "")).strip()
        or str(channel_snapshot.get("room_name", "")).strip()
        or (room.display_name if room else "")
    )
    channel_id = row_channel_id or str(snapshot.get("channel_id", "")).strip() or str(channel_snapshot.get("channel_id", "")).strip()
    return {
        "id": row["id"],
        "room_key": room_key,
        "channel_id": channel_id,
        "room_name": room_name,
        "message": row["message_text"],
        "source_type": row["source_type"],
        "trace_id": row["trace_id"],
        "retry_count": row["retry_count"],
        "meta": json.loads(row["meta_json"] or "{}"),
        "created_at": row["created_at"],
        "last_attempt_at": row["last_attempt_at"],
    }


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {
        str(row["name"]).strip()
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name in columns:
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
    logger.info("database column added table=%s column=%s", table_name, column_name)


def _ensure_page_observations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_observations (
            source_name TEXT NOT NULL,
            page_name TEXT NOT NULL,
            item_url TEXT NOT NULL,
            item_identity_key TEXT,
            item_title TEXT NOT NULL,
            previous_item_title TEXT,
            title_change_observed_at_kst TEXT,
            content_fingerprint TEXT NOT NULL,
            structured_payload_json TEXT,
            source_specific_identity_json TEXT,
            first_seen_at_kst TEXT NOT NULL,
            last_seen_at_kst TEXT NOT NULL,
            last_published_at_kst TEXT,
            last_updated_at_kst TEXT,
            PRIMARY KEY (source_name, page_name, item_url)
        )
        """
    )
    _ensure_column(conn, "page_observations", "item_identity_key", "TEXT")
    _ensure_column(conn, "page_observations", "previous_item_title", "TEXT")
    _ensure_column(conn, "page_observations", "title_change_observed_at_kst", "TEXT")
    _ensure_column(conn, "page_observations", "structured_payload_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "page_observations", "source_specific_identity_json", "TEXT NOT NULL DEFAULT '{}'")


def _ensure_competitor_universe_observations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS competitor_universe_observations (
            competitor TEXT NOT NULL,
            asset TEXT NOT NULL,
            indication TEXT NOT NULL,
            stage_status TEXT,
            region TEXT,
            primary_source_url TEXT NOT NULL,
            source_label TEXT,
            source_type TEXT,
            content_fingerprint TEXT NOT NULL,
            aliases_json TEXT,
            target_moa TEXT,
            layer TEXT,
            provenance_score REAL,
            first_seen_at_kst TEXT NOT NULL,
            last_seen_at_kst TEXT NOT NULL,
            last_verified_at_kst TEXT NOT NULL,
            PRIMARY KEY (competitor, asset, indication, primary_source_url)
        )
        """
    )
    _ensure_column(conn, "competitor_universe_observations", "aliases_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "competitor_universe_observations", "target_moa", "TEXT")
    _ensure_column(conn, "competitor_universe_observations", "layer", "TEXT")
    _ensure_column(conn, "competitor_universe_observations", "provenance_score", "REAL")
    _ensure_column(conn, "competitor_universe_observations", "last_verified_at_kst", "TEXT NOT NULL DEFAULT ''")


def _ensure_stage2_search_evidence_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stage2_search_evidence (
            trace_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            title TEXT NOT NULL,
            published_at_kst TEXT,
            updated_at_kst TEXT,
            confidence REAL,
            excerpt TEXT,
            evidence_kind TEXT,
            confirms_fields_json TEXT,
            entity TEXT,
            asset TEXT,
            indication TEXT,
            region TEXT,
            inserted_at_kst TEXT NOT NULL,
            PRIMARY KEY (trace_id, evidence_id)
        )
        """
    )
    _ensure_column(conn, "stage2_search_evidence", "entity", "TEXT")
    _ensure_column(conn, "stage2_search_evidence", "asset", "TEXT")
    _ensure_column(conn, "stage2_search_evidence", "indication", "TEXT")
    _ensure_column(conn, "stage2_search_evidence", "region", "TEXT")


def _ensure_stage2_verification_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stage2_verification_runs (
            trace_id TEXT PRIMARY KEY,
            room_key TEXT,
            run_started_at_kst TEXT NOT NULL,
            run_finished_at_kst TEXT,
            stage2_status TEXT NOT NULL,
            used_search_verify INTEGER NOT NULL DEFAULT 0,
            gap_target_count INTEGER NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            backfill_count INTEGER NOT NULL DEFAULT 0,
            discovered_confirmed_count INTEGER NOT NULL DEFAULT 0,
            discovered_unverified_count INTEGER NOT NULL DEFAULT 0,
            coverage_upgrade_count INTEGER NOT NULL DEFAULT 0,
            error_detail TEXT
        )
        """
    )
    _ensure_column(conn, "stage2_verification_runs", "coverage_upgrade_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "stage2_verification_runs", "reused_evidence_count", "INTEGER NOT NULL DEFAULT 0")


def _ensure_stage2_finding_provenance_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stage2_finding_provenance (
            trace_id TEXT NOT NULL,
            finding_identity TEXT NOT NULL,
            candidate_id TEXT,
            field_name TEXT NOT NULL,
            field_value TEXT,
            action_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT,
            evidence_id TEXT,
            provenance_strength TEXT,
            note TEXT,
            recorded_at_kst TEXT NOT NULL
        )
        """
    )


def _ensure_hanall_run_snapshots_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hanall_run_snapshots (
            trace_id TEXT PRIMARY KEY,
            room_key TEXT,
            created_at_kst TEXT NOT NULL,
            stage1_candidate_summary_json TEXT NOT NULL,
            stage1_output_json TEXT NOT NULL,
            merged_stage1_output_json TEXT NOT NULL,
            stage2_output_json TEXT,
            search_memory_json TEXT,
            ranked_issues_json TEXT,
            summary_lines_json TEXT,
            final_text TEXT NOT NULL,
            debug_meta_json TEXT
        )
        """
    )


def _ensure_hanall_brief_snapshots_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hanall_brief_snapshots (
            cache_date_kst TEXT PRIMARY KEY,
            source_room_key TEXT,
            trace_id TEXT,
            created_at_kst TEXT NOT NULL,
            public_text TEXT NOT NULL,
            detailed_text TEXT NOT NULL,
            raw_output_text TEXT
        )
        """
    )


def _parse_kst_text(value: str | None) -> datetime | None:
    normalized = str(value or "").strip().removesuffix(" KST").strip()
    if not normalized:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=now_kst().tzinfo)
        except ValueError:
            continue
    return None


def init_db(sqlite_path: str | None = None) -> None:
    global _DB_PATH
    if sqlite_path:
        _DB_PATH = sqlite_path
    with _get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_key TEXT NOT NULL,
                log_id TEXT NOT NULL,
                sender TEXT,
                message TEXT,
                received_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(room_key, log_id)
            );

            CREATE TABLE IF NOT EXISTS processed_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_key TEXT NOT NULL,
                video_id TEXT NOT NULL,
                source_url TEXT,
                processed_at TEXT NOT NULL,
                UNIQUE(room_key, video_id)
            );

            CREATE TABLE IF NOT EXISTS outbox_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_key TEXT NOT NULL,
                channel_id TEXT,
                message_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                source_type TEXT NOT NULL,
                trace_id TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                meta_json TEXT,
                created_at TEXT NOT NULL,
                last_attempt_at TEXT,
                sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                trace_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduler_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                detail TEXT,
                trace_id TEXT,
                meta_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS polling_heartbeats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                trace_id TEXT,
                checked_at TEXT NOT NULL,
                meta_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS room_targets (
                room_key TEXT PRIMARY KEY,
                channel_id TEXT,
                room_name TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delivery_dedupes (
                dedupe_key TEXT PRIMARY KEY,
                room_key TEXT NOT NULL,
                source_type TEXT NOT NULL,
                trace_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_alert_states (
                throttle_key TEXT PRIMARY KEY,
                room_key TEXT NOT NULL,
                feature_key TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_sent_at TEXT NOT NULL,
                suppressed_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS page_observations (
                source_name TEXT NOT NULL,
                page_name TEXT NOT NULL,
                item_url TEXT NOT NULL,
                item_identity_key TEXT,
                item_title TEXT NOT NULL,
                previous_item_title TEXT,
                title_change_observed_at_kst TEXT,
                content_fingerprint TEXT NOT NULL,
                structured_payload_json TEXT,
                source_specific_identity_json TEXT,
                first_seen_at_kst TEXT NOT NULL,
                last_seen_at_kst TEXT NOT NULL,
                last_published_at_kst TEXT,
                last_updated_at_kst TEXT,
                PRIMARY KEY (source_name, page_name, item_url)
            );

            CREATE TABLE IF NOT EXISTS competitor_universe_observations (
                competitor TEXT NOT NULL,
                asset TEXT NOT NULL,
                indication TEXT NOT NULL,
                stage_status TEXT,
                region TEXT,
                primary_source_url TEXT NOT NULL,
                source_label TEXT,
                source_type TEXT,
                content_fingerprint TEXT NOT NULL,
                aliases_json TEXT,
                target_moa TEXT,
                layer TEXT,
                provenance_score REAL,
                first_seen_at_kst TEXT NOT NULL,
                last_seen_at_kst TEXT NOT NULL,
                last_verified_at_kst TEXT NOT NULL,
                PRIMARY KEY (competitor, asset, indication, primary_source_url)
            );

            CREATE TABLE IF NOT EXISTS stage2_search_evidence (
                trace_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                title TEXT NOT NULL,
                published_at_kst TEXT,
                updated_at_kst TEXT,
                confidence REAL,
                excerpt TEXT,
                evidence_kind TEXT,
                confirms_fields_json TEXT,
                entity TEXT,
                asset TEXT,
                indication TEXT,
                region TEXT,
                inserted_at_kst TEXT NOT NULL,
                PRIMARY KEY (trace_id, evidence_id)
            );

            CREATE TABLE IF NOT EXISTS stage2_verification_runs (
                trace_id TEXT PRIMARY KEY,
                room_key TEXT,
                run_started_at_kst TEXT NOT NULL,
                run_finished_at_kst TEXT,
                stage2_status TEXT NOT NULL,
                used_search_verify INTEGER NOT NULL DEFAULT 0,
                gap_target_count INTEGER NOT NULL DEFAULT 0,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                backfill_count INTEGER NOT NULL DEFAULT 0,
                discovered_confirmed_count INTEGER NOT NULL DEFAULT 0,
                discovered_unverified_count INTEGER NOT NULL DEFAULT 0,
                coverage_upgrade_count INTEGER NOT NULL DEFAULT 0,
                reused_evidence_count INTEGER NOT NULL DEFAULT 0,
                error_detail TEXT
            );

            CREATE TABLE IF NOT EXISTS stage2_finding_provenance (
                trace_id TEXT NOT NULL,
                finding_identity TEXT NOT NULL,
                candidate_id TEXT,
                field_name TEXT NOT NULL,
                field_value TEXT,
                action_type TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT,
                evidence_id TEXT,
                provenance_strength TEXT,
                note TEXT,
                recorded_at_kst TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS hanall_run_snapshots (
                trace_id TEXT PRIMARY KEY,
                room_key TEXT,
                created_at_kst TEXT NOT NULL,
                stage1_candidate_summary_json TEXT NOT NULL,
                stage1_output_json TEXT NOT NULL,
                merged_stage1_output_json TEXT NOT NULL,
                stage2_output_json TEXT,
                search_memory_json TEXT,
                ranked_issues_json TEXT,
                summary_lines_json TEXT,
                final_text TEXT NOT NULL,
                debug_meta_json TEXT
            );

            CREATE TABLE IF NOT EXISTS hanall_brief_snapshots (
                cache_date_kst TEXT PRIMARY KEY,
                source_room_key TEXT,
                trace_id TEXT,
                created_at_kst TEXT NOT NULL,
                public_text TEXT NOT NULL,
                detailed_text TEXT NOT NULL,
                raw_output_text TEXT
            );
            """
        )
        _ensure_column(conn, "outbox_messages", "last_attempt_at", "TEXT")
        _ensure_column(conn, "outbox_messages", "sent_at", "TEXT")
        _ensure_column(conn, "scheduler_events", "detail", "TEXT")
        _ensure_column(conn, "scheduler_events", "trace_id", "TEXT")
        _ensure_column(conn, "scheduler_events", "meta_json", "TEXT")
        _ensure_column(conn, "polling_heartbeats", "trace_id", "TEXT")
        _ensure_column(conn, "polling_heartbeats", "meta_json", "TEXT")
        _ensure_column(conn, "page_observations", "item_identity_key", "TEXT")
        _ensure_column(conn, "page_observations", "previous_item_title", "TEXT")
        _ensure_column(conn, "page_observations", "title_change_observed_at_kst", "TEXT")
        _ensure_column(conn, "page_observations", "structured_payload_json", "TEXT")
        _ensure_column(conn, "page_observations", "source_specific_identity_json", "TEXT")
        _ensure_column(conn, "stage2_search_evidence", "entity", "TEXT")
        _ensure_column(conn, "stage2_search_evidence", "asset", "TEXT")
        _ensure_column(conn, "stage2_search_evidence", "indication", "TEXT")
        _ensure_column(conn, "stage2_search_evidence", "region", "TEXT")
        _ensure_column(conn, "stage2_verification_runs", "coverage_upgrade_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "stage2_verification_runs", "reused_evidence_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_hanall_brief_snapshots_table(conn)
        logger.info("database initialized path=%s", get_db_path())


def get_page_observation(
    *,
    source_name: str,
    page_name: str,
    item_url: str,
    item_identity_key: str | None = None,
) -> dict[str, Any] | None:
    normalized_item_url = str(item_url or "").strip()
    normalized_identity = str(item_identity_key or "").strip()
    storage_item_url = _page_observation_storage_key(
        item_url=normalized_item_url,
        item_identity_key=normalized_identity,
    )
    with _get_connection() as conn:
        _ensure_page_observations_table(conn)
        row = conn.execute(
            """
            SELECT source_name, page_name, item_url, item_identity_key, item_title, previous_item_title,
                   title_change_observed_at_kst, content_fingerprint, structured_payload_json,
                   source_specific_identity_json, first_seen_at_kst, last_seen_at_kst,
                   last_published_at_kst, last_updated_at_kst
            FROM page_observations
            WHERE source_name = ? AND page_name = ? AND item_url = ?
            """,
            (source_name, page_name, storage_item_url),
        ).fetchone()
    if row is None:
        return None
    return {
        "source_name": str(row["source_name"]).strip(),
        "page_name": str(row["page_name"]).strip(),
        "item_url": str(row["item_url"]).strip(),
        "item_identity_key": str(row["item_identity_key"]).strip() if row["item_identity_key"] else None,
        "item_title": str(row["item_title"]).strip(),
        "previous_item_title": str(row["previous_item_title"]).strip() if row["previous_item_title"] else None,
        "title_change_observed_at_kst": (
            str(row["title_change_observed_at_kst"]).strip() if row["title_change_observed_at_kst"] else None
        ),
        "content_fingerprint": str(row["content_fingerprint"]).strip(),
        "structured_payload": json.loads(row["structured_payload_json"] or "{}"),
        "source_specific_identity_json": json.loads(row["source_specific_identity_json"] or "{}"),
        "first_seen_at_kst": str(row["first_seen_at_kst"]).strip(),
        "last_seen_at_kst": str(row["last_seen_at_kst"]).strip(),
        "last_published_at_kst": str(row["last_published_at_kst"]).strip() if row["last_published_at_kst"] else None,
        "last_updated_at_kst": str(row["last_updated_at_kst"]).strip() if row["last_updated_at_kst"] else None,
    }


def _page_observation_storage_key(*, item_url: str | None, item_identity_key: str | None) -> str:
    normalized_url = str(item_url or "").strip()
    normalized_identity = str(item_identity_key or "").strip()
    return normalized_url or normalized_identity or "-"


def record_page_observation(
    *,
    source_name: str,
    page_name: str,
    item_url: str,
    item_identity_key: str | None = None,
    item_title: str,
    content_fingerprint: str,
    observed_at_kst: str,
    published_at_kst: str | None = None,
    updated_at_kst: str | None = None,
    structured_payload: dict[str, Any] | None = None,
    source_specific_identity: dict[str, Any] | None = None,
) -> PageObservationDecision:
    normalized_source_name = str(source_name or "").strip()
    normalized_page_name = str(page_name or "").strip()
    normalized_item_url = str(item_url or "").strip()
    normalized_item_identity_key = str(item_identity_key or "").strip() or None
    storage_item_url = _page_observation_storage_key(
        item_url=normalized_item_url,
        item_identity_key=normalized_item_identity_key,
    )
    normalized_item_title = str(item_title or "").strip()
    normalized_fingerprint = str(content_fingerprint or "").strip()
    normalized_observed_at_kst = str(observed_at_kst or "").strip() or now_kst().strftime("%Y-%m-%d %H:%M KST")
    normalized_published_at_kst = str(published_at_kst or "").strip() or None
    normalized_updated_at_kst = str(updated_at_kst or "").strip() or None
    normalized_structured_payload = structured_payload if isinstance(structured_payload, dict) else {}
    structured_payload_json = json.dumps(normalized_structured_payload, ensure_ascii=False, sort_keys=True)
    normalized_source_specific_identity = source_specific_identity if isinstance(source_specific_identity, dict) else {}
    source_specific_identity_json = json.dumps(normalized_source_specific_identity, ensure_ascii=False, sort_keys=True)

    with _get_connection() as conn:
        _ensure_page_observations_table(conn)
        row = conn.execute(
            """
            SELECT item_title, previous_item_title, title_change_observed_at_kst,
                   content_fingerprint, structured_payload_json, source_specific_identity_json,
                   first_seen_at_kst, last_seen_at_kst, last_published_at_kst, last_updated_at_kst
            FROM page_observations
            WHERE source_name = ? AND page_name = ? AND item_url = ?
            """,
            (normalized_source_name, normalized_page_name, storage_item_url),
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO page_observations (
                    source_name, page_name, item_url, item_identity_key, item_title, previous_item_title,
                    title_change_observed_at_kst, content_fingerprint, structured_payload_json,
                    source_specific_identity_json, first_seen_at_kst, last_seen_at_kst,
                    last_published_at_kst, last_updated_at_kst
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_source_name,
                    normalized_page_name,
                    storage_item_url,
                    normalized_item_identity_key,
                    normalized_item_title,
                    None,
                    None,
                    normalized_fingerprint,
                    structured_payload_json,
                    source_specific_identity_json,
                    normalized_observed_at_kst,
                    normalized_observed_at_kst,
                    normalized_published_at_kst,
                    normalized_updated_at_kst,
                ),
            )
            return PageObservationDecision(
                freshness_state="new_item",
                is_new_item=True,
                is_substantive_update=False,
                is_resurfaced_old_news=False,
                changed_fields=[],
                first_seen_at_kst=normalized_observed_at_kst,
                last_seen_at_kst=normalized_observed_at_kst,
            )

        previous_fingerprint = str(row["content_fingerprint"]).strip()
        previous_item_title = str(row["item_title"]).strip()
        stored_previous_item_title = str(row["previous_item_title"]).strip() if row["previous_item_title"] else None
        stored_title_change_observed_at_kst = (
            str(row["title_change_observed_at_kst"]).strip() if row["title_change_observed_at_kst"] else None
        )
        previous_structured_payload = json.loads(row["structured_payload_json"] or "{}")
        first_seen_at_kst = str(row["first_seen_at_kst"]).strip()
        last_seen_at_kst = str(row["last_seen_at_kst"]).strip()
        previous_published_at_kst = str(row["last_published_at_kst"]).strip() if row["last_published_at_kst"] else None
        previous_updated_at_kst = str(row["last_updated_at_kst"]).strip() if row["last_updated_at_kst"] else None
        title_changed = bool(normalized_item_title and normalized_item_title != previous_item_title)

        changed = previous_fingerprint != normalized_fingerprint
        if not changed and normalized_updated_at_kst and previous_updated_at_kst and normalized_updated_at_kst != previous_updated_at_kst:
            changed = True
        if not changed and title_changed:
            changed = True

        candidate_reference = (
            _parse_kst_text(previous_updated_at_kst)
            or _parse_kst_text(previous_published_at_kst)
            or _parse_kst_text(first_seen_at_kst)
        )
        observed_at = _parse_kst_text(normalized_observed_at_kst) or now_kst()
        is_resurfaced_old_news = not changed and candidate_reference is not None and candidate_reference < observed_at - timedelta(hours=24)
        freshness_state = "substantive_update" if changed else "resurfaced_old_news" if is_resurfaced_old_news else "unchanged"
        changed_fields = compute_changed_fields(normalized_structured_payload, previous_structured_payload) if changed else []
        current_previous_item_title = previous_item_title if title_changed else stored_previous_item_title
        current_title_change_observed_at_kst = normalized_observed_at_kst if title_changed else stored_title_change_observed_at_kst

        conn.execute(
            """
            UPDATE page_observations
            SET item_title = ?,
                previous_item_title = ?,
                title_change_observed_at_kst = ?,
                content_fingerprint = ?,
                structured_payload_json = ?,
                source_specific_identity_json = ?,
                last_seen_at_kst = ?,
                last_published_at_kst = ?,
                last_updated_at_kst = ?
            WHERE source_name = ? AND page_name = ? AND item_url = ?
            """,
            (
                normalized_item_title,
                current_previous_item_title,
                current_title_change_observed_at_kst,
                normalized_fingerprint,
                structured_payload_json,
                source_specific_identity_json,
                normalized_observed_at_kst,
                normalized_published_at_kst or previous_published_at_kst,
                normalized_updated_at_kst or previous_updated_at_kst,
                normalized_source_name,
                normalized_page_name,
                storage_item_url,
            ),
        )
        return PageObservationDecision(
            freshness_state=freshness_state,
            is_new_item=False,
            is_substantive_update=changed,
            is_resurfaced_old_news=is_resurfaced_old_news,
            changed_fields=changed_fields,
            first_seen_at_kst=first_seen_at_kst,
            last_seen_at_kst=normalized_observed_at_kst,
            previous_fingerprint=previous_fingerprint,
            previous_item_title=previous_item_title if title_changed else None,
            title_change_observed_at_kst=current_title_change_observed_at_kst if title_changed else None,
        )


def record_competitor_universe_observation(
    *,
    competitor: str,
    asset: str,
    indication: str,
    stage_status: str | None,
    region: str | None,
    primary_source_url: str,
    source_label: str | None,
    source_type: str | None,
    aliases: list[str] | None,
    target_moa: str | None,
    layer: str | None,
    provenance_score: float | None,
    content_fingerprint: str,
    observed_at_kst: str,
) -> None:
    normalized_competitor = str(competitor or "").strip() or "-"
    normalized_asset = str(asset or "").strip() or "-"
    normalized_indication = str(indication or "").strip() or "-"
    normalized_primary_source_url = str(primary_source_url or "").strip() or "-"
    aliases_json = json.dumps(list(aliases or []), ensure_ascii=False)
    with _get_connection() as conn:
        _ensure_competitor_universe_observations_table(conn)
        row = conn.execute(
            """
            SELECT first_seen_at_kst
            FROM competitor_universe_observations
            WHERE competitor = ? AND asset = ? AND indication = ? AND primary_source_url = ?
            """,
            (
                normalized_competitor,
                normalized_asset,
                normalized_indication,
                normalized_primary_source_url,
            ),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO competitor_universe_observations (
                    competitor, asset, indication, stage_status, region, primary_source_url, source_label, source_type,
                    content_fingerprint, aliases_json, target_moa, layer, provenance_score,
                    first_seen_at_kst, last_seen_at_kst, last_verified_at_kst
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_competitor,
                    normalized_asset,
                    normalized_indication,
                    str(stage_status or "").strip() or None,
                    str(region or "").strip() or None,
                    normalized_primary_source_url,
                    str(source_label or "").strip() or None,
                    str(source_type or "").strip() or None,
                    str(content_fingerprint or "").strip(),
                    aliases_json,
                    str(target_moa or "").strip() or None,
                    str(layer or "").strip() or None,
                    None if provenance_score is None else float(provenance_score),
                    observed_at_kst,
                    observed_at_kst,
                    observed_at_kst,
                ),
            )
            return
        conn.execute(
            """
            UPDATE competitor_universe_observations
            SET stage_status = ?,
                region = ?,
                source_label = ?,
                source_type = ?,
                content_fingerprint = ?,
                aliases_json = ?,
                target_moa = ?,
                layer = ?,
                provenance_score = ?,
                last_seen_at_kst = ?,
                last_verified_at_kst = ?
            WHERE competitor = ? AND asset = ? AND indication = ? AND primary_source_url = ?
            """,
            (
                str(stage_status or "").strip() or None,
                str(region or "").strip() or None,
                str(source_label or "").strip() or None,
                str(source_type or "").strip() or None,
                str(content_fingerprint or "").strip(),
                aliases_json,
                str(target_moa or "").strip() or None,
                str(layer or "").strip() or None,
                None if provenance_score is None else float(provenance_score),
                observed_at_kst,
                observed_at_kst,
                normalized_competitor,
                normalized_asset,
                normalized_indication,
                normalized_primary_source_url,
            ),
        )


def load_competitor_universe_observations() -> list[dict[str, Any]]:
    with _get_connection() as conn:
        _ensure_competitor_universe_observations_table(conn)
        rows = conn.execute(
            """
            SELECT competitor, asset, indication, stage_status, region, primary_source_url, source_label, source_type,
                   content_fingerprint, aliases_json, target_moa, layer, provenance_score,
                   first_seen_at_kst, last_seen_at_kst, last_verified_at_kst
            FROM competitor_universe_observations
            ORDER BY last_verified_at_kst DESC, competitor ASC, asset ASC, indication ASC
            """
        ).fetchall()
    observations: list[dict[str, Any]] = []
    for row in rows:
        observations.append(
            {
                "competitor": str(row["competitor"]).strip(),
                "asset": str(row["asset"]).strip(),
                "indication": str(row["indication"]).strip(),
                "stage_status": str(row["stage_status"]).strip() if row["stage_status"] else None,
                "region": str(row["region"]).strip() if row["region"] else None,
                "primary_source_url": str(row["primary_source_url"]).strip(),
                "source_label": str(row["source_label"]).strip() if row["source_label"] else None,
                "source_type": str(row["source_type"]).strip() if row["source_type"] else None,
                "content_fingerprint": str(row["content_fingerprint"]).strip(),
                "aliases": json.loads(row["aliases_json"] or "[]"),
                "target_moa": str(row["target_moa"]).strip() if row["target_moa"] else None,
                "layer": str(row["layer"]).strip() if row["layer"] else None,
                "provenance_score": float(row["provenance_score"]) if row["provenance_score"] is not None else None,
                "first_seen_at_kst": str(row["first_seen_at_kst"]).strip(),
                "last_seen_at_kst": str(row["last_seen_at_kst"]).strip(),
                "last_verified_at_kst": str(row["last_verified_at_kst"]).strip(),
            }
        )
    return observations


def record_stage2_verification_run_start(
    *,
    trace_id: str,
    room_key: str | None,
    run_started_at_kst: str,
    used_search_verify: bool,
    gap_target_count: int,
) -> None:
    with _get_connection() as conn:
        _ensure_stage2_verification_runs_table(conn)
        _ensure_column(conn, "stage2_verification_runs", "reused_evidence_count", "INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """
            INSERT OR REPLACE INTO stage2_verification_runs (
                trace_id, room_key, run_started_at_kst, stage2_status, used_search_verify, gap_target_count,
                evidence_count, backfill_count, discovered_confirmed_count, discovered_unverified_count,
                coverage_upgrade_count, reused_evidence_count, error_detail
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, NULL)
            """,
            (
                str(trace_id or "").strip() or "-",
                str(room_key or "").strip() or None,
                str(run_started_at_kst or "").strip() or now_kst().strftime("%Y-%m-%d %H:%M KST"),
                "started",
                1 if used_search_verify else 0,
                int(gap_target_count),
            ),
        )


def finalize_stage2_verification_run(
    *,
    trace_id: str,
    run_finished_at_kst: str,
    stage2_status: str,
    evidence_count: int = 0,
    backfill_count: int = 0,
    discovered_confirmed_count: int = 0,
    discovered_unverified_count: int = 0,
    coverage_upgrade_count: int = 0,
    reused_evidence_count: int = 0,
    error_detail: str | None = None,
) -> None:
    with _get_connection() as conn:
        _ensure_stage2_verification_runs_table(conn)
        _ensure_column(conn, "stage2_verification_runs", "reused_evidence_count", "INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """
            UPDATE stage2_verification_runs
            SET run_finished_at_kst = ?,
                stage2_status = ?,
                evidence_count = ?,
                backfill_count = ?,
                discovered_confirmed_count = ?,
                discovered_unverified_count = ?,
                coverage_upgrade_count = ?,
                reused_evidence_count = ?,
                error_detail = ?
            WHERE trace_id = ?
            """,
            (
                str(run_finished_at_kst or "").strip() or now_kst().strftime("%Y-%m-%d %H:%M KST"),
                str(stage2_status or "").strip() or "failed",
                int(evidence_count),
                int(backfill_count),
                int(discovered_confirmed_count),
                int(discovered_unverified_count),
                int(coverage_upgrade_count),
                int(reused_evidence_count),
                str(error_detail or "").strip() or None,
                str(trace_id or "").strip() or "-",
            ),
        )


def persist_stage2_verification_success(
    *,
    trace_id: str,
    run_finished_at_kst: str,
    stage2_status: str,
    evidence_catalog: list[dict[str, Any]],
    provenance_rows: list[dict[str, Any]],
    backfill_count: int,
    discovered_confirmed_count: int,
    discovered_unverified_count: int,
    coverage_upgrade_count: int,
    reused_evidence_count: int = 0,
) -> None:
    inserted_at_kst = now_kst().strftime("%Y-%m-%d %H:%M KST")
    with _get_connection() as conn:
        _ensure_stage2_verification_runs_table(conn)
        _ensure_stage2_search_evidence_table(conn)
        _ensure_stage2_finding_provenance_table(conn)
        _ensure_column(conn, "stage2_verification_runs", "reused_evidence_count", "INTEGER NOT NULL DEFAULT 0")
        for evidence in evidence_catalog:
            conn.execute(
                """
                INSERT OR REPLACE INTO stage2_search_evidence (
                    trace_id, evidence_id, topic, source_type, source_name, source_url, title,
                    published_at_kst, updated_at_kst, confidence, excerpt, evidence_kind,
                    confirms_fields_json, entity, asset, indication, region, inserted_at_kst
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(trace_id or "").strip() or "-",
                    str(evidence.get("evidence_id") or "").strip() or "-",
                    str(evidence.get("topic") or "").strip() or "-",
                    str(evidence.get("source_type") or "").strip() or "official",
                    str(evidence.get("source_name") or "").strip() or "-",
                    str(evidence.get("source_url") or "").strip() or "-",
                    str(evidence.get("title") or "").strip() or "-",
                    str(evidence.get("published_at_kst") or "").strip() or None,
                    str(evidence.get("updated_at_kst") or "").strip() or None,
                    float(evidence.get("confidence") or 0.0),
                    str(evidence.get("excerpt") or "").strip() or "",
                    str(evidence.get("evidence_kind") or "").strip() or "field_confirmation",
                    json.dumps(list(evidence.get("confirms_fields") or []), ensure_ascii=False),
                    str(evidence.get("entity") or "").strip() or None,
                    str(evidence.get("asset") or "").strip() or None,
                    str(evidence.get("indication") or "").strip() or None,
                    str(evidence.get("region") or "").strip() or None,
                    inserted_at_kst,
                ),
            )
        for row in provenance_rows:
            conn.execute(
                """
                INSERT INTO stage2_finding_provenance (
                    trace_id, finding_identity, candidate_id, field_name, field_value, action_type,
                    source_type, source_name, source_url, evidence_id, provenance_strength, note, recorded_at_kst
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(trace_id or "").strip() or "-",
                    str(row.get("finding_identity") or "").strip() or "-",
                    str(row.get("candidate_id") or "").strip() or None,
                    str(row.get("field_name") or "").strip() or "-",
                    str(row.get("field_value") or "").strip() or None,
                    str(row.get("action_type") or "").strip() or "filled_blank",
                    str(row.get("source_type") or "").strip() or "official",
                    str(row.get("source_name") or "").strip() or "-",
                    str(row.get("source_url") or "").strip() or None,
                    str(row.get("evidence_id") or "").strip() or None,
                    str(row.get("provenance_strength") or "").strip() or "unknown",
                    str(row.get("note") or "").strip() or "",
                    str(row.get("recorded_at_kst") or "").strip() or inserted_at_kst,
                ),
            )
        conn.execute(
            """
            UPDATE stage2_verification_runs
            SET run_finished_at_kst = ?,
                stage2_status = ?,
                evidence_count = ?,
                backfill_count = ?,
                discovered_confirmed_count = ?,
                discovered_unverified_count = ?,
                coverage_upgrade_count = ?,
                reused_evidence_count = ?,
                error_detail = NULL
            WHERE trace_id = ?
            """,
            (
                str(run_finished_at_kst or "").strip() or inserted_at_kst,
                str(stage2_status or "").strip() or "success",
                len(evidence_catalog),
                int(backfill_count),
                int(discovered_confirmed_count),
                int(discovered_unverified_count),
                int(coverage_upgrade_count),
                int(reused_evidence_count),
                str(trace_id or "").strip() or "-",
            ),
        )


def list_stage2_verification_runs(
    trace_id: str | None = None,
    *,
    room_key: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    with _get_connection() as conn:
        _ensure_stage2_verification_runs_table(conn)
        _ensure_column(conn, "stage2_verification_runs", "reused_evidence_count", "INTEGER NOT NULL DEFAULT 0")
        if str(trace_id or "").strip():
            rows = conn.execute(
                """
                SELECT trace_id, room_key, run_started_at_kst, run_finished_at_kst, stage2_status,
                       used_search_verify, gap_target_count, evidence_count, backfill_count,
                       discovered_confirmed_count, discovered_unverified_count, coverage_upgrade_count,
                       reused_evidence_count, error_detail
                FROM stage2_verification_runs
                WHERE trace_id = ?
                ORDER BY run_started_at_kst DESC
                """,
                (str(trace_id).strip(),),
            ).fetchall()
        else:
            query = """
                SELECT trace_id, room_key, run_started_at_kst, run_finished_at_kst, stage2_status,
                       used_search_verify, gap_target_count, evidence_count, backfill_count,
                       discovered_confirmed_count, discovered_unverified_count, coverage_upgrade_count,
                       reused_evidence_count, error_detail
                FROM stage2_verification_runs
            """
            params: list[Any] = []
            if str(room_key or "").strip():
                query += " WHERE room_key = ?"
                params.append(str(room_key).strip())
            query += " ORDER BY run_started_at_kst DESC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(int(limit))
            rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def list_stage2_search_evidence(
    trace_id: str | None = None,
    *,
    room_key: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    with _get_connection() as conn:
        _ensure_stage2_search_evidence_table(conn)
        if str(trace_id or "").strip():
            rows = conn.execute(
                """
                SELECT trace_id, evidence_id, topic, source_type, source_name, source_url, title,
                       published_at_kst, updated_at_kst, confidence, excerpt, evidence_kind,
                       confirms_fields_json, entity, asset, indication, region, inserted_at_kst
                FROM stage2_search_evidence
                WHERE trace_id = ?
                ORDER BY inserted_at_kst DESC, evidence_id ASC
                """,
                (str(trace_id).strip(),),
            ).fetchall()
        else:
            query = """
                SELECT e.trace_id, e.evidence_id, e.topic, e.source_type, e.source_name, e.source_url, e.title,
                       e.published_at_kst, e.updated_at_kst, e.confidence, e.excerpt, e.evidence_kind,
                       e.confirms_fields_json, e.entity, e.asset, e.indication, e.region, e.inserted_at_kst
                FROM stage2_search_evidence e
            """
            params: list[Any] = []
            if str(room_key or "").strip():
                query += " JOIN stage2_verification_runs r ON r.trace_id = e.trace_id WHERE r.room_key = ?"
                params.append(str(room_key).strip())
            query += " ORDER BY e.inserted_at_kst DESC, e.evidence_id ASC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(int(limit))
            rows = conn.execute(query, tuple(params)).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["confirms_fields"] = json.loads(item.pop("confirms_fields_json") or "[]")
        items.append(item)
    return items


def list_stage2_finding_provenance(trace_id: str | None = None) -> list[dict[str, Any]]:
    with _get_connection() as conn:
        _ensure_stage2_finding_provenance_table(conn)
        if str(trace_id or "").strip():
            rows = conn.execute(
                """
                SELECT trace_id, finding_identity, candidate_id, field_name, field_value, action_type,
                       source_type, source_name, source_url, evidence_id, provenance_strength, note, recorded_at_kst
                FROM stage2_finding_provenance
                WHERE trace_id = ?
                ORDER BY recorded_at_kst ASC, finding_identity ASC
                """,
                (str(trace_id).strip(),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT trace_id, finding_identity, candidate_id, field_name, field_value, action_type,
                       source_type, source_name, source_url, evidence_id, provenance_strength, note, recorded_at_kst
                FROM stage2_finding_provenance
                ORDER BY recorded_at_kst ASC, finding_identity ASC
                """
            ).fetchall()
    return [dict(row) for row in rows]


def persist_hanall_run_snapshot(
    *,
    trace_id: str,
    room_key: str | None,
    created_at_kst: str,
    stage1_candidate_summary: dict[str, Any],
    stage1_output: dict[str, Any],
    merged_stage1_output: dict[str, Any],
    stage2_output: dict[str, Any] | None,
    search_memory: dict[str, Any] | None,
    ranked_issues: list[dict[str, Any]],
    summary_lines: list[str],
    final_text: str,
    debug_meta: dict[str, Any] | None = None,
) -> None:
    with _get_connection() as conn:
        _ensure_hanall_run_snapshots_table(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO hanall_run_snapshots (
                trace_id, room_key, created_at_kst, stage1_candidate_summary_json, stage1_output_json,
                merged_stage1_output_json, stage2_output_json, search_memory_json, ranked_issues_json,
                summary_lines_json, final_text, debug_meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(trace_id or "").strip() or "-",
                str(room_key or "").strip() or None,
                str(created_at_kst or "").strip() or now_kst().strftime("%Y-%m-%d %H:%M KST"),
                json.dumps(stage1_candidate_summary or {}, ensure_ascii=False),
                json.dumps(stage1_output or {}, ensure_ascii=False),
                json.dumps(merged_stage1_output or {}, ensure_ascii=False),
                json.dumps(stage2_output, ensure_ascii=False) if isinstance(stage2_output, dict) else None,
                json.dumps(search_memory or {}, ensure_ascii=False),
                json.dumps(ranked_issues or [], ensure_ascii=False),
                json.dumps(list(summary_lines or []), ensure_ascii=False),
                str(final_text or "").strip(),
                json.dumps(debug_meta or {}, ensure_ascii=False),
            ),
        )


def get_hanall_run_snapshot(trace_id: str) -> dict[str, Any] | None:
    normalized_trace_id = str(trace_id or "").strip()
    if not normalized_trace_id:
        return None
    with _get_connection() as conn:
        _ensure_hanall_run_snapshots_table(conn)
        row = conn.execute(
            """
            SELECT trace_id, room_key, created_at_kst, stage1_candidate_summary_json, stage1_output_json,
                   merged_stage1_output_json, stage2_output_json, search_memory_json, ranked_issues_json,
                   summary_lines_json, final_text, debug_meta_json
            FROM hanall_run_snapshots
            WHERE trace_id = ?
            """,
            (normalized_trace_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "trace_id": str(row["trace_id"]).strip(),
        "room_key": str(row["room_key"]).strip() if row["room_key"] else None,
        "created_at_kst": str(row["created_at_kst"]).strip(),
        "stage1_candidate_summary": json.loads(row["stage1_candidate_summary_json"] or "{}"),
        "stage1_output": json.loads(row["stage1_output_json"] or "{}"),
        "merged_stage1_output": json.loads(row["merged_stage1_output_json"] or "{}"),
        "stage2_output": json.loads(row["stage2_output_json"] or "{}") if row["stage2_output_json"] else None,
        "search_memory": json.loads(row["search_memory_json"] or "{}"),
        "ranked_issues": json.loads(row["ranked_issues_json"] or "[]"),
        "summary_lines": json.loads(row["summary_lines_json"] or "[]"),
        "final_text": str(row["final_text"]).strip(),
        "debug_meta": json.loads(row["debug_meta_json"] or "{}"),
    }


def list_hanall_run_snapshots(limit: int = 20, *, room_key: str | None = None) -> list[dict[str, Any]]:
    with _get_connection() as conn:
        _ensure_hanall_run_snapshots_table(conn)
        query = """
            SELECT trace_id, room_key, created_at_kst, stage1_candidate_summary_json, stage1_output_json,
                   merged_stage1_output_json, stage2_output_json, search_memory_json, ranked_issues_json,
                   summary_lines_json, final_text, debug_meta_json
            FROM hanall_run_snapshots
        """
        params: list[Any] = []
        if str(room_key or "").strip():
            query += " WHERE room_key = ?"
            params.append(str(room_key).strip())
        query += " ORDER BY created_at_kst DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = conn.execute(query, tuple(params)).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "trace_id": str(row["trace_id"]).strip(),
                "room_key": str(row["room_key"]).strip() if row["room_key"] else None,
                "created_at_kst": str(row["created_at_kst"]).strip(),
                "stage1_candidate_summary": json.loads(row["stage1_candidate_summary_json"] or "{}"),
                "stage1_output": json.loads(row["stage1_output_json"] or "{}"),
                "merged_stage1_output": json.loads(row["merged_stage1_output_json"] or "{}"),
                "stage2_output": json.loads(row["stage2_output_json"] or "{}") if row["stage2_output_json"] else None,
                "search_memory": json.loads(row["search_memory_json"] or "{}"),
                "ranked_issues": json.loads(row["ranked_issues_json"] or "[]"),
                "summary_lines": json.loads(row["summary_lines_json"] or "[]"),
                "final_text": str(row["final_text"]).strip(),
                "debug_meta": json.loads(row["debug_meta_json"] or "{}"),
            }
        )
    return items


def persist_hanall_brief_snapshot(
    *,
    cache_date_kst: str,
    source_room_key: str | None,
    trace_id: str | None,
    created_at_kst: str,
    public_text: str,
    detailed_text: str,
    raw_output_text: str | None = None,
) -> None:
    normalized_cache_date = str(cache_date_kst or "").strip() or now_kst().strftime("%Y-%m-%d")
    with _get_connection() as conn:
        _ensure_hanall_brief_snapshots_table(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO hanall_brief_snapshots (
                cache_date_kst, source_room_key, trace_id, created_at_kst, public_text, detailed_text, raw_output_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_cache_date,
                str(source_room_key or "").strip() or None,
                str(trace_id or "").strip() or None,
                str(created_at_kst or "").strip() or now_kst().strftime("%Y-%m-%d %H:%M KST"),
                str(public_text or "").strip(),
                str(detailed_text or "").strip(),
                str(raw_output_text or "").strip() or None,
            ),
        )


def get_hanall_brief_snapshot(*, cache_date_kst: str) -> dict[str, Any] | None:
    normalized_cache_date = str(cache_date_kst or "").strip()
    if not normalized_cache_date:
        return None
    with _get_connection() as conn:
        _ensure_hanall_brief_snapshots_table(conn)
        row = conn.execute(
            """
            SELECT cache_date_kst, source_room_key, trace_id, created_at_kst, public_text, detailed_text, raw_output_text
            FROM hanall_brief_snapshots
            WHERE cache_date_kst = ?
            """,
            (normalized_cache_date,),
        ).fetchone()
    if row is None:
        return None
    return {
        "cache_date_kst": str(row["cache_date_kst"]).strip(),
        "source_room_key": str(row["source_room_key"]).strip() if row["source_room_key"] else None,
        "trace_id": str(row["trace_id"]).strip() if row["trace_id"] else None,
        "created_at_kst": str(row["created_at_kst"]).strip(),
        "public_text": str(row["public_text"]).strip(),
        "detailed_text": str(row["detailed_text"]).strip(),
        "raw_output_text": str(row["raw_output_text"]).strip() if row["raw_output_text"] else None,
    }


def reset_inflight_outbox_messages() -> int:
    with _get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE outbox_messages
            SET status = ?
            WHERE status = ?
            """,
            (OUTBOX_STATUS_PENDING, OUTBOX_STATUS_INFLIGHT),
        )
    updated = int(cursor.rowcount)
    if updated:
        logger.info("outbox inflight messages reset count=%s", updated)
    return updated


def has_processed_message(room_key: str, log_id: str) -> bool:
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE room_key = ? AND log_id = ?",
            (room_key, log_id),
        ).fetchone()
    return row is not None


def save_processed_message(
    room_key: str,
    log_id: str,
    sender: str,
    message: str,
    received_at: str,
) -> None:
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_messages
            (room_key, log_id, sender, message, received_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (room_key, log_id, sender, message, received_at, now_kst().isoformat()),
        )


def is_video_recently_processed(room_key: str, video_id: str, dedupe_hours: int = 12) -> bool:
    with _get_connection() as conn:
        row = conn.execute(
            """
            SELECT processed_at
            FROM processed_videos
            WHERE room_key = ? AND video_id = ?
            """,
            (room_key, video_id),
        ).fetchone()
    if row is None:
        return False
    processed_at = str(row["processed_at"]).strip()
    if not processed_at:
        return False
    return datetime.fromisoformat(processed_at) >= now_kst() - timedelta(hours=dedupe_hours)


def save_processed_video(room_key: str, video_id: str, source_url: str) -> None:
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO processed_videos (room_key, video_id, source_url, processed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(room_key, video_id) DO UPDATE SET
                source_url = excluded.source_url,
                processed_at = excluded.processed_at
            """,
            (room_key, video_id, source_url, now_kst().isoformat()),
        )


def enqueue_outbox_message(
    room_key: str,
    message_text: str,
    source_type: str,
    trace_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    channel_id = _get_room_channel_id(room_key)
    with _get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO outbox_messages
            (room_key, channel_id, message_text, source_type, trace_id, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room_key,
                channel_id,
                message_text,
                source_type,
                trace_id,
                json.dumps(meta or {}, ensure_ascii=False),
                now_kst().isoformat(),
            ),
        )
        outbox_id = int(cursor.lastrowid)
    logger.info("outbox enqueued id=%s room_key=%s source_type=%s", outbox_id, room_key, source_type)
    return outbox_id


def pull_pending_outbox_messages(
    room_key: str | None = None,
    channel_id: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    stale_before = (now_kst() - timedelta(minutes=OUTBOX_STALE_INFLIGHT_MINUTES)).isoformat()
    query = """
        SELECT id, room_key, channel_id, message_text, source_type, trace_id, retry_count, meta_json, created_at, last_attempt_at
        FROM outbox_messages
        WHERE (
            status = ?
            OR (status = ? AND COALESCE(last_attempt_at, created_at) <= ?)
        )
    """
    params: list[Any] = [OUTBOX_STATUS_PENDING, OUTBOX_STATUS_INFLIGHT, stale_before]
    resolved_room_key = room_key
    if not resolved_room_key and channel_id:
        resolved_room_key, _ = resolve_room(None, channel_id)
    if resolved_room_key:
        query += " AND room_key = ?"
        params.append(resolved_room_key)
    query += " ORDER BY id ASC LIMIT ?"
    params.append(max(1, min(limit, 100)))

    with _get_connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        message_ids = [int(row["id"]) for row in rows]
        if message_ids:
            placeholders = ",".join(["?"] * len(message_ids))
            conn.execute(
                f"""
                UPDATE outbox_messages
                SET status = ?, last_attempt_at = ?
                WHERE id IN ({placeholders}) AND status IN (?, ?)
                """,
                (
                    OUTBOX_STATUS_INFLIGHT,
                    now_kst().isoformat(),
                    *message_ids,
                    OUTBOX_STATUS_PENDING,
                    OUTBOX_STATUS_INFLIGHT,
                ),
            )

    return [_serialize_outbox_row(row) for row in rows]


def ack_outbox_messages(
    message_ids: list[Any],
    success: bool = True,
    increment_retry: bool = False,
) -> int:
    normalized_ids = [int(item) for item in message_ids if str(item).isdigit()]
    if not normalized_ids:
        return 0
    placeholders = ",".join(["?"] * len(normalized_ids))
    status = OUTBOX_STATUS_SENT if success else OUTBOX_STATUS_PENDING
    with _get_connection() as conn:
        if success:
            cursor = conn.execute(
                f"""
                UPDATE outbox_messages
                SET status = ?, sent_at = ?
                WHERE id IN ({placeholders})
                """,
                (status, now_kst().isoformat(), *normalized_ids),
            )
        elif increment_retry:
            cursor = conn.execute(
                f"""
                UPDATE outbox_messages
                SET status = ?, retry_count = retry_count + 1
                WHERE id IN ({placeholders})
                """,
                (status, *normalized_ids),
            )
        else:
            cursor = conn.execute(
                f"""
                UPDATE outbox_messages
                SET status = ?
                WHERE id IN ({placeholders})
                """,
                (status, *normalized_ids),
            )
    return int(cursor.rowcount)


def count_outbox_messages(status: str | None = None) -> int:
    query = "SELECT COUNT(1) AS count FROM outbox_messages"
    params: tuple[Any, ...] = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    with _get_connection() as conn:
        row = conn.execute(query, params).fetchone()
    return int(row["count"] if row else 0)


def record_job_run(job_name: str, status: str, detail: str, trace_id: str | None = None) -> None:
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO job_runs (job_name, status, detail, trace_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_name, status, detail, trace_id, now_kst().isoformat()),
        )


def record_scheduler_event(
    event_type: str,
    detail: str,
    trace_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_events (event_type, detail, trace_id, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_type,
                detail,
                trace_id,
                json.dumps(meta or {}, ensure_ascii=False),
                now_kst().isoformat(),
            ),
        )


def list_scheduler_events(limit: int = 10) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(int(limit), 100))
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, event_type, detail, trace_id, meta_json, created_at
            FROM scheduler_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (normalized_limit,),
        ).fetchall()

    return [
        {
            "id": int(row["id"]),
            "event_type": str(row["event_type"]).strip(),
            "detail": str(row["detail"] or "").strip(),
            "trace_id": str(row["trace_id"]).strip() if row["trace_id"] else None,
            "meta": json.loads(row["meta_json"] or "{}"),
            "created_at": str(row["created_at"]).strip(),
        }
        for row in rows
    ]


def record_polling_heartbeat(
    event_type: str,
    *,
    trace_id: str | None = None,
    checked_at: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    normalized_event_type = str(event_type or "").strip() or "unknown"
    normalized_checked_at = str(checked_at or "").strip() or now_kst().isoformat()
    retention_before = (now_kst() - timedelta(days=POLLING_HEARTBEAT_RETENTION_DAYS)).isoformat()
    with _get_connection() as conn:
        conn.execute(
            "DELETE FROM polling_heartbeats WHERE checked_at < ?",
            (retention_before,),
        )
        conn.execute(
            """
            INSERT INTO polling_heartbeats (event_type, trace_id, checked_at, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                normalized_event_type,
                str(trace_id).strip() if trace_id else None,
                normalized_checked_at,
                json.dumps(meta or {}, ensure_ascii=False),
                now_kst().isoformat(),
            ),
        )


def list_polling_heartbeats(limit: int = 10, event_type: str | None = None) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(int(limit), 100))
    normalized_event_type = str(event_type or "").strip() or None
    query = """
        SELECT id, event_type, trace_id, checked_at, meta_json, created_at
        FROM polling_heartbeats
    """
    params: list[Any] = []
    if normalized_event_type:
        query += " WHERE event_type = ?"
        params.append(normalized_event_type)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(normalized_limit)

    with _get_connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    return [
        {
            "id": int(row["id"]),
            "event_type": str(row["event_type"]).strip(),
            "trace_id": str(row["trace_id"]).strip() if row["trace_id"] else None,
            "checked_at": str(row["checked_at"]).strip(),
            "meta": json.loads(row["meta_json"] or "{}"),
            "created_at": str(row["created_at"]).strip(),
        }
        for row in rows
    ]


def save_room_target(room_key: str, room_name: str | None = None, channel_id: str | None = None) -> None:
    normalized_room_key = str(room_key).strip()
    normalized_room_name = str(room_name or "").strip() or None
    normalized_channel_id = str(channel_id or "").strip() or None
    if not normalized_room_key or (not normalized_room_name and not normalized_channel_id):
        return

    with _get_connection() as conn:
        existing = conn.execute(
            """
            SELECT channel_id, room_name
            FROM room_targets
            WHERE room_key = ?
            """,
            (normalized_room_key,),
        ).fetchone()
        resolved_channel_id = normalized_channel_id or (
            str(existing["channel_id"]).strip() if existing and existing["channel_id"] else None
        )
        resolved_room_name = normalized_room_name or (
            str(existing["room_name"]).strip() if existing and existing["room_name"] else None
        )
        conn.execute(
            """
            INSERT INTO room_targets (room_key, channel_id, room_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(room_key) DO UPDATE SET
                channel_id = excluded.channel_id,
                room_name = excluded.room_name,
                updated_at = excluded.updated_at
            """,
            (
                normalized_room_key,
                resolved_channel_id,
                resolved_room_name,
                now_kst().isoformat(),
            ),
        )


def get_room_target_snapshot(room_key: str) -> dict[str, Any] | None:
    normalized_room_key = str(room_key).strip()
    if not normalized_room_key:
        return None
    with _get_connection() as conn:
        row = conn.execute(
            """
            SELECT room_key, channel_id, room_name, updated_at
            FROM room_targets
            WHERE room_key = ?
            """,
            (normalized_room_key,),
        ).fetchone()
    if row is None:
        return None
    return {
        "room_key": str(row["room_key"]).strip(),
        "channel_id": str(row["channel_id"]).strip() if row["channel_id"] else "",
        "room_name": str(row["room_name"]).strip() if row["room_name"] else "",
        "updated_at": str(row["updated_at"]).strip(),
    }


def register_delivery_dedupe(
    *,
    dedupe_key: str,
    room_key: str,
    source_type: str,
    trace_id: str,
    ttl_seconds: int,
) -> bool:
    normalized_key = str(dedupe_key).strip()
    if not normalized_key:
        return True
    created_at = now_kst()
    expires_at = created_at + timedelta(seconds=max(60, ttl_seconds))
    with _get_connection() as conn:
        conn.execute(
            "DELETE FROM delivery_dedupes WHERE expires_at <= ?",
            (created_at.isoformat(),),
        )
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO delivery_dedupes
            (dedupe_key, room_key, source_type, trace_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_key,
                room_key,
                source_type,
                trace_id,
                created_at.isoformat(),
                expires_at.isoformat(),
            ),
        )
    inserted = int(cursor.rowcount) == 1
    if not inserted:
        logger.info("delivery dedupe hit room_key=%s source_type=%s dedupe_key=%s", room_key, source_type, normalized_key)
    return inserted


def register_admin_alert_attempt(
    *,
    room_key: str,
    feature_key: str,
    throttle_seconds: int,
) -> AdminAlertThrottleDecision:
    normalized_room_key = str(room_key).strip()
    normalized_feature_key = str(feature_key).strip()
    if not normalized_room_key or not normalized_feature_key:
        return AdminAlertThrottleDecision(should_send=True, suppressed_count=0)

    throttle_key = f"{normalized_room_key}:{normalized_feature_key}"
    now = now_kst()
    with _get_connection() as conn:
        row = conn.execute(
            """
            SELECT throttle_key, last_sent_at, suppressed_count
            FROM admin_alert_states
            WHERE throttle_key = ?
            """,
            (throttle_key,),
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO admin_alert_states
                (throttle_key, room_key, feature_key, last_seen_at, last_sent_at, suppressed_count)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    throttle_key,
                    normalized_room_key,
                    normalized_feature_key,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return AdminAlertThrottleDecision(should_send=True, suppressed_count=0)

        last_sent_at = datetime.fromisoformat(str(row["last_sent_at"]).strip())
        suppressed_count = int(row["suppressed_count"] or 0)
        throttle_window = timedelta(seconds=max(1, throttle_seconds))
        if now >= last_sent_at + throttle_window:
            conn.execute(
                """
                UPDATE admin_alert_states
                SET last_seen_at = ?, last_sent_at = ?, suppressed_count = 0
                WHERE throttle_key = ?
                """,
                (now.isoformat(), now.isoformat(), throttle_key),
            )
            return AdminAlertThrottleDecision(should_send=True, suppressed_count=suppressed_count)

        next_suppressed_count = suppressed_count + 1
        conn.execute(
            """
            UPDATE admin_alert_states
            SET last_seen_at = ?, suppressed_count = ?
            WHERE throttle_key = ?
            """,
            (now.isoformat(), next_suppressed_count, throttle_key),
        )
    logger.info(
        "admin alert throttled room_key=%s feature_key=%s throttle_seconds=%s suppressed_count=%s",
        normalized_room_key,
        normalized_feature_key,
        throttle_seconds,
        next_suppressed_count,
    )
    return AdminAlertThrottleDecision(should_send=False, suppressed_count=next_suppressed_count)
