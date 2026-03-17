"""
Reactive pattern detection.

Takes a list of AggregateRecord objects (current window) plus the previous
window's records (for spike detection) and emits AlertEvent objects for:

  1. high_frequency   – count/rate in window ≥ threshold
  2. repeated_identical – same message ≥ N times in window
  3. spike            – current count ≥ ratio × previous count
  4. failure_ratio    – failures / total ≥ configured ratio (per-API rule)

Severity is derived entirely from error count and pattern type using
configured thresholds — clients never send severity.
"""
from __future__ import annotations

import logging
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


class PatternDetector:

    def run(
        self,
        current: list[AggregateRecord],
        previous: Optional[list[AggregateRecord]] = None,
    ) -> list[AlertEvent]:
        """
        Evaluate all reactive patterns and return alert events.
        Severity is derived internally from count/pattern — not from client input.
        """
        prev_map = _build_map(previous or [])
        alerts: list[AlertEvent] = []

        for rec in current:
            rules = get_rules(rec.service, rec.endpoint)
            alerts += self._check_high_frequency(rec, rules)
            alerts += self._check_repeated_identical(rec, rules)
            alerts += self._check_spike(rec, prev_map.get(rec.dimension_key), rules)
            alerts += self._check_failure_ratio(rec, rules)

        return alerts

    # ── High frequency ────────────────────────────────────────────────────────

    def _check_high_frequency(self, rec: AggregateRecord, rules: dict) -> list[AlertEvent]:
        threshold = rules.get(
            "high_frequency_threshold", settings.DEFAULT_HIGH_FREQUENCY_THRESHOLD
        )
        if rec.count >= threshold:
            logger.info("Pattern high_frequency: %s count=%d threshold=%d", rec.dimension_key, rec.count, threshold)
            return [
                AlertEvent(
                    pattern_type=PatternType.HIGH_FREQUENCY,
                    event_type=AlertEventType.REACTIVE,
                    service=rec.service,
                    endpoint=rec.endpoint,
                    severity=_derive_severity(rec.count, PatternType.HIGH_FREQUENCY, rules),
                    count=rec.count,
                    rate_per_minute=rec.rate_per_minute,
                    window_start=rec.window_start,
                    window_end=rec.window_end,
                )
            ]
        return []

    # ── Repeated identical ────────────────────────────────────────────────────

    def _check_repeated_identical(self, rec: AggregateRecord, rules: dict) -> list[AlertEvent]:
        threshold = rules.get(
            "repeated_identical_min_count", settings.DEFAULT_REPEATED_IDENTICAL_MIN_COUNT
        )
        alerts = []
        for msg, count in rec.message_counts.items():
            if count >= threshold:
                logger.info(
                    "Pattern repeated_identical: %s msg=%r count=%d threshold=%d",
                    rec.dimension_key, msg, count, threshold,
                )
                alerts.append(
                    AlertEvent(
                        pattern_type=PatternType.REPEATED_IDENTICAL,
                        event_type=AlertEventType.REACTIVE,
                        service=rec.service,
                        endpoint=rec.endpoint,
                        severity=_derive_severity(count, PatternType.REPEATED_IDENTICAL, rules),
                        count=count,
                        rate_per_minute=rec.rate_per_minute,
                        window_start=rec.window_start,
                        window_end=rec.window_end,
                        message=msg,
                    )
                )
        return alerts

    # ── Spike ─────────────────────────────────────────────────────────────────

    def _check_spike(
        self,
        rec: AggregateRecord,
        prev: Optional[AggregateRecord],
        rules: dict,
    ) -> list[AlertEvent]:
        if prev is None or prev.count == 0:
            return []

        ratio_threshold = rules.get(
            "spike_ratio_threshold", settings.DEFAULT_SPIKE_RATIO_THRESHOLD
        )
        min_current = rules.get(
            "spike_min_current_count", settings.DEFAULT_SPIKE_MIN_CURRENT_COUNT
        )

        ratio = rec.count / prev.count
        if ratio >= ratio_threshold and rec.count >= min_current:
            logger.info(
                "Pattern spike: %s count=%d prev=%d ratio=%.1f threshold=%.1f",
                rec.dimension_key, rec.count, prev.count, ratio, ratio_threshold,
            )
            return [
                AlertEvent(
                    pattern_type=PatternType.SPIKE,
                    event_type=AlertEventType.REACTIVE,
                    service=rec.service,
                    endpoint=rec.endpoint,
                    severity=_derive_severity(rec.count, PatternType.SPIKE, rules),
                    count=rec.count,
                    rate_per_minute=rec.rate_per_minute,
                    window_start=rec.window_start,
                    window_end=rec.window_end,
                    previous_count=prev.count,
                    ratio=ratio,
                )
            ]
        return []

    # ── Failure ratio ─────────────────────────────────────────────────────────

    def _check_failure_ratio(self, rec: AggregateRecord, rules: dict) -> list[AlertEvent]:
        """
        Ratio-based rule: alert when failures / total_requests ≥ threshold.
        Uses failure_ratio_window as a rolling count (we only have error events,
        so we approximate total from recent event counts if no success data).
        NOTE: In a real system you'd also ingest success events; for now this
        fires when error count ≥ ratio_threshold × ratio_window (meaning: at
        least ratio_threshold fraction of the last N "known events" are errors).
        """
        ratio_threshold: Optional[float] = rules.get(
            "failure_ratio_threshold",
            settings.DEFAULT_FAILURE_RATIO_THRESHOLD,
        )
        if ratio_threshold is None:
            return []

        ratio_window: int = rules.get(
            "failure_ratio_window", settings.DEFAULT_FAILURE_RATIO_WINDOW
        )

        min_errors_to_trigger = ratio_threshold * ratio_window
        if rec.count >= min_errors_to_trigger:
            approx_ratio = min(1.0, rec.count / ratio_window)
            logger.info(
                "Pattern failure_ratio: %s count=%d window=%d threshold=%.2f approx_ratio=%.2f",
                rec.dimension_key, rec.count, ratio_window, ratio_threshold, approx_ratio,
            )
            return [
                AlertEvent(
                    pattern_type=PatternType.FAILURE_RATIO,
                    event_type=AlertEventType.REACTIVE,
                    service=rec.service,
                    endpoint=rec.endpoint,
                    severity=_derive_severity(rec.count, PatternType.FAILURE_RATIO, rules),
                    count=rec.count,
                    rate_per_minute=rec.rate_per_minute,
                    window_start=rec.window_start,
                    window_end=rec.window_end,
                    failure_ratio=approx_ratio,
                )
            ]
        return []


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_map(records: list[AggregateRecord]) -> dict[str, AggregateRecord]:
    return {r.dimension_key: r for r in records}


_SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2}
_RANK_SEVERITY = {0: "P0", 1: "P1", 2: "P2"}


def _derive_severity(count: int, pattern_type: PatternType, rules: dict) -> str:
    """
    Derive alert severity from error count and pattern type only.

    Logic:
      1. Map count to base severity using threshold bands (P0/P1/P2).
      2. Escalate by one level for spike patterns (sudden jump indicates urgency).
      3. Enforce minimum P1 for failure_ratio (partial failure still significant).
      4. Predictive patterns are always P1 (warning, not yet critical).
    """
    p0_threshold = rules.get("severity_p0_count_threshold", settings.SEVERITY_P0_COUNT_THRESHOLD)
    p1_threshold = rules.get("severity_p1_count_threshold", settings.SEVERITY_P1_COUNT_THRESHOLD)

    # Base severity from count bands
    if count >= p0_threshold:
        base = "P0"
    elif count >= p1_threshold:
        base = "P1"
    else:
        base = "P2"

    # Spike: escalate base by one level (P2→P1, P1→P0, P0 stays P0)
    if pattern_type == PatternType.SPIKE and settings.SPIKE_SEVERITY_ESCALATION:
        rank = _SEVERITY_RANK[base]
        base = _RANK_SEVERITY[max(0, rank - 1)]

    # Failure ratio: never below P1
    if pattern_type == PatternType.FAILURE_RATIO:
        if _SEVERITY_RANK[base] > _SEVERITY_RANK[settings.FAILURE_RATIO_MIN_SEVERITY]:
            base = settings.FAILURE_RATIO_MIN_SEVERITY

    return base
