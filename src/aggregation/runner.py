"""
Aggregation pipeline runner.

Called by the scheduler every N seconds.  For each tick it:
  1. Runs aggregation for the current window (and past windows for history).
  2. Runs pattern detection (reactive).
  3. Runs predictive module (proactive).
  4. Returns combined list of AlertEvents for the Alerting Layer.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import settings
from src.aggregation.aggregator import Aggregator
from src.aggregation.models import AggregateRecord, AlertEvent
from src.aggregation.pattern_detector import PatternDetector
from src.aggregation.predictive import PredictiveModule
from src.store.event_store import EventStore

logger = logging.getLogger(__name__)

# Keep at most this many windows in memory for trend detection
MAX_HISTORY = settings.HISTORY_WINDOWS + 1


class AggregationRunner:
    """
    Stateful runner that maintains a rolling window history.
    Call .run() once per tick from the scheduler.
    """

    def __init__(self, event_store: EventStore) -> None:
        self._aggregator = Aggregator(event_store)
        self._detector = PatternDetector()
        self._predictive = PredictiveModule()
        # Rolling history: deque of AggregateRecord lists (oldest first)
        self._history: deque[list[AggregateRecord]] = deque(maxlen=MAX_HISTORY)

    def run(self, window_end: Optional[datetime] = None) -> list[AlertEvent]:
        if window_end is None:
            window_end = datetime.now(timezone.utc)

        window_minutes = settings.AGGREGATION_WINDOW_MINUTES

        # ── 1. Aggregate current window ───────────────────────────────────────
        current_records = self._aggregator.run(
            window_end=window_end,
            window_minutes=window_minutes,
        )
        self._history.append(current_records)

        history_list = list(self._history)
        previous_records = history_list[-2] if len(history_list) >= 2 else None

        logger.info(
            "AggregationRunner tick: window_end=%s records=%d history_depth=%d",
            window_end.isoformat(),
            len(current_records),
            len(history_list),
        )

        # ── 2. Reactive pattern detection ─────────────────────────────────────
        reactive_alerts = self._detector.run(
            current=current_records,
            previous=previous_records,
        )

        # ── 3. Predictive module ───────────────────────────────────────────────
        predictive_alerts = self._predictive.run(history=history_list)

        all_alerts = reactive_alerts + predictive_alerts

        if all_alerts:
            logger.info(
                "AggregationRunner produced %d alert event(s) (%d reactive, %d predictive)",
                len(all_alerts),
                len(reactive_alerts),
                len(predictive_alerts),
            )

        return all_alerts
