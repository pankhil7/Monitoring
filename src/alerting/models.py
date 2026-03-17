"""
Models for the alerting layer: incident state and notification records.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class IncidentStatus(str, Enum):
    ACTIVE = "active"
    WORSENED = "worsened"
    RESOLVED = "resolved"


class Incident(BaseModel):
    """Tracks the state of one ongoing alert key."""

    alert_key: str
    service: str
    endpoint: Optional[str] = None
    pattern_type: str
    severity: str
    first_seen: datetime
    last_seen: datetime
    last_count: int
    last_rate: float
    status: IncidentStatus = IncidentStatus.ACTIVE
    last_notified_at: Optional[datetime] = None
    clean_window_count: int = 0   # consecutive windows with no alert for this key

    def model_dump(self) -> dict:
        d = super().model_dump()
        # Convert datetimes to isoformat for JSON-serialisable output
        for k in ("first_seen", "last_seen", "last_notified_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


class NotificationRecord(BaseModel):
    """Record of a notification that was sent."""

    alert_key: str
    service: str
    endpoint: Optional[str]
    pattern_type: str
    severity: str
    status: IncidentStatus
    engineering_message: str
    business_message: str
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    channels: list[str] = Field(default_factory=list)

    def model_dump(self) -> dict:
        d = super().model_dump()
        d["sent_at"] = d["sent_at"].isoformat()
        return d
