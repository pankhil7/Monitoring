"""
Noise reduction: the gate between raw AlertEvents and actual notifications.

Rules (moderately aggressive):
  1. Group by alert_key (service + endpoint? + pattern_type + message?).
  2. Apply cooldown: same key → suppress unless issue worsened or changed.
  3. Re-alert when issue worsens (count/rate crosses higher band or severity increases).
  4. Track "resolved": N consecutive clean windows → mark resolved and notify once.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import settings
from config.service_registry import get_rules
from src.aggregation.models import AlertEvent, PatternType
from src.alerting.models import Incident, IncidentStatus, NotificationRecord

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2}


class NoiseReducer:
    """
    Maintains per-alert-key incident state.
    Call .process(alert_events, active_keys_this_tick) every tick.

    active_keys_this_tick: set of alert_key strings that fired this tick
    (used to track clean windows and eventually resolve incidents).
    """

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._recent_notifications: deque[NotificationRecord] = deque(maxlen=200)
        self._lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    def process(
        self,
        alert_events: list[AlertEvent],
    ) -> list[AlertEvent]:
        """
        Returns the subset of alert_events that should trigger a notification.
        Also updates incident state and may append "resolved" synthetic events.
        """
        now = datetime.now(timezone.utc)
        active_keys: set[str] = {e.alert_key for e in alert_events}

        approved: list[AlertEvent] = []

        with self._lock:
            # ── Update existing incidents for events that fired ───────────────
            for event in alert_events:
                key = event.alert_key
                inc = self._incidents.get(key)

                if inc is None:
                    # New incident
                    inc = Incident(
                        alert_key=key,
                        service=event.service,
                        endpoint=event.endpoint,
                        pattern_type=event.pattern_type.value,
                        severity=event.severity,
                        first_seen=event.window_end,
                        last_seen=event.window_end,
                        last_count=event.count,
                        last_rate=event.rate_per_minute,
                        status=IncidentStatus.ACTIVE,
                        clean_window_count=0,
                    )
                    self._incidents[key] = inc
                    approved.append(event)
                    logger.info("NEW incident: %s", key)
                else:
                    inc.last_seen = event.window_end
                    inc.clean_window_count = 0
                    inc.status = IncidentStatus.ACTIVE

                    if self._should_re_alert(event, inc, now):
                        inc.severity = event.severity
                        inc.last_count = event.count
                        inc.last_rate = event.rate_per_minute
                        inc.status = IncidentStatus.WORSENED
                        approved.append(event)
                        logger.info("WORSENED incident: %s count=%d", key, event.count)
                    else:
                        logger.debug("SUPPRESSED (cooldown): %s", key)

            # ── Advance clean-window counter for silent incidents ─────────────
            resolved_events: list[AlertEvent] = []
            for key, inc in list(self._incidents.items()):
                if key not in active_keys and inc.status != IncidentStatus.RESOLVED:
                    inc.clean_window_count += 1
                    needed = settings.DEFAULT_RESOLVED_CLEAN_WINDOWS
                    if inc.clean_window_count >= needed:
                        inc.status = IncidentStatus.RESOLVED
                        # Emit a synthetic "resolved" version of the last-known event
                        resolved_events.append(self._make_resolved_event(inc))
                        logger.info("RESOLVED incident: %s", key)

            approved += resolved_events

        return approved

    def record_notification(self, record: NotificationRecord) -> None:
        with self._lock:
            self._recent_notifications.append(record)
            key = record.alert_key
            if key in self._incidents:
                self._incidents[key].last_notified_at = record.sent_at

    def get_active_incidents(self) -> list[Incident]:
        with self._lock:
            return [
                inc for inc in self._incidents.values()
                if inc.status != IncidentStatus.RESOLVED
            ]

    def get_recent_alerts(self, limit: int = 20) -> list[NotificationRecord]:
        with self._lock:
            return list(self._recent_notifications)[-limit:]

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _should_re_alert(
        self, event: AlertEvent, inc: Incident, now: datetime
    ) -> bool:
        """Allow re-alert if not in cooldown AND (worsened OR severity escalated)."""
        rules = get_rules(event.service, event.endpoint)
        cooldown_min = rules.get(
            "alert_cooldown_minutes", settings.DEFAULT_ALERT_COOLDOWN_MINUTES
        )
        if inc.last_notified_at is None:
            return True

        since_last = (now - inc.last_notified_at).total_seconds() / 60
        if since_last < cooldown_min:
            # Within cooldown: only allow if worsened
            worsen_ratio = settings.DEFAULT_WORSEN_RATIO
            count_worsened = (
                inc.last_count > 0 and event.count >= inc.last_count * worsen_ratio
            )
            severity_escalated = (
                SEVERITY_RANK.get(event.severity, 99)
                < SEVERITY_RANK.get(inc.severity, 99)
            )
            return count_worsened or severity_escalated

        return True  # Outside cooldown → always allow

    def _make_resolved_event(self, inc: Incident) -> AlertEvent:
        """Create a synthetic AlertEvent to signal incident resolution."""
        from src.aggregation.models import AlertEventType

        now = datetime.now(timezone.utc)
        return AlertEvent(
            pattern_type=PatternType(inc.pattern_type),
            event_type=AlertEventType.REACTIVE,
            service=inc.service,
            endpoint=inc.endpoint,
            severity=inc.severity,
            count=0,
            rate_per_minute=0.0,
            window_start=now,
            window_end=now,
            message="__resolved__",
        )
