"""
Message builder: produces engineering and business-facing alert copies.

Engineering message: technical detail (service, endpoint, count, pattern).
Business message:    user-impact language (no stack traces, no internal names).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Tuple

from src.aggregation.models import AlertEvent, PatternType, AlertEventType
from src.alerting.models import IncidentStatus

logger = logging.getLogger(__name__)

# Human-readable service names (falls back to raw name)
_FRIENDLY_SERVICE_NAMES: dict[str, str] = {
    "atlas-api": "Atlas",
    "advisor-api": "the Advisor platform",
    "report-pipeline": "the report generation service",
    "chat-pipeline": "the AI chat service",
    "chat-websocket": "real-time chat",
    "notification-sse": "push notifications",
    "report-generation-job": "scheduled report generation",
    "data-sync-job": "data synchronisation",
    "cleanup-job": "background maintenance",
}

_FRIENDLY_PATTERN: dict[PatternType, str] = {
    PatternType.HIGH_FREQUENCY: "high error rate",
    PatternType.REPEATED_IDENTICAL: "repeated error",
    PatternType.SPIKE: "sudden spike in errors",
    PatternType.POTENTIAL_OUTAGE: "potential outage risk",
    PatternType.FAILURE_RATIO: "high failure rate",
}

_BUSINESS_IMPACT: dict[str, str] = {
    "atlas-api": "Some users may experience issues accessing reports or data.",
    "advisor-api": "Advisors may be unable to access the platform or see errors.",
    "report-pipeline": "Report generation may be delayed or unavailable.",
    "chat-pipeline": "AI-assisted responses may be slower or unavailable.",
    "chat-websocket": "Real-time chat may be disrupted or show connection issues.",
    "notification-sse": "Push notifications may be delayed.",
    "report-generation-job": "Scheduled reports may not be generated on time.",
    "data-sync-job": "Advisor data may not be up to date.",
    "cleanup-job": "Background maintenance tasks may be delayed.",
}

_DEFAULT_BUSINESS_IMPACT = "Some users may experience degraded service."


def build_messages(
    event: AlertEvent,
    incident_status: IncidentStatus = IncidentStatus.ACTIVE,
) -> Tuple[str, str]:
    """
    Return (engineering_message, business_message) for the given AlertEvent.
    """
    if event.message == "__resolved__":
        eng, biz = _build_resolved(event)
        logger.debug("Built RESOLVED messages: service=%s endpoint=%s", event.service, event.endpoint)
        return eng, biz

    engineering = _build_engineering(event, incident_status)
    business = _build_business(event, incident_status)
    logger.debug(
        "Built alert messages: service=%s endpoint=%s pattern=%s severity=%s status=%s",
        event.service, event.endpoint, event.pattern_type.value, event.severity, incident_status.value,
    )
    return engineering, business


# ── Engineering messages ───────────────────────────────────────────────────────

def _build_engineering(event: AlertEvent, status: IncidentStatus) -> str:
    svc = event.service
    ep = f" ({event.endpoint})" if event.endpoint else ""
    pattern = _FRIENDLY_PATTERN.get(event.pattern_type, event.pattern_type.value)
    severity = event.severity

    prefix = ""
    if status == IncidentStatus.WORSENED:
        prefix = "[WORSENED] "
    elif event.event_type == AlertEventType.PREDICTIVE:
        prefix = "[WARNING] "

    base = f"{prefix}[{severity}] {pattern.upper()} in {svc}{ep}."

    detail_parts = [f"{event.count} error(s)"]
    if event.rate_per_minute:
        detail_parts.append(f"rate {event.rate_per_minute:.1f}/min")

    window_str = _window_str(event)
    if window_str:
        detail_parts.append(f"in {window_str}")

    if event.pattern_type == PatternType.REPEATED_IDENTICAL and event.message:
        detail_parts.append(f'Error: "{event.message}"')

    if event.pattern_type == PatternType.SPIKE and event.previous_count is not None:
        detail_parts.append(
            f"Spike: {event.count} now vs {event.previous_count} previously "
            f"({event.ratio:.1f}x increase)"
        )

    if event.pattern_type == PatternType.FAILURE_RATIO and event.failure_ratio is not None:
        detail_parts.append(f"Failure ratio: {event.failure_ratio:.0%}")

    if event.pattern_type == PatternType.POTENTIAL_OUTAGE:
        if event.rate_increase_pct is not None:
            detail_parts.append(f"Rate increased {event.rate_increase_pct:.0f}% vs previous window.")
        if event.trend_counts:
            detail_parts.append(f"Trend: {event.trend_counts}")

    return f"{base} {', '.join(detail_parts)}."


# ── Business messages ──────────────────────────────────────────────────────────

def _build_business(event: AlertEvent, status: IncidentStatus) -> str:
    friendly = _FRIENDLY_SERVICE_NAMES.get(event.service, event.service)
    impact = _BUSINESS_IMPACT.get(event.service, _DEFAULT_BUSINESS_IMPACT)

    if event.event_type == AlertEventType.PREDICTIVE:
        return (
            f"We are monitoring a potential issue with {friendly}. "
            f"No impact confirmed yet, but our team is investigating. "
            f"{impact}"
        )

    if status == IncidentStatus.WORSENED:
        return (
            f"The issue with {friendly} has worsened. "
            f"{impact} "
            f"Our team is actively working to resolve this."
        )

    return (
        f"{friendly.capitalize()} is currently experiencing an issue. "
        f"{impact} "
        f"Our team is investigating and working to resolve this as quickly as possible."
    )


def _build_resolved(event: AlertEvent) -> Tuple[str, str]:
    svc = event.service
    ep = f" ({event.endpoint})" if event.endpoint else ""
    friendly = _FRIENDLY_SERVICE_NAMES.get(svc, svc)
    eng = f"[RESOLVED] {svc}{ep}: error rate has returned to normal."
    biz = f"{friendly.capitalize()} has recovered. Service is operating normally."
    return eng, biz


def _window_str(event: AlertEvent) -> str:
    try:
        diff_min = int((event.window_end - event.window_start).total_seconds() / 60)
        return f"last {diff_min} min" if diff_min > 0 else ""
    except Exception:
        return ""
