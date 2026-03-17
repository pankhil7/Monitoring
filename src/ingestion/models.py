"""
Pydantic models for the ingestion layer.

Severity is NOT accepted from clients. It is derived internally by the
aggregation layer based on error count, pattern type, and configured thresholds.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ErrorEventRequest(BaseModel):
    """Payload accepted by POST /errors."""

    service: str = Field(..., min_length=1, description="Name of the reporting service")
    endpoint: Optional[str] = Field(
        default=None,
        description="Specific API route or job name (e.g. 'POST /reports/generate')",
    )
    message: str = Field(..., min_length=1, description="Error description")
    timestamp: datetime = Field(..., description="When the error occurred (ISO 8601)")

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v):
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v


class ErrorEvent(BaseModel):
    """Full error event as stored in the event store (includes generated id)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    service: str
    endpoint: Optional[str]
    message: str
    timestamp: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_request(cls, req: ErrorEventRequest) -> "ErrorEvent":
        return cls(
            service=req.service,
            endpoint=req.endpoint,
            message=req.message,
            timestamp=req.timestamp,
        )


class ErrorEventResponse(BaseModel):
    """Response payload returned by POST /errors."""

    id: str
    status: str = "accepted"
    message: str = "Error event ingested successfully"
