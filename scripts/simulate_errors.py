"""
Error simulator: POSTs realistic error events to POST /errors to demonstrate
the full monitoring pipeline (ingestion → aggregation → patterns → alerting).

Usage:
    python scripts/simulate_errors.py [--scenario SCENARIO] [--host HOST] [--port PORT]

Scenarios:
    all              – run all scenarios in sequence (default)
    spike            – sudden spike in errors for atlas-api
    repeated         – same error repeating in report-pipeline
    high_freq        – sustained high error frequency
    predictive       – gradual upward trend that triggers predictive alert
    ratio            – 2-out-of-3 failure ratio on a specific endpoint
    recovery         – errors that spike then stop (triggers "resolved")
    custom_threshold – configure low threshold via Admin API, send fewer errors,
                       verify alert fires at custom level not the default
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone, timedelta

import httpx

BASE_URL = "http://localhost:8000"
HEADERS = {"Content-Type": "application/json"}


def admin_put_rules(service: str, rules: dict, host: str = BASE_URL) -> None:
    """Set service-level rules via Admin API (PUT /admin/rules/{service})."""
    url = f"{host}/admin/rules/{service}"
    try:
        resp = httpx.put(url, json={"rules": rules}, headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            print(f"  ✓ Admin rules set for '{service}': {rules}")
        else:
            print(f"  ✗ Admin PUT failed [{resp.status_code}]: {resp.text}")
    except Exception as exc:
        print(f"  ✗ Admin PUT request failed: {exc}")


def admin_delete_rule(service: str, rule_key: str, host: str = BASE_URL) -> None:
    """Delete a service-level rule (reverts to global default)."""
    url = f"{host}/admin/rules/{service}/{rule_key}"
    try:
        resp = httpx.delete(url, headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            print(f"  ✓ Admin rule '{rule_key}' deleted for '{service}' (reverted to default)")
        else:
            print(f"  ✗ Admin DELETE failed [{resp.status_code}]: {resp.text}")
    except Exception as exc:
        print(f"  ✗ Admin DELETE request failed: {exc}")


def post_error(
    service: str,
    message: str,
    endpoint: str | None = None,
    timestamp: datetime | None = None,
    host: str = BASE_URL,
) -> None:
    payload = {
        "service": service,
        "message": message,
        "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
    }
    if endpoint:
        payload["endpoint"] = endpoint

    try:
        resp = httpx.post(f"{host}/errors", json=payload, headers=HEADERS, timeout=5)
        status = resp.status_code
        marker = "✓" if status == 202 else "✗"
        print(f"  {marker} [{status}] {service}{f' ({endpoint})' if endpoint else ''} – {message[:60]}")
    except Exception as exc:
        print(f"  ✗ Request failed: {exc}")


def scenario_spike(host: str) -> None:
    """
    Burst of errors on atlas-api / POST /reports/generate.

    Triggers on this run:
      • high_frequency  (33 errors ≥ threshold 10)
      • failure_ratio   (33 errors ≥ 0.67 × 3 = 2.01)

    The SPIKE pattern (current/previous ratio ≥ 2.0) fires on a *second* run
    after this window's events age out — the 3 baseline errors become the
    "previous window" and the 30-error burst becomes the new spike.
    """
    print("\n── Scenario: SPIKE ──────────────────────────────────────────")
    print("Sending 3 baseline errors (same endpoint as spike)…")
    for i in range(3):
        post_error("atlas-api", "Database query timeout", "POST /reports/generate", host=host)
        time.sleep(0.1)

    print("Sending 30 rapid errors (burst)…")
    for i in range(30):
        post_error("atlas-api", "Connection refused", "POST /reports/generate", host=host)
        time.sleep(0.05)
    print("Done. high_frequency + failure_ratio alert on next tick.")


def scenario_repeated(host: str) -> None:
    """Same LLM timeout error repeating in report-pipeline."""
    print("\n── Scenario: REPEATED IDENTICAL ─────────────────────────────")
    print("Sending 15x same error from report-pipeline…")
    for i in range(15):
        post_error(
            "report-pipeline",
            "LLM timeout: model did not respond within 30s",
            host=host,
        )
        time.sleep(0.1)
    print("Done. Repeated identical scenario complete.")


def scenario_high_frequency(host: str) -> None:
    """
    High error rate across multiple services.

    Sends 51 errors (17 per service) so each service exceeds its
    high_frequency_threshold (atlas-api/GET stream: 15, advisor-api: 15,
    chat-pipeline: 15).
    """
    print("\n── Scenario: HIGH FREQUENCY ─────────────────────────────────")
    services = [
        ("atlas-api",    "Rate limit exceeded",    "GET /stream"),
        ("advisor-api",  "Authentication failure", "POST /auth/login"),
        ("chat-pipeline","Model unavailable",       None),
    ]
    print("Sending 51 errors spread across 3 services (17 each)…")
    for i in range(51):
        svc, msg, ep = services[i % len(services)]
        post_error(svc, msg, ep, host=host)
        time.sleep(0.05)
    print("Done. high_frequency alert fires for all 3 services on next tick.")


def scenario_predictive(host: str, pause_seconds: int = 65) -> None:
    """
    Gradual upward trend across consecutive scheduler windows.

    Sends 3 batches of 3 → 8 → 20 errors, each with current timestamps so
    they land in the active aggregation window.  Between batches the script
    waits pause_seconds so that the scheduler fires and records each count in
    its rolling history.  After all 3 batches the predictive module sees counts
    [3, 8, 20] across 3 windows and fires rate_increase + upward_trend alerts.

    pause_seconds=0 is only used by scenario_all which intentionally skips this
    scenario — run it standalone for full effect:
        python scripts/simulate_errors.py --scenario predictive
    """
    print("\n── Scenario: PREDICTIVE (upward trend) ─────────────────────")

    if pause_seconds == 0:
        print("Skipped in 'all' mode — needs consecutive ticks. Run standalone.")
        print("  python scripts/simulate_errors.py --scenario predictive")
        return

    batches = [3, 8, 20]  # errors per window: 3 → 8 → 20 (upward trend)
    print(f"Sending 3 batches: {batches}  (waiting {pause_seconds}s between each)")

    for batch_i, count in enumerate(batches):
        print(f"\n  Batch {batch_i + 1}/{len(batches)}: {count} errors…")
        for j in range(count):
            post_error(
                "report-pipeline",
                "Downstream service degraded",
                host=host,
                # Use current time so events fall inside the active 5-min window
                timestamp=datetime.now(timezone.utc),
            )
            time.sleep(0.03)

        if batch_i < len(batches) - 1:
            print(f"  Waiting {pause_seconds}s for scheduler tick…")
            time.sleep(pause_seconds)

    print("\nPredictive scenario complete.")
    print("Wait one more tick to see potential_outage alert in server logs.")


def scenario_failure_ratio(host: str) -> None:
    """2-out-of-3 failure ratio on a specific endpoint."""
    print("\n── Scenario: FAILURE RATIO (2 out of 3) ────────────────────")
    print("Sending errors for POST /reports/generate (threshold: 2/3 = 67%)…")
    for i in range(6):
        post_error(
            "atlas-api",
            "LLM timeout",
            "POST /reports/generate",
            host=host,
        )
        time.sleep(0.1)
    print("Done. Failure ratio scenario complete.")


def scenario_recovery(host: str, pause_seconds: int = 70) -> None:
    """Spike then stop – should trigger alert then resolved."""
    print("\n── Scenario: RECOVERY (spike → resolved) ────────────────────")
    print("Sending 20 errors to trigger alert…")
    for i in range(20):
        post_error("chat-pipeline", "Model crashed", host=host)
        time.sleep(0.05)
    print(f"Errors sent. Now waiting {pause_seconds}s for errors to age out of window…")
    print("(The next 2 clean aggregation windows will trigger a RESOLVED notification)")
    if pause_seconds > 0:
        time.sleep(pause_seconds)
    print("Recovery scenario complete.")


def scenario_custom_threshold(host: str) -> None:
    """
    Demonstrate client-configurable thresholds via the Admin API.

    Steps:
      1. Read current (default) rules for 'advisor-api' — default
         high_frequency_threshold is 15.
      2. Lower it to 5 via PUT /admin/rules/advisor-api.
      3. Send only 8 errors — enough to breach the custom threshold (5)
         but well below the original default (15).
      4. Wait for a scheduler tick to confirm the alert fires.
      5. Restore the default by deleting the custom rule.

    Also sets spike_ratio_threshold=1.5 (default 2.0) and sends a small
    baseline + spike burst so the spike pattern fires at the lower ratio.
    """
    SERVICE = "advisor-api"
    ENDPOINT = "POST /auth/login"

    print("\n── Scenario: CUSTOM THRESHOLD ───────────────────────────────")

    # ── Step 1: show current default rules ───────────────────────────────────
    try:
        resp = httpx.get(f"{host}/admin/rules/{SERVICE}", headers=HEADERS, timeout=5)
        current = resp.json()
        print(f"\nCurrent rules for '{SERVICE}':")
        print(f"  high_frequency_threshold = {current.get('high_frequency_threshold')}  (default)")
        print(f"  spike_ratio_threshold    = {current.get('spike_ratio_threshold')}  (default)")
        print(f"  spike_min_current_count  = {current.get('spike_min_current_count')}  (default)")
    except Exception as exc:
        print(f"  ✗ Could not fetch rules: {exc}")

    # ── Step 2: set custom (lower) thresholds ────────────────────────────────
    print(f"\nSetting custom thresholds for '{SERVICE}' via Admin API…")
    admin_put_rules(SERVICE, {
        "high_frequency_threshold": 5,   # fire after 5 errors, not 15
        "spike_ratio_threshold": 1.5,    # fire at 1.5× increase, not 2.0×
        "spike_min_current_count": 3,    # spike needs only 3 current errors
    }, host=host)

    # ── Step 3a: high-frequency test ─────────────────────────────────────────
    print(f"\n[Part A] Sending 8 errors — above custom threshold (5), below default (15)…")
    for i in range(8):
        post_error(SERVICE, "Auth service degraded", ENDPOINT, host=host)
        time.sleep(0.1)
    print("  → Expect: high_frequency alert at custom threshold=5 on next tick")
    print("  → Without custom rule this would be SUPPRESSED (default=15)")

    # ── Step 3b: spike test ───────────────────────────────────────────────────
    print(f"\n[Part B] Sending spike: 2 baseline + 6 burst on same endpoint…")
    print("  (baseline establishes previous_count=2 in aggregator history)")
    for i in range(2):
        post_error(SERVICE, "Baseline error", ENDPOINT, host=host)
        time.sleep(0.1)
    print("  Burst (6 errors — 3× the baseline, fires at custom ratio 1.5×)…")
    for i in range(6):
        post_error(SERVICE, "Spike error", ENDPOINT, host=host)
        time.sleep(0.05)
    print("  → Expect: spike alert at custom ratio=1.5 on second tick")

    print(f"\nWaiting for scheduler tick — watch server logs for alerts…")
    print(f"  Custom rules active: high_frequency_threshold=5, spike_ratio_threshold=1.5")

    # ── Step 4 (cleanup note) ─────────────────────────────────────────────────
    print(f"\n[Cleanup] Restoring default rules for '{SERVICE}'…")
    for key in ("high_frequency_threshold", "spike_ratio_threshold", "spike_min_current_count"):
        admin_delete_rule(SERVICE, key, host=host)

    try:
        resp = httpx.get(f"{host}/admin/rules/{SERVICE}", headers=HEADERS, timeout=5)
        restored = resp.json()
        print(f"\nRestored rules for '{SERVICE}':")
        print(f"  high_frequency_threshold = {restored.get('high_frequency_threshold')}  (back to default)")
        print(f"  spike_ratio_threshold    = {restored.get('spike_ratio_threshold')}  (back to default)")
    except Exception as exc:
        print(f"  ✗ Could not fetch restored rules: {exc}")

    print("\nCustom threshold scenario complete.")
    print("Check server logs — alert should show: threshold=5, not threshold=15.")


def scenario_all(host: str) -> None:
    """
    Runs all quick scenarios back-to-back.

    Predictive is excluded here because it requires multiple scheduler ticks
    (each 60 s apart by default).  Run it standalone:
        python scripts/simulate_errors.py --scenario predictive
    """
    scenario_spike(host)
    time.sleep(1)
    scenario_repeated(host)
    time.sleep(1)
    scenario_high_frequency(host)
    time.sleep(1)
    scenario_failure_ratio(host)
    time.sleep(1)
    scenario_recovery(host, pause_seconds=0)
    time.sleep(1)
    scenario_custom_threshold(host)
    print("\n── All quick scenarios sent. Wait for next scheduler tick to see alerts.")
    print("── Predictive testing: python scripts/simulate_errors.py --scenario predictive")


SCENARIOS = {
    "all": scenario_all,
    "spike": scenario_spike,
    "repeated": scenario_repeated,
    "high_freq": scenario_high_frequency,
    "predictive": scenario_predictive,
    "ratio": scenario_failure_ratio,
    "recovery": scenario_recovery,
    "custom_threshold": scenario_custom_threshold,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Error simulator for monitoring system")
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        default="all",
        help="Scenario to run (default: all)",
    )
    parser.add_argument("--host", default=BASE_URL, help=f"Base URL (default: {BASE_URL})")
    args = parser.parse_args()

    print(f"Targeting: {args.host}")
    print(f"Running scenario: {args.scenario}\n")

    fn = SCENARIOS[args.scenario]
    # Only pass host (pause_seconds uses default for non-interactive run)
    fn(args.host)

    print("\nDone. Check server logs or GET /status for active incidents.")


if __name__ == "__main__":
    main()
