"""
Health checker: actively polls each registered service's health endpoint.

Each service in SERVICE_REGISTRY can declare a `health_check_url`.
This module is invoked by a dedicated APScheduler job (separate from the
aggregation pipeline) and produces AlertEvents that flow through the normal
Alerting Layer (noise reduction → message builder → notifier).

Consecutive-failure state is persisted in the existing `monitoring.db`
(table: `health_check_state`) so it survives server restarts.

Behaviour:
  - GET {health_check_url} with a short timeout (HEALTH_CHECK_TIMEOUT_SECONDS)
  - 2xx response  → reset consecutive_failures = 0 in DB
  - non-2xx / connection error / timeout
                  → increment consecutive_failures in DB
                  → AlertEvent emitted only when counter reaches
                    HEALTH_CHECK_CONSECUTIVE_FAILURES (default 3)

Severity mapping:
  P0 service → P0 alert
  P1 service → P1 alert
  P2 service → P2 alert

DB table: health_check_state
  service               TEXT PRIMARY KEY
  consecutive_failures  INTEGER  (0 = healthy)
  last_checked_at       TEXT     ISO-8601 UTC
  last_error            TEXT     last failure message, NULL when healthy
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import settings
from config.service_registry import get_all_health_check_targets
from src.aggregation.models import AlertEvent, AlertEventType, PatternType

logger = logging.getLogger(__name__)

_CRITICALITY_TO_SEVERITY = {"P0": "P0", "P1": "P1", "P2": "P2"}


class HealthChecker:
    """
    Polls registered service health endpoints and produces AlertEvents.

    Consecutive-failure counts are stored in `health_check_state` inside the
    shared monitoring.db so the window survives process restarts.
    """

    def __init__(self, db_path: str = "monitoring.db") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    # ── DB setup ──────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS health_check_state (
                    service               TEXT PRIMARY KEY,
                    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
                    last_checked_at       TEXT NOT NULL,
                    last_error            TEXT
                )
            """)
            conn.commit()
        logger.info("HealthChecker initialised (db=%s)", self._db_path)

    # ── State helpers ─────────────────────────────────────────────────────────

    def _get_state(self, service: str) -> dict:
        """Return current DB row for service, or defaults if not yet seen."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM health_check_state WHERE service = ?", (service,)
            ).fetchone()
        if row is None:
            return {"consecutive_failures": 0, "last_error": None}
        return dict(row)

    def _record_failure(self, service: str, error: str, now: str) -> int:
        """Increment consecutive_failures, return new count."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO health_check_state
                    (service, consecutive_failures, last_checked_at, last_error)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(service) DO UPDATE SET
                    consecutive_failures = consecutive_failures + 1,
                    last_checked_at      = excluded.last_checked_at,
                    last_error           = excluded.last_error
                """,
                (service, now, error),
            )
            conn.commit()
            row = conn.execute(
                "SELECT consecutive_failures FROM health_check_state WHERE service = ?",
                (service,),
            ).fetchone()
        return row["consecutive_failures"]

    def _record_recovery(self, service: str, now: str) -> None:
        """Reset consecutive_failures to 0."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO health_check_state
                    (service, consecutive_failures, last_checked_at, last_error)
                VALUES (?, 0, ?, NULL)
                ON CONFLICT(service) DO UPDATE SET
                    consecutive_failures = 0,
                    last_checked_at      = excluded.last_checked_at,
                    last_error           = NULL
                """,
                (service, now),
            )
            conn.commit()

    # ── Main poll loop ────────────────────────────────────────────────────────

    def run(self) -> list[AlertEvent]:
        """
        Poll all configured health endpoints.

        Returns SERVICE_UNREACHABLE AlertEvents only for services that have been
        continuously unhealthy for >= HEALTH_CHECK_CONSECUTIVE_FAILURES ticks.
        """
        targets = get_all_health_check_targets()
        if not targets:
            logger.debug("HealthChecker: no targets configured, skipping")
            return []

        threshold = settings.HEALTH_CHECK_CONSECUTIVE_FAILURES
        alert_events: list[AlertEvent] = []
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()

        for target in targets:
            service = target["service"]
            url = target["health_check_url"]
            criticality = target.get("criticality", "P1")

            error_message = self._check(url)

            if error_message:
                consecutive = self._record_failure(service, error_message, now_str)

                logger.warning(
                    "Health check FAILED: service=%s url=%s error=%s consecutive=%d/%d",
                    service, url, error_message, consecutive, threshold,
                )

                if consecutive >= threshold:
                    severity = _CRITICALITY_TO_SEVERITY.get(criticality, "P1")
                    alert_events.append(AlertEvent(
                        pattern_type=PatternType.SERVICE_UNREACHABLE,
                        event_type=AlertEventType.REACTIVE,
                        service=service,
                        endpoint=None,
                        severity=severity,
                        count=consecutive,
                        rate_per_minute=0.0,
                        window_start=now,
                        window_end=now,
                        message=(
                            f"{error_message} "
                            f"(down for {consecutive} consecutive checks)"
                        ),
                    ))
                    logger.warning(
                        "SERVICE_UNREACHABLE alert: service=%s consecutive=%d severity=%s",
                        service, consecutive, severity,
                    )
                else:
                    logger.info(
                        "Health check failing but below threshold: "
                        "service=%s consecutive=%d/%d — suppressing alert",
                        service, consecutive, threshold,
                    )
            else:
                prev_state = self._get_state(service)
                prev_failures = prev_state["consecutive_failures"]
                self._record_recovery(service, now_str)

                if prev_failures > 0:
                    logger.info(
                        "Health check RECOVERED: service=%s "
                        "(was down for %d consecutive check(s))",
                        service, prev_failures,
                    )
                else:
                    logger.info("Health check OK: service=%s url=%s", service, url)

        logger.info(
            "HealthChecker tick: checked=%d alerting=%d",
            len(targets), len(alert_events),
        )
        return alert_events

    # ── HTTP helper ───────────────────────────────────────────────────────────

    def _check(self, url: str) -> Optional[str]:
        """GET the URL. Returns an error string on failure, None on success."""
        timeout = settings.HEALTH_CHECK_TIMEOUT_SECONDS
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            if resp.status_code < 200 or resp.status_code >= 300:
                return f"HTTP {resp.status_code}"
            return None
        except httpx.TimeoutException:
            return f"Timeout after {timeout}s"
        except httpx.ConnectError:
            return "Connection refused"
        except httpx.RequestError as exc:
            return f"Request error: {exc}"
