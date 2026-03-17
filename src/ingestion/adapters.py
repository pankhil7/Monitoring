"""
Per-client payload adapters for the ingestion layer.

Each adapter normalises a client-specific payload shape into the canonical
dict expected by ErrorEventRequest:
  { service, endpoint?, message, timestamp }

Usage:
  client_type = request.headers.get("X-Client-Type", "canonical")
  adapter     = get_adapter(client_type)
  canonical   = adapter.normalise(raw_body)
  payload     = ErrorEventRequest(**canonical)

To add a new client type:
  1. Create a class that inherits BaseAdapter and implements normalise().
  2. Register it in ADAPTER_REGISTRY with a string key.
  3. Client sends that key in the X-Client-Type header.

Supported client types:
  canonical   – default; fields already match our schema
  sentry      – Sentry webhook / error event format
  datadog     – Datadog event/log format
  cloudwatch  – AWS CloudWatch alarm notification
  custom      – flat dict with arbitrary field names (mapped via X-Field-Map header)
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Base ───────────────────────────────────────────────────────────────────────

class BaseAdapter(ABC):
    """All adapters must implement normalise() and return a canonical dict."""

    @abstractmethod
    def normalise(self, raw: dict, field_map: Optional[dict] = None) -> dict:
        """
        Convert client-specific payload to canonical fields.

        Returns:
            dict with keys: service (str), message (str), timestamp (str),
                            endpoint (str | None)
        Raises:
            AdapterError: if required fields cannot be mapped.
        """

    @staticmethod
    def _require(value: Any, field: str) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise AdapterError(f"Required field '{field}' is missing or empty")
        return value


class AdapterError(ValueError):
    """Raised when a payload cannot be normalised."""


# ── Canonical (default) ────────────────────────────────────────────────────────

class CanonicalAdapter(BaseAdapter):
    """
    Passes the payload through as-is.
    Expected fields: service, message, timestamp, endpoint? (already canonical).
    """

    def normalise(self, raw: dict, field_map: Optional[dict] = None) -> dict:
        return {
            "service":   self._require(raw.get("service"), "service"),
            "message":   self._require(raw.get("message"), "message"),
            "timestamp": self._require(raw.get("timestamp"), "timestamp"),
            "endpoint":  raw.get("endpoint"),
        }


# ── Sentry ─────────────────────────────────────────────────────────────────────

class SentryAdapter(BaseAdapter):
    """
    Sentry webhook / Sentry SDK error event shape.

    Expected keys:
      project         → service
      platform        → ignored
      exception.values[0].value  → message (first exception value)
      timestamp       → timestamp (ISO 8601)
      request.url     → endpoint (optional)
      transaction     → endpoint fallback (optional)

    Example payload:
    {
      "project": "atlas-api",
      "timestamp": "2026-03-17T10:00:00Z",
      "exception": {
        "values": [{ "type": "TimeoutError", "value": "LLM timeout after 30s" }]
      },
      "request": { "url": "/reports/generate", "method": "POST" },
      "transaction": "POST /reports/generate"
    }
    """

    def normalise(self, raw: dict, field_map: Optional[dict] = None) -> dict:
        service = self._require(raw.get("project"), "project")

        # Extract message from exception values or top-level message field
        message = raw.get("message")
        if not message:
            exc = raw.get("exception", {})
            values = exc.get("values", [])
            if values:
                v = values[0]
                exc_type = v.get("type", "")
                exc_val = v.get("value", "")
                message = f"{exc_type}: {exc_val}".strip(": ") if exc_type else exc_val
        self._require(message, "exception.values[0].value or message")

        timestamp = self._require(raw.get("timestamp"), "timestamp")

        # Prefer transaction field, fall back to request url+method
        endpoint = raw.get("transaction")
        if not endpoint:
            req = raw.get("request", {})
            url = req.get("url", "")
            method = req.get("method", "")
            if url:
                endpoint = f"{method} {url}".strip() if method else url

        return {
            "service":   service,
            "message":   message,
            "timestamp": timestamp,
            "endpoint":  endpoint or None,
        }


# ── Datadog ───────────────────────────────────────────────────────────────────

class DatadogAdapter(BaseAdapter):
    """
    Datadog log / event format.

    Expected keys:
      service         → service
      message / text  → message
      date            → timestamp (unix epoch or ISO)
      http.url_details.path + http.method → endpoint (optional)
      @network.client.service → service fallback

    Example payload:
    {
      "service": "atlas-api",
      "message": "LLM timeout",
      "date": 1710676800000,
      "http": {
        "method": "POST",
        "url_details": { "path": "/reports/generate" }
      }
    }
    """

    def normalise(self, raw: dict, field_map: Optional[dict] = None) -> dict:
        service = (
            raw.get("service")
            or raw.get("@network.client.service")
        )
        self._require(service, "service")

        message = raw.get("message") or raw.get("text")
        self._require(message, "message or text")

        # Datadog uses "date" as epoch ms or ISO string
        raw_ts = raw.get("date") or raw.get("timestamp")
        self._require(raw_ts, "date or timestamp")
        timestamp = _epoch_ms_to_iso(raw_ts) if isinstance(raw_ts, (int, float)) else str(raw_ts)

        # Build endpoint from http fields
        endpoint = None
        http = raw.get("http", {})
        path = http.get("url_details", {}).get("path") or http.get("url")
        method = http.get("method", "")
        if path:
            endpoint = f"{method} {path}".strip() if method else path

        return {
            "service":   service,
            "message":   message,
            "timestamp": timestamp,
            "endpoint":  endpoint,
        }


# ── CloudWatch ────────────────────────────────────────────────────────────────

class CloudWatchAdapter(BaseAdapter):
    """
    AWS CloudWatch Alarm SNS notification shape.

    Expected keys (inside SNS Message JSON):
      AlarmName       → service (or mapped via field_map)
      NewStateReason  → message
      StateChangeTime → timestamp
      AlarmDescription → endpoint (optional)

    Example payload (the parsed SNS Message body):
    {
      "AlarmName": "atlas-api-error-rate",
      "NewStateReason": "Threshold crossed: 3 datapoints > 10",
      "StateChangeTime": "2026-03-17T10:00:00.000+0000",
      "AlarmDescription": "POST /reports/generate"
    }
    """

    def normalise(self, raw: dict, field_map: Optional[dict] = None) -> dict:
        # Support SNS wrapper (Message is a JSON string inside SNS envelope)
        if "Message" in raw and isinstance(raw["Message"], str):
            try:
                raw = json.loads(raw["Message"])
            except (json.JSONDecodeError, TypeError):
                pass

        alarm_name = self._require(raw.get("AlarmName"), "AlarmName")
        # Strip common suffixes to get a clean service name
        service = alarm_name.replace("-error-rate", "").replace("-alarm", "").strip("-")

        message = self._require(raw.get("NewStateReason"), "NewStateReason")
        timestamp = self._require(raw.get("StateChangeTime"), "StateChangeTime")
        endpoint = raw.get("AlarmDescription") or None

        return {
            "service":   service,
            "message":   message,
            "timestamp": timestamp,
            "endpoint":  endpoint,
        }


# ── Custom ────────────────────────────────────────────────────────────────────

class CustomAdapter(BaseAdapter):
    """
    Generic adapter for any flat dict with non-standard field names.

    The caller provides a field_map (via X-Field-Map header as JSON) that maps
    canonical field names to the client's actual field names. Example:
      X-Field-Map: {"service": "source", "message": "error_text", "timestamp": "occurred_at"}

    Falls back to canonical names if field_map is not provided.
    """

    def normalise(self, raw: dict, field_map: Optional[dict] = None) -> dict:
        fm = field_map or {}

        def _get(canonical_key: str) -> Any:
            client_key = fm.get(canonical_key, canonical_key)
            return raw.get(client_key)

        return {
            "service":   self._require(_get("service"), fm.get("service", "service")),
            "message":   self._require(_get("message"), fm.get("message", "message")),
            "timestamp": self._require(_get("timestamp"), fm.get("timestamp", "timestamp")),
            "endpoint":  _get("endpoint"),
        }


# ── Registry ──────────────────────────────────────────────────────────────────

ADAPTER_REGISTRY: dict[str, BaseAdapter] = {
    "canonical":   CanonicalAdapter(),
    "sentry":      SentryAdapter(),
    "datadog":     DatadogAdapter(),
    "cloudwatch":  CloudWatchAdapter(),
    "custom":      CustomAdapter(),
}

# Signatures used by auto-detection: list of field sets that uniquely identify
# a payload format. The first match wins.
_AUTO_DETECT_SIGNATURES: list[tuple[str, set[str]]] = [
    ("sentry",      {"project", "exception"}),
    ("sentry",      {"project", "message", "timestamp"}),
    ("cloudwatch",  {"AlarmName", "NewStateReason", "StateChangeTime"}),
    ("cloudwatch",  {"AlarmName", "StateChangeTime"}),
    ("datadog",     {"service", "date", "host"}),
    ("datadog",     {"service", "message", "date"}),
]


def _auto_detect(raw: dict) -> Optional[str]:
    """
    Attempt to identify the payload format from its field names.
    Returns the client type string, or None if no signature matched.
    """
    raw_keys = set(raw.keys())
    for client_type, signature in _AUTO_DETECT_SIGNATURES:
        if signature.issubset(raw_keys):
            return client_type
    return None


def get_adapter(client_type: str, raw: Optional[dict] = None) -> tuple[BaseAdapter, str]:
    """
    Return (adapter, resolved_client_type) for the given client type.

    Resolution order:
      1. Exact match in ADAPTER_REGISTRY  → use as-is.
      2. Unknown type + raw body provided → try auto-detection by field signatures.
      3. Auto-detection inconclusive      → fall back to CanonicalAdapter (log warning).

    Never raises for an unknown client type — always returns a usable adapter.
    The resolved_client_type reflects what was actually used (useful for logging).
    """
    key = client_type.lower()
    adapter = ADAPTER_REGISTRY.get(key)
    if adapter is not None:
        logger.debug("Adapter resolved: client_type='%s'", key)
        return adapter, key

    # Unknown type — try auto-detection
    detected: Optional[str] = None
    if raw is not None:
        detected = _auto_detect(raw)

    if detected:
        logger.warning(
            "Unknown X-Client-Type '%s'; auto-detected as '%s' from payload fields.",
            client_type, detected,
        )
        return ADAPTER_REGISTRY[detected], detected

    # Final fallback: canonical
    logger.warning(
        "Unknown X-Client-Type '%s'; auto-detection inconclusive. "
        "Falling back to canonical adapter. Known types: %s",
        client_type,
        list(ADAPTER_REGISTRY.keys()),
    )
    return ADAPTER_REGISTRY["canonical"], "canonical (fallback)"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _epoch_ms_to_iso(epoch_ms: int | float) -> str:
    """Convert Unix epoch milliseconds to ISO 8601 string."""
    from datetime import datetime, timezone
    epoch_s = epoch_ms / 1000 if epoch_ms > 1e10 else epoch_ms
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()
