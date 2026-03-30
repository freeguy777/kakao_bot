from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class AdminAlertThrottleDecision:
    should_send: bool
    suppressed_count: int


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
            """
        )
        _ensure_column(conn, "outbox_messages", "last_attempt_at", "TEXT")
        _ensure_column(conn, "outbox_messages", "sent_at", "TEXT")
        _ensure_column(conn, "scheduler_events", "detail", "TEXT")
        _ensure_column(conn, "scheduler_events", "trace_id", "TEXT")
        _ensure_column(conn, "scheduler_events", "meta_json", "TEXT")
    logger.info("database initialized path=%s", get_db_path())


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
