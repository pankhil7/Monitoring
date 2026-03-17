"""
Event store interface and SQLite-backed implementation.

Public interface:
  add(event)                   – persist one ErrorEvent
  get_events(service?, endpoint?, since, until) → list[ErrorEvent]
  get_recent_events(service?, endpoint?, minutes) → list[ErrorEvent]
  count()                      – total events stored
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.ingestion.models import ErrorEvent

logger = logging.getLogger(__name__)


class EventStore:
    """
    Thread-safe SQLite-backed event store.

    A single `events` table holds all ingested error events.
    Indexes on (service, timestamp) and (service, endpoint, timestamp)
    keep range queries fast.
    """

    def __init__(self, db_path: str = "monitoring.db") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    # ── Internal setup ────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id          TEXT PRIMARY KEY,
                    service     TEXT NOT NULL,
                    endpoint    TEXT,
                    message     TEXT NOT NULL,
                    timestamp   TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_svc_ts ON events(service, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_svc_ep_ts ON events(service, endpoint, timestamp)"
            )
            conn.commit()
        logger.info("EventStore initialised (db=%s)", self._db_path)

    # ── Public API ────────────────────────────────────────────────────────────

    def add(self, event: ErrorEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO events
                    (id, service, endpoint, message, timestamp, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.service,
                    event.endpoint,
                    event.message,
                    event.timestamp.isoformat(),
                    event.received_at.isoformat(),
                ),
            )
            conn.commit()

    def get_events(
        self,
        *,
        service: Optional[str] = None,
        endpoint: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[ErrorEvent]:
        """
        Return events matching the given filters.
        All filters are optional and composable.
        """
        clauses: list[str] = []
        params: list = []

        if service is not None:
            clauses.append("service = ?")
            params.append(service)
        if endpoint is not None:
            clauses.append("endpoint = ?")
            params.append(endpoint)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM events {where} ORDER BY timestamp ASC"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [self._row_to_event(r) for r in rows]

    def get_recent_events(
        self,
        *,
        service: Optional[str] = None,
        endpoint: Optional[str] = None,
        minutes: int = 5,
    ) -> list[ErrorEvent]:
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return self.get_events(service=service, endpoint=endpoint, since=since)

    def get_distinct_services(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT service FROM events").fetchall()
        return [r["service"] for r in rows]

    def get_distinct_endpoints(self, service: str) -> list[Optional[str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT endpoint FROM events WHERE service = ?", (service,)
            ).fetchall()
        return [r["endpoint"] for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> ErrorEvent:
        return ErrorEvent(
            id=row["id"],
            service=row["service"],
            endpoint=row["endpoint"],
            message=row["message"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            received_at=datetime.fromisoformat(row["received_at"]),
        )
