"""
Predictive module: detects potential outages BEFORE thresholds are breached.

Uses trend-based signals from multiple successive aggregate windows:
  1. Rapid rate increase  – current rate ≥ X% above previous window
  2. Sustained upward trend – last N windows monotonically increasing
  3. Repeated failures in short intervals – same dimension failing every window
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from config import settings
from config.service_registry import get_rules
from src.aggregation.models import (
    AggregateRecord,
    AlertEvent,
    AlertEventType,
    PatternType,
)

logger = logging.getLogger(__name__)


class PredictiveModule:

    def run(self, history: list[list[AggregateRecord]]) -> list[AlertEvent]:
        """
        Evaluate predictive rules.

        Args:
            history: Ordered list of AggregateRecord lists (oldest → newest).
                     Must contain at least 2 windows; 3 recommended.
        Returns:
            List of potential_outage AlertEvents.
        """
        if len(history) < 2:
            return []

        # Group history by dimension key (service::endpoint?)
        # Each entry: list of (count, record) per window in order
        by_dim: dict[str, list[AggregateRecord]] = defaultdict(list)
        for window_records in history:
            seen_dims: set[str] = set()
            dim_map = {r.dimension_key: r for r in window_records}
            for dim_key, rec in dim_map.items():
                by_dim[dim_key].append(rec)
                seen_dims.add(dim_key)
            # Fill missing windows with zero-count placeholder using last seen record shape
            for dim_key, records in by_dim.items():
                if dim_key not in seen_dims and records:
                    last = records[-1]
                    by_dim[dim_key].append(
                        AggregateRecord(
                            service=last.service,
                            endpoint=last.endpoint,
                            window_start=last.window_end,
                            window_end=last.window_end,
                            count=0,
                            rate_per_minute=0,
                        )
                    )

        alerts: list[AlertEvent] = []
        for dim_key, window_list in by_dim.items():
            if len(window_list) < 2:
                continue
            latest = window_list[-1]
            rules = get_rules(latest.service, latest.endpoint)
            alerts += self._check_rate_increase(window_list, rules)
            alerts += self._check_upward_trend(window_list, rules)
            alerts += self._check_repeated_failures(window_list, rules)

        return alerts

    # ── 1. Rapid rate increase ─────────────────────────────────────────────────

    def _check_rate_increase(
        self,
        windows: list[AggregateRecord],
        rules: dict,
    ) -> list[AlertEvent]:
        current = windows[-1]
        previous = windows[-2]

        if previous.count == 0:
            return []

        min_count = rules.get(
            "predictive_min_count", settings.DEFAULT_PREDICTIVE_MIN_COUNT
        )
        if current.count < min_count:
            return []

        increase_pct_threshold = rules.get(
            "predictive_rate_increase_pct", settings.DEFAULT_PREDICTIVE_RATE_INCREASE_PCT
        )
        actual_increase_pct = ((current.count - previous.count) / previous.count) * 100

        if actual_increase_pct >= increase_pct_threshold:
            logger.debug(
                "predictive/rate_increase: %s current=%d prev=%d increase=%.0f%%",
                current.dimension_key, current.count, previous.count, actual_increase_pct,
            )
            return [
                AlertEvent(
                    pattern_type=PatternType.POTENTIAL_OUTAGE,
                    event_type=AlertEventType.PREDICTIVE,
                    service=current.service,
                    endpoint=current.endpoint,
                    severity=settings.PREDICTIVE_SEVERITY,
                    count=current.count,
                    rate_per_minute=current.rate_per_minute,
                    window_start=current.window_start,
                    window_end=current.window_end,
                    previous_count=previous.count,
                    rate_increase_pct=actual_increase_pct,
                    message=f"rate_increase:{actual_increase_pct:.0f}%",
                )
            ]
        return []

    # ── 2. Sustained upward trend ──────────────────────────────────────────────

    def _check_upward_trend(
        self,
        windows: list[AggregateRecord],
        rules: dict,
    ) -> list[AlertEvent]:
        needed = rules.get(
            "predictive_trend_windows", settings.DEFAULT_PREDICTIVE_TREND_WINDOWS
        )
        if len(windows) < needed:
            return []

        tail = windows[-needed:]
        counts = [r.count for r in tail]
        if all(counts[i] < counts[i + 1] for i in range(len(counts) - 1)) and counts[-1] >= settings.DEFAULT_PREDICTIVE_MIN_COUNT:
            current = tail[-1]
            logger.debug(
                "predictive/upward_trend: %s counts=%s",
                current.dimension_key, counts,
            )
            return [
                AlertEvent(
                    pattern_type=PatternType.POTENTIAL_OUTAGE,
                    event_type=AlertEventType.PREDICTIVE,
                    service=current.service,
                    endpoint=current.endpoint,
                    severity=settings.PREDICTIVE_SEVERITY,
                    count=current.count,
                    rate_per_minute=current.rate_per_minute,
                    window_start=tail[0].window_start,
                    window_end=current.window_end,
                    trend_counts=counts,
                    message=f"upward_trend:{counts}",
                )
            ]
        return []

    # ── 3. Repeated failures in short intervals ────────────────────────────────

    def _check_repeated_failures(
        self,
        windows: list[AggregateRecord],
        rules: dict,
    ) -> list[AlertEvent]:
        """
        Fire if ALL of the last N windows have at least min_count errors.
        Indicates that failures are consistent, not a one-off spike.
        """
        needed = rules.get(
            "predictive_trend_windows", settings.DEFAULT_PREDICTIVE_TREND_WINDOWS
        )
        if len(windows) < needed:
            return []

        tail = windows[-needed:]
        min_count = rules.get(
            "predictive_min_count", settings.DEFAULT_PREDICTIVE_MIN_COUNT
        )
        counts = [r.count for r in tail]

        if all(c >= min_count for c in counts):
            current = tail[-1]
            logger.debug(
                "predictive/repeated_failures: %s counts=%s",
                current.dimension_key, counts,
            )
            return [
                AlertEvent(
                    pattern_type=PatternType.POTENTIAL_OUTAGE,
                    event_type=AlertEventType.PREDICTIVE,
                    service=current.service,
                    endpoint=current.endpoint,
                    severity=settings.PREDICTIVE_SEVERITY,
                    count=current.count,
                    rate_per_minute=current.rate_per_minute,
                    window_start=tail[0].window_start,
                    window_end=current.window_end,
                    trend_counts=counts,
                    message=f"repeated_failures:{counts}",
                )
            ]
        return []
