"""
Service registry: defines criticality (P0/P1/P2) and static seed rules for each
known service.

Rules resolution at runtime:
  1. Global defaults (seeded from settings.py into service_rules DB table)
  2. Service-level rules (from DB, originally seeded from SERVICE_REGISTRY)
  3. Endpoint-level rules (from DB, originally seeded from SERVICE_REGISTRY endpoints)

At startup, `seed_registry_to_db()` is called to push SERVICE_REGISTRY rules
into the DB (INSERT OR IGNORE — won't overwrite rules already changed via API).

At runtime, `get_rules()` delegates to the DB-backed RulesStore (with cache).

Rule keys:
  high_frequency_threshold       int   – count in window that triggers high-frequency alert
  spike_ratio_threshold          float – current/previous ratio that triggers spike
  spike_min_current_count        int   – minimum current count to consider as spike
  repeated_identical_min_count   int   – same (service, endpoint?, message) count threshold
  failure_ratio_threshold        float – failures/total ratio threshold (e.g. 0.67 = 2 out of 3)
  failure_ratio_window           int   – rolling window of N events for ratio check
  alert_cooldown_minutes         int   – cooldown between same-key alerts
  predictive_rate_increase_pct   float – % increase to trigger potential_outage
  severity_p0_count_threshold    int   – count that maps to P0
  severity_p1_count_threshold    int   – count that maps to P1
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Severity / criticality levels
P0 = "P0"
P1 = "P1"
P2 = "P2"

SERVICE_REGISTRY: dict[str, dict] = {
    # ── APIs (P0) ──────────────────────────────────────────────────────────────
    "atlas-api": {
        "criticality": P0,
        "description": "Core Atlas reporting and advisor API",
        "health_check_url": "http://atlas-api/health",
        "rules": {
            "high_frequency_threshold": 20,
            "spike_ratio_threshold": 2.0,
            "spike_min_current_count": 5,
            "repeated_identical_min_count": 5,
            "alert_cooldown_minutes": 10,
        },
        "endpoints": {
            "POST /reports/generate": {
                "criticality": P0,
                "rules": {
                    # Alert if 2 out of 3 recent requests fail
                    "failure_ratio_threshold": 0.67,
                    "failure_ratio_window": 3,
                    "high_frequency_threshold": 10,
                    "repeated_identical_min_count": 3,
                },
            },
            "GET /stream": {
                "criticality": P0,
                "rules": {
                    "high_frequency_threshold": 15,
                    "failure_ratio_threshold": 0.5,
                    "failure_ratio_window": 4,
                },
            },
        },
    },
    "advisor-api": {
        "criticality": P0,
        "description": "Advisor-facing REST API",
        "health_check_url": "http://advisor-api/health",
        "rules": {
            "high_frequency_threshold": 15,
            "spike_ratio_threshold": 2.5,
            "repeated_identical_min_count": 5,
            "alert_cooldown_minutes": 10,
        },
    },

    # ── AI pipelines (P0) ──────────────────────────────────────────────────────
    "report-pipeline": {
        "criticality": P0,
        "description": "LLM-powered report generation pipeline",
        "health_check_url": "http://report-pipeline/health",
        "rules": {
            "high_frequency_threshold": 10,
            "spike_ratio_threshold": 2.0,
            "repeated_identical_min_count": 3,
            "alert_cooldown_minutes": 10,
            "predictive_rate_increase_pct": 150.0,
        },
    },
    "chat-pipeline": {
        "criticality": P0,
        "description": "Real-time chat AI pipeline",
        "health_check_url": "http://chat-pipeline/health",
        "rules": {
            "high_frequency_threshold": 15,
            "repeated_identical_min_count": 5,
            "alert_cooldown_minutes": 10,
        },
    },

    # ── WebSocket / SSE (P0/P1) ───────────────────────────────────────────────
    "chat-websocket": {
        "criticality": P0,
        "description": "WebSocket service for real-time chat",
        "health_check_url": "http://chat-websocket/health",
        "rules": {
            "high_frequency_threshold": 20,
            "spike_ratio_threshold": 3.0,
            "repeated_identical_min_count": 8,
            "alert_cooldown_minutes": 15,
        },
    },
    "notification-sse": {
        "criticality": P1,
        "description": "SSE service for push notifications",
        "health_check_url": "http://notification-sse/health",
        "rules": {
            "high_frequency_threshold": 30,
            "spike_ratio_threshold": 3.0,
            "alert_cooldown_minutes": 20,
        },
    },

    # ── Background jobs (P1/P2, some overridden to P0) ────────────────────────
    "report-generation-job": {
        "criticality": P0,   # P0 override: required for report generation feature
        "description": "Background job: generates scheduled reports",
        "health_check_url": "http://report-generation-job/health",
        "rules": {
            "high_frequency_threshold": 5,
            "repeated_identical_min_count": 2,
            "failure_ratio_threshold": 0.5,
            "failure_ratio_window": 4,
            "alert_cooldown_minutes": 10,
        },
    },
    "data-sync-job": {
        "criticality": P1,
        "description": "Background job: syncs advisor data",
        "rules": {
            "high_frequency_threshold": 10,
            "repeated_identical_min_count": 5,
            "alert_cooldown_minutes": 20,
        },
    },
    "cleanup-job": {
        "criticality": P2,
        "description": "Background job: database cleanup",
        "rules": {
            "high_frequency_threshold": 20,
            "alert_cooldown_minutes": 60,
        },
    },
}

# Default for unknown services not listed above
DEFAULT_CRITICALITY = P2

# Module-level reference to the RulesStore, set by app.py at startup.
# Pattern detector and predictive module call get_rules() which delegates here.
_rules_store = None


def init_rules_store(rules_store) -> None:
    """Called once at app startup to inject the RulesStore instance."""
    global _rules_store
    _rules_store = rules_store
    logger.info("Service registry: RulesStore injected")


def seed_registry_to_db(rules_store) -> None:
    """
    Push SERVICE_REGISTRY rules into the DB using INSERT OR IGNORE.
    Runs at startup so the DB is pre-populated with sensible per-service
    rules. Rules already changed via the admin API are NOT overwritten.
    """
    count = 0
    for svc_name, svc_data in SERVICE_REGISTRY.items():
        for key, value in svc_data.get("rules", {}).items():
            try:
                rules_store.set_rule(key, value, service=svc_name, endpoint=None)
                count += 1
            except Exception as exc:
                logger.warning("Could not seed rule %s/%s: %s", svc_name, key, exc)

        for ep_name, ep_data in svc_data.get("endpoints", {}).items():
            for key, value in ep_data.get("rules", {}).items():
                try:
                    rules_store.set_rule(key, value, service=svc_name, endpoint=ep_name)
                    count += 1
                except Exception as exc:
                    logger.warning("Could not seed endpoint rule %s/%s/%s: %s", svc_name, ep_name, key, exc)

    logger.info("Service registry: seeded %d rules into DB", count)


def get_service_info(service: str) -> dict:
    """Return registry entry for service, or a minimal default dict."""
    return SERVICE_REGISTRY.get(service, {"criticality": DEFAULT_CRITICALITY, "rules": {}})


def get_criticality(service: str, endpoint: Optional[str] = None) -> str:
    """Return criticality for (service, endpoint?), falling back to service-level then default."""
    svc = SERVICE_REGISTRY.get(service, {})
    if endpoint:
        ep = svc.get("endpoints", {}).get(endpoint, {})
        if "criticality" in ep:
            return ep["criticality"]
    return svc.get("criticality", DEFAULT_CRITICALITY)


def get_health_check_url(service: str) -> Optional[str]:
    """Return the configured health-check URL for a service, or None if not set."""
    return SERVICE_REGISTRY.get(service, {}).get("health_check_url")


def get_all_health_check_targets() -> list[dict]:
    """
    Return all services that have a health_check_url configured.
    Each entry: { service, health_check_url, criticality }
    """
    return [
        {
            "service": name,
            "health_check_url": data["health_check_url"],
            "criticality": data.get("criticality", DEFAULT_CRITICALITY),
        }
        for name, data in SERVICE_REGISTRY.items()
        if data.get("health_check_url")
    ]


def get_rules(service: str, endpoint: Optional[str] = None) -> dict:
    """
    Return merged alert rules for (service, endpoint?).

    Delegates to DB-backed RulesStore when available (runtime).
    Falls back to static SERVICE_REGISTRY dict during tests or before startup.

    Merge order (lowest → highest priority):
      global defaults (DB) → service rules (DB) → endpoint rules (DB)
    """
    if _rules_store is not None:
        return _rules_store.get_rules(service=service, endpoint=endpoint)

    # Fallback: static config (used in tests / before app startup)
    svc = SERVICE_REGISTRY.get(service, {})
    service_rules: dict = svc.get("rules", {})
    endpoint_rules: dict = {}
    if endpoint:
        ep = svc.get("endpoints", {}).get(endpoint, {})
        endpoint_rules = ep.get("rules", {})
    return {**service_rules, **endpoint_rules}
