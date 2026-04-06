from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
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
