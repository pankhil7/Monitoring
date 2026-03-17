"""
Time-window aggregation at service level and endpoint level.

Produces AggregateRecord objects consumed by the pattern detector and
predictive module.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import settings
from src.aggregation.models import AggregateRecord
from src.ingestion.models import ErrorEvent
from src.store.event_store import EventStore

logger = logging.getLogger(__name__)


class Aggregator:
    """
    Aggregates raw ErrorEvents over a trailing time window.

    For each aggregation run the caller provides a window_end (usually
    "now") and window_minutes.  The aggregator produces:
      - one AggregateRecord per (service) – service-level
      - one AggregateRecord per (service, endpoint) – endpoint-level
    """

    def __init__(self, event_store: EventStore) -> None:
        self._store = event_store

    def run(
        self,
        window_end: Optional[datetime] = None,
        window_minutes: int = settings.AGGREGATION_WINDOW_MINUTES,
    ) -> list[AggregateRecord]:
        """
        Return all aggregate records for the given trailing window.
        """
        if window_end is None:
            window_end = datetime.now(timezone.utc)
        window_start = window_end - timedelta(minutes=window_minutes)

        events = self._store.get_events(since=window_start, until=window_end)

        if not events:
            return []

        records = self._aggregate(events, window_start, window_end)
        logger.debug(
            "Aggregation window=%s–%s: %d events → %d records",
            window_start.isoformat(),
            window_end.isoformat(),
            len(events),
            len(records),
        )
        return records

    # ── Internal ──────────────────────────────────────────────────────────────

    def _aggregate(
        self,
        events: list[ErrorEvent],
        window_start: datetime,
        window_end: datetime,
    ) -> list[AggregateRecord]:
        window_minutes = (window_end - window_start).total_seconds() / 60

        # Accumulate per service and per (service, endpoint)
        svc_counts: dict[str, dict] = defaultdict(
            lambda: {"count": 0, "message_counts": defaultdict(int)}
        )
        ep_counts: dict[tuple, dict] = defaultdict(
            lambda: {"count": 0, "message_counts": defaultdict(int)}
        )

        for ev in events:
            svc = ev.service
            ep = ev.endpoint

            svc_counts[svc]["count"] += 1
            svc_counts[svc]["message_counts"][ev.message] += 1

            if ep:
                key = (svc, ep)
                ep_counts[key]["count"] += 1
                ep_counts[key]["message_counts"][ev.message] += 1

        records: list[AggregateRecord] = []

        for svc, data in svc_counts.items():
            count = data["count"]
            records.append(
                AggregateRecord(
                    service=svc,
                    endpoint=None,
                    window_start=window_start,
                    window_end=window_end,
                    count=count,
                    rate_per_minute=count / window_minutes if window_minutes > 0 else 0,
                    message_counts=dict(data["message_counts"]),
                )
            )

        for (svc, ep), data in ep_counts.items():
            count = data["count"]
            records.append(
                AggregateRecord(
                    service=svc,
                    endpoint=ep,
                    window_start=window_start,
                    window_end=window_end,
                    count=count,
                    rate_per_minute=count / window_minutes if window_minutes > 0 else 0,
                    message_counts=dict(data["message_counts"]),
                )
            )

        return records
