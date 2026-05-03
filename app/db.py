from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker


def create_sqlite_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 30
    return create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def configure_sqlite(engine: Engine) -> None:
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.begin() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL;"))
        connection.execute(text("PRAGMA foreign_keys=ON;"))
        connection.execute(text("PRAGMA synchronous=NORMAL;"))


def ensure_runtime_schema(engine: Engine) -> None:
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.begin() as connection:
        tables = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if "inbound_events" not in tables:
            return
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info('inbound_events')"))}
        if "processing_status" not in columns:
            connection.execute(
                text("ALTER TABLE inbound_events ADD COLUMN processing_status VARCHAR(32) NOT NULL DEFAULT 'processed'")
            )
        if "processing_started_at" not in columns:
            connection.execute(text("ALTER TABLE inbound_events ADD COLUMN processing_started_at DATETIME"))
        if "processed_at" not in columns:
            connection.execute(text("ALTER TABLE inbound_events ADD COLUMN processed_at DATETIME"))
        if "last_error_message" not in columns:
            connection.execute(text("ALTER TABLE inbound_events ADD COLUMN last_error_message TEXT"))
        connection.execute(
            text(
                "UPDATE inbound_events "
                "SET processing_status = COALESCE(processing_status, 'processed'), "
                "processed_at = COALESCE(processed_at, server_received_at) "
                "WHERE processing_status IS NULL OR processed_at IS NULL"
            )
        )
        _ensure_options_pcr_daily_summary_schema(connection)


def _ensure_options_pcr_daily_summary_schema(connection: Connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS options_pcr_daily_summary (
                id INTEGER NOT NULL PRIMARY KEY,
                date_us DATE NOT NULL,
                date_kst DATE NOT NULL,
                symbol VARCHAR(32) NOT NULL,
                close FLOAT,
                change_1d_pct FLOAT,
                pcr_oi_total FLOAT,
                pcr_vol_total FLOAT,
                put_oi_total INTEGER NOT NULL DEFAULT 0,
                call_oi_total INTEGER NOT NULL DEFAULT 0,
                put_vol_total INTEGER NOT NULL DEFAULT 0,
                call_vol_total INTEGER NOT NULL DEFAULT 0,
                total_option_volume INTEGER NOT NULL DEFAULT 0,
                total_option_oi INTEGER NOT NULL DEFAULT 0,
                short_dte_pcr_oi FLOAT,
                short_dte_pcr_vol FLOAT,
                data_quality_flag VARCHAR(64) NOT NULL DEFAULT 'ERROR',
                should_publish_public BOOLEAN NOT NULL DEFAULT 0,
                no_publish_reason VARCHAR(128),
                source VARCHAR(64) NOT NULL DEFAULT 'tradier',
                source_environment VARCHAR(32) NOT NULL DEFAULT 'sandbox',
                retrieved_at_utc DATETIME NOT NULL,
                oi_effective_date DATE,
                by_expiry_json TEXT,
                raw_response_json TEXT
            )
            """
        )
    )
    existing_columns = {row[1] for row in connection.execute(text("PRAGMA table_info('options_pcr_daily_summary')"))}
    expected_columns = {
        "date_us": "ALTER TABLE options_pcr_daily_summary ADD COLUMN date_us DATE",
        "date_kst": "ALTER TABLE options_pcr_daily_summary ADD COLUMN date_kst DATE",
        "symbol": "ALTER TABLE options_pcr_daily_summary ADD COLUMN symbol VARCHAR(32)",
        "close": "ALTER TABLE options_pcr_daily_summary ADD COLUMN close FLOAT",
        "change_1d_pct": "ALTER TABLE options_pcr_daily_summary ADD COLUMN change_1d_pct FLOAT",
        "pcr_oi_total": "ALTER TABLE options_pcr_daily_summary ADD COLUMN pcr_oi_total FLOAT",
        "pcr_vol_total": "ALTER TABLE options_pcr_daily_summary ADD COLUMN pcr_vol_total FLOAT",
        "put_oi_total": "ALTER TABLE options_pcr_daily_summary ADD COLUMN put_oi_total INTEGER NOT NULL DEFAULT 0",
        "call_oi_total": "ALTER TABLE options_pcr_daily_summary ADD COLUMN call_oi_total INTEGER NOT NULL DEFAULT 0",
        "put_vol_total": "ALTER TABLE options_pcr_daily_summary ADD COLUMN put_vol_total INTEGER NOT NULL DEFAULT 0",
        "call_vol_total": "ALTER TABLE options_pcr_daily_summary ADD COLUMN call_vol_total INTEGER NOT NULL DEFAULT 0",
        "total_option_volume": "ALTER TABLE options_pcr_daily_summary ADD COLUMN total_option_volume INTEGER NOT NULL DEFAULT 0",
        "total_option_oi": "ALTER TABLE options_pcr_daily_summary ADD COLUMN total_option_oi INTEGER NOT NULL DEFAULT 0",
        "short_dte_pcr_oi": "ALTER TABLE options_pcr_daily_summary ADD COLUMN short_dte_pcr_oi FLOAT",
        "short_dte_pcr_vol": "ALTER TABLE options_pcr_daily_summary ADD COLUMN short_dte_pcr_vol FLOAT",
        "data_quality_flag": (
            "ALTER TABLE options_pcr_daily_summary ADD COLUMN data_quality_flag VARCHAR(64) NOT NULL DEFAULT 'ERROR'"
        ),
        "should_publish_public": (
            "ALTER TABLE options_pcr_daily_summary ADD COLUMN should_publish_public BOOLEAN NOT NULL DEFAULT 0"
        ),
        "no_publish_reason": "ALTER TABLE options_pcr_daily_summary ADD COLUMN no_publish_reason VARCHAR(128)",
        "source": "ALTER TABLE options_pcr_daily_summary ADD COLUMN source VARCHAR(64) NOT NULL DEFAULT 'tradier'",
        "source_environment": (
            "ALTER TABLE options_pcr_daily_summary ADD COLUMN source_environment VARCHAR(32) NOT NULL DEFAULT 'sandbox'"
        ),
        "retrieved_at_utc": "ALTER TABLE options_pcr_daily_summary ADD COLUMN retrieved_at_utc DATETIME",
        "oi_effective_date": "ALTER TABLE options_pcr_daily_summary ADD COLUMN oi_effective_date DATE",
        "by_expiry_json": "ALTER TABLE options_pcr_daily_summary ADD COLUMN by_expiry_json TEXT",
        "raw_response_json": "ALTER TABLE options_pcr_daily_summary ADD COLUMN raw_response_json TEXT",
    }
    for column_name, ddl in expected_columns.items():
        if column_name not in existing_columns:
            connection.execute(text(ddl))
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_options_pcr_daily_summary_symbol_date_env "
            "ON options_pcr_daily_summary(symbol, date_us, source_environment)"
        )
    )


def commit_with_retry(session: Session, retries: int, delay_seconds: float) -> None:
    for attempt in range(retries + 1):
        try:
            session.commit()
            return
        except OperationalError as exc:
            session.rollback()
            if "database is locked" not in str(exc).lower() or attempt >= retries:
                raise
            time.sleep(delay_seconds * (attempt + 1))


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
