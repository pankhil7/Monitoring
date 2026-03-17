"""
DB-backed rules store with in-memory cache.

Rules are stored in a `service_rules` SQLite table.
Resolution order (lowest → highest priority):
  1. Global defaults  (service IS NULL, endpoint IS NULL)
  2. Service-level    (service = "atlas-api", endpoint IS NULL)
  3. Endpoint-level   (service = "atlas-api", endpoint = "POST /reports/generate")

Any service/endpoint not explicitly configured automatically inherits
global defaults (guaranteed to exist after seeding).

Cache:
  Rules are cached in memory per (service, endpoint) key.
  Cache is invalidated after RULES_CACHE_TTL_SECONDS.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# TTL for the in-memory rules cache (seconds)
RULES_CACHE_TTL_SECONDS = 60

# All valid rule keys with their Python types
RULE_SCHEMA: dict[str, type] = {
    "high_frequency_threshold": int,
    "spike_ratio_threshold": float,
    "spike_min_current_count": int,
    "repeated_identical_min_count": int,
    "failure_ratio_threshold": float,
    "failure_ratio_window": int,
    "alert_cooldown_minutes": int,
    "predictive_rate_increase_pct": float,
    "predictive_min_count": int,
    "predictive_trend_windows": int,
    "severity_p0_count_threshold": int,
    "severity_p1_count_threshold": int,
}

# Global defaults seeded into the DB on startup
SEED_DEFAULTS: dict[str, object] = {
    "high_frequency_threshold": settings.DEFAULT_HIGH_FREQUENCY_THRESHOLD,
    "spike_ratio_threshold": settings.DEFAULT_SPIKE_RATIO_THRESHOLD,
    "spike_min_current_count": settings.DEFAULT_SPIKE_MIN_CURRENT_COUNT,
    "repeated_identical_min_count": settings.DEFAULT_REPEATED_IDENTICAL_MIN_COUNT,
    "alert_cooldown_minutes": settings.DEFAULT_ALERT_COOLDOWN_MINUTES,
    "predictive_rate_increase_pct": settings.DEFAULT_PREDICTIVE_RATE_INCREASE_PCT,
    "predictive_min_count": settings.DEFAULT_PREDICTIVE_MIN_COUNT,
    "predictive_trend_windows": settings.DEFAULT_PREDICTIVE_TREND_WINDOWS,
    "severity_p0_count_threshold": settings.SEVERITY_P0_COUNT_THRESHOLD,
    "severity_p1_count_threshold": settings.SEVERITY_P1_COUNT_THRESHOLD,
}


class RulesStore:
    """
    Thread-safe SQLite-backed rules store with TTL in-memory cache.

    Usage:
        rules_store = RulesStore(db_path="monitoring.db")
        rules = rules_store.get_rules("atlas-api", "POST /reports/generate")
    """

    def __init__(self, db_path: str = "monitoring.db") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        # Cache: key → (rules_dict, expiry_timestamp)
        self._cache: dict[str, tuple[dict, float]] = {}
        self._init_db()
        self._seed_defaults()

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_rules(self, service: str, endpoint: Optional[str] = None) -> dict:
        """
        Return fully merged rules for (service, endpoint?).
        Falls back cleanly through: endpoint → service → global defaults.
        Result is cached for RULES_CACHE_TTL_SECONDS.
        """
        cache_key = f"{service}::{endpoint or '*'}"
        cached = self._cache.get(cache_key)
        if cached and time.monotonic() < cached[1]:
            return cached[0]

        # Load and merge in priority order
        global_rules = self._load_from_db(service=None, endpoint=None)
        service_rules = self._load_from_db(service=service, endpoint=None)
        endpoint_rules = self._load_from_db(service=service, endpoint=endpoint) if endpoint else {}

        merged = {**global_rules, **service_rules, **endpoint_rules}

        self._cache[cache_key] = (merged, time.monotonic() + RULES_CACHE_TTL_SECONDS)
        return merged

    def set_rule(
        self,
        rule_key: str,
        rule_value: object,
        service: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> None:
        """
        Insert or update a single rule.
        service=None → global default.
        endpoint=None → service-level rule (or global if service is also None).
        """
        if rule_key not in RULE_SCHEMA:
            raise ValueError(f"Unknown rule key '{rule_key}'. Valid keys: {list(RULE_SCHEMA)}")
        if endpoint and not service:
            raise ValueError("endpoint requires service to be specified")

        coerced = RULE_SCHEMA[rule_key](rule_value)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO service_rules (service, endpoint, rule_key, rule_value, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT (service, endpoint, rule_key)
                DO UPDATE SET rule_value = excluded.rule_value,
                              updated_at = excluded.updated_at
                """,
                (_none_to_null(service), _none_to_null(endpoint), rule_key, str(coerced)),
            )
            conn.commit()
        self._invalidate_cache(service, endpoint)
        logger.info("Rule set: service=%s endpoint=%s %s=%s", service, endpoint, rule_key, coerced)

    def delete_rule(
        self,
        rule_key: str,
        service: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> bool:
        """
        Delete a rule row. Returns True if a row was deleted.
        Deleting a service rule reverts that key to global default.
        Deleting a global default is allowed but re-seeded on next startup.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM service_rules
                WHERE (service IS ? OR (service IS NULL AND ? IS NULL))
                  AND (endpoint IS ? OR (endpoint IS NULL AND ? IS NULL))
                  AND rule_key = ?
                """,
                (service, service, endpoint, endpoint, rule_key),
            )
            conn.commit()
            deleted = cur.rowcount > 0
        if deleted:
            self._invalidate_cache(service, endpoint)
        return deleted

    def get_all_rules(self) -> list[dict]:
        """Return all rows from service_rules table (for admin UI)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT service, endpoint, rule_key, rule_value, updated_at "
                "FROM service_rules ORDER BY service NULLS FIRST, endpoint NULLS FIRST, rule_key"
            ).fetchall()
        return [
            {
                "service": row["service"],
                "endpoint": row["endpoint"],
                "rule_key": row["rule_key"],
                "rule_value": _coerce(row["rule_key"], row["rule_value"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def get_rules_for_service(self, service: str) -> dict:
        """Return merged rules for a service (global + service-level, no endpoint)."""
        return self.get_rules(service=service, endpoint=None)

    def invalidate_all_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS service_rules (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    service    TEXT,
                    endpoint   TEXT,
                    rule_key   TEXT NOT NULL,
                    rule_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (service, endpoint, rule_key)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rules_svc "
                "ON service_rules(service, endpoint)"
            )
            conn.commit()
        logger.info("RulesStore initialised (db=%s)", self._db_path)

    def _seed_defaults(self) -> None:
        """Insert global defaults if they don't already exist (INSERT OR IGNORE)."""
        with self._lock, self._connect() as conn:
            for key, value in SEED_DEFAULTS.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO service_rules
                        (service, endpoint, rule_key, rule_value, updated_at)
                    VALUES (NULL, NULL, ?, ?, datetime('now'))
                    """,
                    (key, str(value)),
                )
            conn.commit()
        logger.info("RulesStore: global defaults seeded (%d keys)", len(SEED_DEFAULTS))

    def _load_from_db(
        self,
        service: Optional[str],
        endpoint: Optional[str],
    ) -> dict:
        """Load rules for the exact (service, endpoint) combination."""
        with self._connect() as conn:
            if service is None:
                rows = conn.execute(
                    "SELECT rule_key, rule_value FROM service_rules "
                    "WHERE service IS NULL AND endpoint IS NULL"
                ).fetchall()
            elif endpoint is None:
                rows = conn.execute(
                    "SELECT rule_key, rule_value FROM service_rules "
                    "WHERE service = ? AND endpoint IS NULL",
                    (service,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT rule_key, rule_value FROM service_rules "
                    "WHERE service = ? AND endpoint = ?",
                    (service, endpoint),
                ).fetchall()
        return {row["rule_key"]: _coerce(row["rule_key"], row["rule_value"]) for row in rows}

    def _invalidate_cache(self, service: Optional[str], endpoint: Optional[str]) -> None:
        """Remove all cache entries that could be affected by a rule change."""
        with self._lock:
            keys_to_drop = []
            for cache_key in self._cache:
                svc_part = cache_key.split("::")[0]
                # Global change affects everyone; service change affects that service
                if service is None or svc_part == service:
                    keys_to_drop.append(cache_key)
            for k in keys_to_drop:
                del self._cache[k]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _none_to_null(v: Optional[str]) -> Optional[str]:
    return v  # SQLite driver handles None → NULL correctly


def _coerce(rule_key: str, raw: str) -> object:
    """Cast raw string value to the correct Python type for this rule key."""
    typ = RULE_SCHEMA.get(rule_key)
    if typ is None:
        return raw
    try:
        return typ(raw)
    except (ValueError, TypeError):
        return raw
