"""
Global configuration loaded from environment variables (with defaults).
All threshold and timing values can be overridden per-service/endpoint
in config/service_registry.py.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, default))


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, default))


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ── Server ────────────────────────────────────────────────────────────────────
APP_HOST = _str("APP_HOST", "0.0.0.0")
APP_PORT = _int("APP_PORT", 8000)

# ── Aggregation schedule ──────────────────────────────────────────────────────
# How often (seconds) the aggregation + processing pipeline runs
AGGREGATION_INTERVAL_SECONDS = _int("AGGREGATION_INTERVAL_SECONDS", 60)

# Width of each aggregation window (minutes). Events within this trailing
# window are counted per (service, endpoint).
AGGREGATION_WINDOW_MINUTES = _int("AGGREGATION_WINDOW_MINUTES", 5)

# How many past windows to keep for trend/spike comparison
HISTORY_WINDOWS = _int("HISTORY_WINDOWS", 3)

# ── Global pattern-detection thresholds ──────────────────────────────────────
# High-frequency: fire when error count in window ≥ this value
DEFAULT_HIGH_FREQUENCY_THRESHOLD = _int("DEFAULT_HIGH_FREQUENCY_THRESHOLD", 10)

# Spike: fire when current_count / previous_count ≥ this ratio
DEFAULT_SPIKE_RATIO_THRESHOLD = _float("DEFAULT_SPIKE_RATIO_THRESHOLD", 2.0)

# Spike: only consider spike if current count is at least this value
# (prevents "2 vs 1" false positives)
DEFAULT_SPIKE_MIN_CURRENT_COUNT = _int("DEFAULT_SPIKE_MIN_CURRENT_COUNT", 5)

# Repeated-identical: fire when same (service, endpoint?, message) appears
# this many times within one window
DEFAULT_REPEATED_IDENTICAL_MIN_COUNT = _int("DEFAULT_REPEATED_IDENTICAL_MIN_COUNT", 5)

# Ratio-based rule: raise alert when failures / total_in_window ≥ this ratio.
# Only effective when the service/endpoint also reports successes (total tracked).
# Set to None to disable ratio-based rules globally.
DEFAULT_FAILURE_RATIO_THRESHOLD: float | None = None

# How many recent events to consider for ratio-based check (rolling window)
DEFAULT_FAILURE_RATIO_WINDOW = _int("DEFAULT_FAILURE_RATIO_WINDOW", 3)

# ── Predictive thresholds ─────────────────────────────────────────────────────
# Fire "potential_outage" when rate increased by at least this % vs previous window
DEFAULT_PREDICTIVE_RATE_INCREASE_PCT = _float("DEFAULT_PREDICTIVE_RATE_INCREASE_PCT", 200.0)

# Require at least this many errors in the current window to trigger predictive
DEFAULT_PREDICTIVE_MIN_COUNT = _int("DEFAULT_PREDICTIVE_MIN_COUNT", 3)

# Number of consecutive increasing windows to declare an "upward trend"
DEFAULT_PREDICTIVE_TREND_WINDOWS = _int("DEFAULT_PREDICTIVE_TREND_WINDOWS", 3)

# ── Severity derivation (threshold-based, no client input) ───────────────────
# Severity is derived entirely from error count/rate and pattern type.
# Thresholds: count in window that maps to each severity level.
# If count >= P0 threshold → P0; elif >= P1 threshold → P1; else P2.
SEVERITY_P0_COUNT_THRESHOLD = _int("SEVERITY_P0_COUNT_THRESHOLD", 50)
SEVERITY_P1_COUNT_THRESHOLD = _int("SEVERITY_P1_COUNT_THRESHOLD", 10)

# Spike pattern: always escalates by one level (P2→P1, P1→P0, P0 stays P0)
SPIKE_SEVERITY_ESCALATION = True

# Failure ratio pattern: always at least P1
FAILURE_RATIO_MIN_SEVERITY = "P1"

# Predictive / potential_outage: always P1 (warning, not critical yet)
PREDICTIVE_SEVERITY = "P1"

# ── Health polling ────────────────────────────────────────────────────────────
# How often (seconds) the health checker polls each registered service
HEALTH_CHECK_INTERVAL_SECONDS = _int("HEALTH_CHECK_INTERVAL_SECONDS", 60)

# HTTP timeout (seconds) for each health poll request
HEALTH_CHECK_TIMEOUT_SECONDS = _float("HEALTH_CHECK_TIMEOUT_SECONDS", 5.0)

# Number of consecutive failed health checks before a SERVICE_UNREACHABLE alert
# is raised. Prevents alerts from transient single-tick network blips.
HEALTH_CHECK_CONSECUTIVE_FAILURES = _int("HEALTH_CHECK_CONSECUTIVE_FAILURES", 3)

# ── Alerting / noise-reduction ────────────────────────────────────────────────
# Minimum minutes before the same alert key can fire again
DEFAULT_ALERT_COOLDOWN_MINUTES = _int("DEFAULT_ALERT_COOLDOWN_MINUTES", 15)

# Re-alert within cooldown if count/rate grows by this factor (worsening)
DEFAULT_WORSEN_RATIO = _float("DEFAULT_WORSEN_RATIO", 2.0)

# Number of consecutive clean windows before an incident is marked resolved
DEFAULT_RESOLVED_CLEAN_WINDOWS = _int("DEFAULT_RESOLVED_CLEAN_WINDOWS", 2)

# ── Notification channels ─────────────────────────────────────────────────────
SLACK_WEBHOOK_URL = _str("SLACK_WEBHOOK_URL")
EMAIL_HOST = _str("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = _int("EMAIL_PORT", 587)
EMAIL_USER = _str("EMAIL_USER")
EMAIL_PASSWORD = _str("EMAIL_PASSWORD")
EMAIL_FROM = _str("EMAIL_FROM")
EMAIL_TO = _str("EMAIL_TO")

# ── Optional OpenAI (business messages) ──────────────────────────────────────
USE_OPENAI_FOR_BUSINESS_MESSAGES = _bool("USE_OPENAI_FOR_BUSINESS_MESSAGES", False)
OPENAI_API_KEY = _str("OPENAI_API_KEY")

# ── Storage ───────────────────────────────────────────────────────────────────
# Path to SQLite database file; use ":memory:" for in-memory (lost on restart)
DATABASE_PATH = _str("DATABASE_PATH", "monitoring.db")
