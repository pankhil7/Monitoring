"""
Alerting pipeline runner.

Called by the aggregation scheduler after AggregationRunner.run().
Consumes AlertEvents and:
  1. Applies noise reduction (dedupe, cooldown, re-alert on change/worsen, resolved).
  2. Builds engineering + business messages.
  3. Sends notifications via Notifier.
  4. Records notification history.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.aggregation.models import AlertEvent, PatternType
from src.alerting.alerting_state import AlertingState
from src.alerting.message_builder import build_messages
from src.alerting.models import IncidentStatus, NotificationRecord
from src.alerting.notifier import Notifier

logger = logging.getLogger(__name__)


class AlertingRunner:

    def __init__(self, alerting_state: AlertingState) -> None:
        self._state = alerting_state
        self._notifier = Notifier()

    def run(self, alert_events: list[AlertEvent]) -> None:
        """
        Process a list of AlertEvents produced by the aggregation runner.
        """
        if not alert_events:
            return

        # ── 1. Noise reduction ────────────────────────────────────────────────
        approved = self._state.noise_reducer.process(alert_events)

        if not approved:
            logger.debug("All %d alert event(s) suppressed by noise reduction", len(alert_events))
            return

        logger.info(
            "Alerting: %d event(s) approved out of %d",
            len(approved),
            len(alert_events),
        )

        # ── 2. Build messages and notify ──────────────────────────────────────
        for event in approved:
            inc = self._state.noise_reducer._incidents.get(event.alert_key)
            status = inc.status if inc else IncidentStatus.ACTIVE

            engineering_msg, business_msg = build_messages(event, status)

            # ── 3. Send ───────────────────────────────────────────────────────
            channels = self._notifier.send(
                engineering_message=engineering_msg,
                business_message=business_msg,
                severity=event.severity,
                incident_status=status,
                service=event.service,
                endpoint=event.endpoint,
                pattern_type=event.pattern_type.value,
            )

            # ── 4. Record ─────────────────────────────────────────────────────
            record = NotificationRecord(
                alert_key=event.alert_key,
                service=event.service,
                endpoint=event.endpoint,
                pattern_type=event.pattern_type.value,
                severity=event.severity,
                status=status,
                engineering_message=engineering_msg,
                business_message=business_msg,
                sent_at=datetime.now(timezone.utc),
                channels=channels,
            )
            self._state.noise_reducer.record_notification(record)
