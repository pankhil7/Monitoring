"""
Data models shared across the aggregation & processing layer.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PatternType(str, Enum):
    HIGH_FREQUENCY = "high_frequency"
    REPEATED_IDENTICAL = "repeated_identical"
    SPIKE = "spike"
    POTENTIAL_OUTAGE = "potential_outage"
    FAILURE_RATIO = "failure_ratio"
    SERVICE_UNREACHABLE = "service_unreachable"  # heartbeat went silent


class AlertEventType(str, Enum):
    REACTIVE = "reactive"      # threshold/pattern already breached
    PREDICTIVE = "predictive"  # trend-based warning (before breach)


class AggregateRecord(BaseModel):
    """Time-window aggregate for one (service, endpoint?) pair."""

    service: str
    endpoint: Optional[str] = None
    window_start: datetime
    window_end: datetime
    count: int = 0
    rate_per_minute: float = 0.0
    # message → count mapping for "repeated identical" detection
    message_counts: dict[str, int] = Field(default_factory=dict)

    @property
    def dimension_key(self) -> str:
        return f"{self.service}::{self.endpoint or '*'}"


class AlertEvent(BaseModel):
    """
    A structured signal produced by pattern detection or the predictive module.
    This is NOT a notification yet — it feeds into the Alerting Layer.
    """

    pattern_type: PatternType
    event_type: AlertEventType
    service: str
    endpoint: Optional[str] = None
    severity: str          # P0 / P1 / P2 — derived by aggregation layer from count + pattern type
    count: int
    rate_per_minute: float
    window_start: datetime
    window_end: datetime
    # Pattern-specific extras
    message: Optional[str] = None          # for repeated_identical
    previous_count: Optional[int] = None   # for spike
    ratio: Optional[float] = None          # for spike
    rate_increase_pct: Optional[float] = None  # for predictive
    trend_counts: Optional[list[int]] = None   # for upward trend
    failure_ratio: Optional[float] = None      # for ratio-based rule
    created_at: datetime = Field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc))

    @property
    def alert_key(self) -> str:
        """Unique key identifying the type of ongoing issue."""
        parts = [self.service, self.endpoint or "*", self.pattern_type.value]
        if self.pattern_type == PatternType.REPEATED_IDENTICAL and self.message:
            # Include message fingerprint so different messages get different keys
            parts.append(self.message[:64])
        return "::".join(parts)
