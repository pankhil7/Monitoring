"""
Additional validation helpers beyond Pydantic field validators.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.ingestion.models import ErrorEventRequest


MAX_TIMESTAMP_DRIFT_MINUTES = 60


def validate_timestamp_freshness(req: ErrorEventRequest) -> list[str]:
    """
    Warn (not reject) if the event timestamp is far in the past or future.
    Returns a list of warning strings; empty list means no issues.
    """
    now = datetime.now(timezone.utc)
    drift = abs((now - req.timestamp).total_seconds()) / 60
    if drift > MAX_TIMESTAMP_DRIFT_MINUTES:
        return [
            f"Timestamp drift of {drift:.1f} min detected "
            f"(threshold: {MAX_TIMESTAMP_DRIFT_MINUTES} min)"
        ]
    return []


def validate_service_name(service: str) -> list[str]:
    """Basic sanity check on service name."""
    if len(service) > 128:
        return ["service name too long (max 128 chars)"]
    if not service.replace("-", "").replace("_", "").isalnum():
        return ["service name should contain only alphanumeric chars, hyphens, or underscores"]
    return []
