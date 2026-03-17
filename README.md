# Intelligent Error Monitoring & Predictive Alerting System

A prototype monitoring system for AdvisoryAI that captures errors from multiple services, aggregates and classifies them, detects abnormal patterns, and predicts potential outages before they happen.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            EXTERNAL SOURCES                                  │
│  APIs (P0)  │  AI Pipelines (P0)  │  WebSocket/SSE (P0/P1)  │  Jobs (P1/P2) │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │  POST /errors
                               │  Headers: X-Client-Type, X-Field-Map (optional)
                               │  Body:  { service, endpoint?, message, timestamp }
                               │         (no severity — derived internally)
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            INGESTION LAYER                                   │
│                                                                             │
│  ┌─────────────────┐   ┌──────────────────────────────────────────────┐    │
│  │  Ingestion API  │   │  Client Adapters  (normalise payload shape)  │    │
│  │  POST /errors   │──▶│                                              │    │
│  │  GET  /health   │   │  canonical │ sentry │ datadog │ cloudwatch   │    │
│  │  GET  /status   │   │  custom (field-map) │ auto-detect │ fallback  │    │
│  │  GET  /adapters │   └──────────────────────┬───────────────────────┘    │
│  └─────────────────┘                          │  canonical ErrorEvent       │
│                                               ▼                            │
│                                  ┌────────────────────┐                    │
│                                  │  Validation        │                    │
│                                  │  (schema, timestamp│                    │
│                                  │   service name)    │                    │
│                                  └─────────┬──────────┘                    │
│                                            │                               │
│                                            ▼                               │
│                                  ┌────────────────────┐                    │
│                                  │  EventStore        │                    │
│                                  │  (SQLite)          │                    │
│                                  │  events table      │                    │
│                                  └────────────────────┘                    │
└─────────────────────────────────────────────────────────────────────────────┘
                                            │ read events (last N minutes)
                                            │
                           ┌────────────────┘
                           │  APScheduler fires every 60s
                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      AGGREGATION & PROCESSING LAYER                          │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Aggregator                                                          │   │
│  │  • Service-level  → (service, window)          → count, rate        │   │
│  │  • Endpoint-level → (service, endpoint, window) → count, rate       │   │
│  └───────────────────────────┬─────────────────────────────────────────┘   │
│                              │  AggregateRecords + rolling history          │
│               ┌──────────────┴──────────────┐                              │
│               ▼                             ▼                              │
│  ┌────────────────────────┐   ┌──────────────────────────┐                 │
│  │  Pattern Detector      │   │  Predictive Module       │                 │
│  │  (reactive)            │   │  (proactive)             │                 │
│  │  • high_frequency      │   │  • rate_increase         │                 │
│  │  • repeated_identical  │   │  • upward_trend          │                 │
│  │  • spike               │   │  • repeated_failures     │                 │
│  │  • failure_ratio       │   │  → potential_outage (P1) │                 │
│  └────────────┬───────────┘   └─────────────┬────────────┘                 │
│               │    ↑ reads thresholds        │    ↑ reads thresholds        │
│               │    └──────────────┐          │    ┘                        │
│               │          ┌────────┴──────────┐                             │
│               │          │   RulesStore      │◀── Admin API (live updates) │
│               │          │   (SQLite +       │    PUT/PATCH/DELETE          │
│               │          │    TTL cache)     │    /admin/rules/...          │
│               │          │   Global defaults │                             │
│               │          │   → Service rules │                             │
│               │          │   → Endpoint rules│                             │
│               │          └───────────────────┘                             │
│               └──────────────────┬───────────────────────────────────────  │
│                                  │  AlertEvents                            │
│                                  │  (severity = f(count, pattern_type))   │
└──────────────────────────────────┼──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ALERTING LAYER                                    │
│                                                                             │
│  ┌───────────────────────┐  ┌──────────────────────┐  ┌─────────────────┐  │
│  │  Noise Reduction      │─▶│  Message Builder     │─▶│  Notifier       │  │
│  │  • alert_key          │  │  • Engineering copy  │  │  • Slack        │  │
│  │    (svc+ep+pattern    │  │    (technical detail)│  │  • Email        │  │
│  │     +message[:64])    │  │  • Business copy     │  │  • Console      │  │
│  │  • cooldown window    │  │    (user impact)     │  │  retry/backoff  │  │
│  │  • re-alert on worsen │  └──────────────────────┘  └─────────────────┘  │
│  │  • RESOLVED after N   │                                                 │
│  │    clean windows      │                                                 │
│  └───────────────────────┘                                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Severity derivation (internal — no client input)

```
client sends: { service, endpoint?, message, timestamp }   ← NO severity field

aggregation derives severity from count + pattern type:

  count in window ≥ 50  ──────────────────────────────▶  P0
  count in window ≥ 10  ──────────────────────────────▶  P1
  count in window <  10  ─────────────────────────────▶  P2

  spike pattern          ──── escalate 1 level ────────▶  P2→P1, P1→P0
  failure_ratio pattern  ──── minimum P1 ──────────────▶  never below P1
  potential_outage       ──── always P1 (warning) ─────▶  P1
```

Thresholds are configurable per-service and per-endpoint via the Admin API (live, no restart needed).

---

## Rules resolution flow

```
PATCH /admin/rules/atlas-api/high_frequency_threshold  { "value": 5 }
          │
          ▼
  RulesStore.set_rule()  →  service_rules table (SQLite)
  Cache invalidated for "atlas-api::*"
          │
          ▼  (next aggregation tick, within 60s)
  get_rules("atlas-api", "POST /reports/generate")
          │
          ▼  merge in priority order:
  Global defaults  (service IS NULL)
    +  Service rules  (service = "atlas-api")
    +  Endpoint rules (service = "atlas-api", endpoint = "POST /reports/generate")
          │
          ▼
  { high_frequency_threshold: 5, spike_ratio_threshold: 2.0, ... }
```

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — set SLACK_WEBHOOK_URL, email, thresholds etc.

# 3. Start the monitoring server
python3 -m uvicorn src.app:app --port 8000

# 4. In a separate terminal, run the error simulator
python3 scripts/simulate_errors.py --scenario all
```

The server starts on `http://localhost:8000`.

---

## Testing with the simulator

`scripts/simulate_errors.py` sends realistic error events to the running server so you can observe the full pipeline (ingestion → aggregation → patterns → alerting) in the server logs.

### Workflow for each scenario

```
1. Clear the DB between runs (avoids cooldown suppression from previous incidents)
2. Run the scenario
3. Wait up to 60s for the scheduler tick
4. Watch server logs for Pattern / NEW incident / ALERT lines
```

**Clear the DB between runs:**

```bash
python3 -c "import sqlite3; c=sqlite3.connect('monitoring.db'); c.execute('DELETE FROM events'); c.commit()"
```

---

### Available scenarios

| Scenario | Flag | What it does | Alert(s) expected |
|---|---|---|---|
| High frequency | `--scenario high_freq` | 51 errors across 3 services (17 each) — above per-service threshold | `high_frequency` + `repeated_identical` for all 3 |
| Repeated identical | `--scenario repeated` | Same `LLM timeout` error 15× on `report-pipeline` | `high_frequency` + `repeated_identical` |
| Spike | `--scenario spike` | 3 baseline + 30 burst errors on same endpoint | `high_frequency` + `repeated_identical` + `failure_ratio` |
| Failure ratio | `--scenario ratio` | 6 errors on `POST /reports/generate` (100% of requests) | `failure_ratio` + `repeated_identical` |
| Recovery | `--scenario recovery` | 20 errors then silence — alerts fire then resolve | `high_frequency` → `RESOLVED` after 2 clean ticks |
| Custom threshold | `--scenario custom_threshold` | Lowers `high_frequency_threshold` from 15 → 5 via Admin API, sends 8 errors — only fires because of the custom rule | `high_frequency` + `repeated_identical` at threshold=5 |
| Predictive | `--scenario predictive` | 3 batches (3 → 8 → 20 errors) with a 65s pause between each so the scheduler builds history | `potential_outage` (predictive) |
| All (quick) | `--scenario all` | Runs all except predictive back-to-back | All of the above |

---

### Running each scenario

**1. High frequency — sustained errors across multiple services**

```bash
# Clear DB first
python3 -c "import sqlite3; c=sqlite3.connect('monitoring.db'); c.execute('DELETE FROM events'); c.commit()"
python3 scripts/simulate_errors.py --scenario high_freq
# Wait ~60s → server logs show:
# Pattern high_frequency: atlas-api::* count=17 threshold=15
# Pattern high_frequency: advisor-api::* count=17 threshold=15
# Pattern high_frequency: chat-pipeline::* count=17 threshold=15
```

**2. Repeated identical — same error message over and over**

```bash
python3 -c "import sqlite3; c=sqlite3.connect('monitoring.db'); c.execute('DELETE FROM events'); c.commit()"
python3 scripts/simulate_errors.py --scenario repeated
# Wait ~60s → server logs show:
# Pattern repeated_identical: report-pipeline::* msg='LLM timeout...' count=15 threshold=3
```

**3. Spike — sudden burst above baseline**

```bash
python3 -c "import sqlite3; c=sqlite3.connect('monitoring.db'); c.execute('DELETE FROM events'); c.commit()"
python3 scripts/simulate_errors.py --scenario spike
# Wait ~60s → server logs show:
# Pattern high_frequency: atlas-api::POST /reports/generate count=33 threshold=10
# Pattern failure_ratio:  atlas-api::POST /reports/generate ... approx_ratio=1.00
```

**4. Failure ratio — every request is failing**

```bash
python3 -c "import sqlite3; c=sqlite3.connect('monitoring.db'); c.execute('DELETE FROM events'); c.commit()"
python3 scripts/simulate_errors.py --scenario ratio
# Wait ~60s → server logs show:
# Pattern failure_ratio: atlas-api::POST /reports/generate count=6 threshold=0.67 approx_ratio=1.00
# Engineering: [P1] HIGH FAILURE RATE ... Failure ratio: 100%
```

**5. Recovery — alert then silence → RESOLVED**

```bash
python3 -c "import sqlite3; c=sqlite3.connect('monitoring.db'); c.execute('DELETE FROM events'); c.commit()"
python3 scripts/simulate_errors.py --scenario recovery
# Script sends 20 errors then waits 70s internally
# Tick 1 (~60s): alert fires → NEW incident
# Tick 2 (~120s): no new errors → clean_window_count = 1
# Tick 3 (~180s): still clean → RESOLVED notification fires
# Server logs show:
# RESOLVED incident: chat-pipeline::*::high_frequency
# Engineering: [RESOLVED] chat-pipeline: error rate has returned to normal.
```

**6. Custom threshold — live rule update via Admin API**

```bash
python3 -c "import sqlite3; c=sqlite3.connect('monitoring.db'); c.execute('DELETE FROM events'); c.commit()"
python3 scripts/simulate_errors.py --scenario custom_threshold
# Script automatically:
#   1. Reads current defaults for advisor-api (high_frequency_threshold=15)
#   2. Sets custom threshold=5 via PUT /admin/rules/advisor-api
#   3. Sends 8 errors (above 5, below default 15)
#   4. Restores defaults via DELETE /admin/rules/advisor-api/...
# Wait ~60s → server logs show:
# AUDIT rule_change: service=advisor-api key=high_frequency_threshold value=5
# Pattern high_frequency: advisor-api::* count=8 threshold=5   ← custom threshold used
# AUDIT rule_delete: service=advisor-api key=high_frequency_threshold (reverted to global default)
```

**7. Predictive — gradual upward trend (run standalone, takes ~3 min)**

```bash
python3 -c "import sqlite3; c=sqlite3.connect('monitoring.db'); c.execute('DELETE FROM events'); c.commit()"
python3 scripts/simulate_errors.py --scenario predictive
# Sends 3 batches with 65s pause between each:
#   Batch 1:  3 errors  → count=3
#   Batch 2:  8 errors  → count=8   (rate increasing)
#   Batch 3: 20 errors  → count=20  (upward trend confirmed)
# After batch 3, next tick fires:
# Pattern potential_outage (predictive): rate_increase + upward_trend
# Engineering: [WARNING] [P1] POTENTIAL OUTAGE RISK ...
```

> **Note on cooldowns:** If you run scenarios back-to-back without clearing the DB, the noise reducer's in-memory incident state may suppress re-alerts during the cooldown window (default 15 min). Always clear the DB between isolated scenario runs for a clean result.

---

## API reference

### Ingestion

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/errors` | Ingest an error event |
| `GET` | `/health` | Health check |
| `GET` | `/status` | Active incidents and recent alerts |
| `GET` | `/adapters` | List supported client types |

**POST /errors — headers:**

| Header | Required | Default | Description |
|--------|----------|---------|-------------|
| `X-Client-Type` | No | `canonical` | Payload format: `canonical` \| `sentry` \| `datadog` \| `cloudwatch` \| `custom`. Unknown values trigger auto-detection then canonical fallback — never a hard rejection. |
| `X-Field-Map` | No | — | JSON object mapping canonical fields to your field names (for `custom` type only) |

**Canonical payload (default):**

```bash
curl -X POST http://localhost:8000/errors \
  -H "Content-Type: application/json" \
  -d '{"service": "atlas-api", "endpoint": "POST /reports/generate", "message": "LLM timeout", "timestamp": "2026-03-17T10:00:00Z"}'
```

**Sentry webhook:**

```bash
curl -X POST http://localhost:8000/errors \
  -H "Content-Type: application/json" \
  -H "X-Client-Type: sentry" \
  -d '{
    "project": "atlas-api",
    "timestamp": "2026-03-17T10:00:00Z",
    "exception": {"values": [{"type": "TimeoutError", "value": "LLM timeout after 30s"}]},
    "transaction": "POST /reports/generate"
  }'
```

**Datadog log:**

```bash
curl -X POST http://localhost:8000/errors \
  -H "Content-Type: application/json" \
  -H "X-Client-Type: datadog" \
  -d '{
    "service": "atlas-api",
    "message": "LLM timeout",
    "date": 1710676800000,
    "http": {"method": "POST", "url_details": {"path": "/reports/generate"}}
  }'
```

**Custom (arbitrary field names):**

```bash
curl -X POST http://localhost:8000/errors \
  -H "Content-Type: application/json" \
  -H "X-Client-Type: custom" \
  -H 'X-Field-Map: {"service": "source", "message": "error_text", "timestamp": "occurred_at"}' \
  -d '{"source": "atlas-api", "error_text": "LLM timeout", "occurred_at": "2026-03-17T10:00:00Z"}'
```

> **Unknown client type?** If `X-Client-Type` is not in the registry, the system auto-detects the format from the payload's field names (e.g. `project + exception` → Sentry). If detection is inconclusive it falls back to the canonical adapter silently — the request is never rejected solely because of an unrecognised header value. When a fallback is used, the `202` response includes `adapter_used` and `adapter_note` fields so the caller knows what happened.

> Severity is **not** accepted from clients — derived internally from error count and pattern type.

### Admin — Rules management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/rules` | All rules in DB |
| `GET` | `/admin/rules/defaults` | Current global defaults |
| `GET` | `/admin/rules/schema` | Valid rule keys and types |
| `GET` | `/admin/rules/{service}` | Merged rules for a service |
| `GET` | `/admin/rules/{service}/{endpoint}` | Merged rules for an endpoint |
| `PUT` | `/admin/rules/{service}` | Bulk set service-level rules |
| `PUT` | `/admin/rules/{service}/{endpoint}` | Bulk set endpoint-level rules |
| `PATCH` | `/admin/rules/{service}/{rule_key}` | Update one rule |
| `DELETE` | `/admin/rules/{service}/{rule_key}` | Revert to global default |

**Example — tighten threshold for a critical endpoint:**

```bash
curl -X PATCH http://localhost:8000/admin/rules/atlas-api/high_frequency_threshold \
     -H "Content-Type: application/json" \
     -d '{"value": 5}'
```

---

## Configuration

### Global thresholds (`.env` / `config/settings.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `AGGREGATION_INTERVAL_SECONDS` | 60 | How often the pipeline runs |
| `AGGREGATION_WINDOW_MINUTES` | 5 | Trailing window for aggregation |
| `DEFAULT_HIGH_FREQUENCY_THRESHOLD` | 10 | Count to trigger high-frequency |
| `DEFAULT_SPIKE_RATIO_THRESHOLD` | 2.0 | Current/previous ratio for spike |
| `DEFAULT_REPEATED_IDENTICAL_MIN_COUNT` | 5 | Same message repeat count |
| `DEFAULT_ALERT_COOLDOWN_MINUTES` | 15 | Min minutes between same alerts |
| `SEVERITY_P0_COUNT_THRESHOLD` | 50 | Count → P0 severity |
| `SEVERITY_P1_COUNT_THRESHOLD` | 10 | Count → P1 severity |
| `SLACK_WEBHOOK_URL` | — | Slack webhook (optional) |

### Service registry (`config/service_registry.py`)

Defines each service's **criticality** (business importance, static) and seeds initial rules into the DB on startup:

```python
SERVICE_REGISTRY = {
    "atlas-api": {
        "criticality": "P0",
        "rules": {
            "high_frequency_threshold": 20,
        },
        "endpoints": {
            "POST /reports/generate": {
                "rules": {
                    "failure_ratio_threshold": 0.67,  # 2 out of 3
                    "failure_ratio_window": 3,
                }
            }
        }
    }
}
```

> Rules seeded from `SERVICE_REGISTRY` use `INSERT OR IGNORE` — rules changed via the Admin API are never overwritten on restart.

---

## Key features

- **Dual granularity:** Service-level and endpoint-level aggregation.
- **Pattern detection:** High frequency, repeated identical errors, sudden spikes, failure ratio (e.g. 2 out of 3).
- **Predictive alerts:** Rate-of-increase, upward trend, repeated failures → "potential outage" warning before threshold breach.
- **Severity from thresholds only:** P0/P1/P2 derived from error count + pattern type; clients never send severity.
- **Live-editable rules:** Admin API changes per-service/endpoint thresholds instantly via DB; no restart needed.
- **Rules fallback chain:** Global defaults → service rules → endpoint rules.
- **Noise reduction:** Alert key, cooldown, re-alert on change/worsen, resolved state.
- **Dual-audience messages:** Engineering (technical) and business (user-impact) copies.
- **Notifications:** Slack webhook, email, console with retry/backoff.

---

## Project structure

```
monitoring/
├── config/
│   ├── settings.py            # Global thresholds and config (env-driven)
│   └── service_registry.py    # Service criticality + seed rules; get_rules() dispatcher
├── src/
│   ├── ingestion/             # POST /errors API, validation, payload models
│   ├── store/
│   │   ├── event_store.py     # SQLite events table
│   │   └── rules_store.py     # SQLite service_rules table + TTL cache
│   ├── aggregation/           # Aggregator, pattern detector, predictive module, runner
│   ├── alerting/              # Noise reduction, message builder, notifier, runner
│   ├── admin/                 # CRUD API for live rule management
│   └── app.py                 # Entry point: HTTP server + scheduler
└── scripts/
    └── simulate_errors.py     # 7 error scenarios + Admin API helpers for demo/testing
```
