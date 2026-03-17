"""
Main entry point.

Wires together:
  - FastAPI HTTP server (ingestion API + /health + /status)
  - EventStore (SQLite)
  - AggregationRunner (pattern detection + predictive)
  - AlertingRunner (noise reduction + message building + notifications)
  - APScheduler (triggers the aggregation→alerting pipeline every N seconds)
"""
from __future__ import annotations

import logging
import sys

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from config.service_registry import init_rules_store, seed_registry_to_db
from src.admin.api import router as admin_router
from src.alerting.alerting_runner import AlertingRunner
from src.alerting.alerting_state import AlertingState
from src.aggregation.runner import AggregationRunner
from src.health.checker import HealthChecker
from src.admin.health_check_api import router as health_check_admin_router
from src.ingestion.api import router as ingestion_router
from src.store.event_store import EventStore
from src.store.rules_store import RulesStore

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Application factory ────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Intelligent Error Monitoring & Predictive Alerting",
        description="Captures, aggregates, and alerts on errors from multiple services.",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Shared state ──────────────────────────────────────────────────────────
    event_store = EventStore(db_path=settings.DATABASE_PATH)
    rules_store = RulesStore(db_path=settings.DATABASE_PATH)
    alerting_state = AlertingState()
    aggregation_runner = AggregationRunner(event_store=event_store)
    alerting_runner = AlertingRunner(alerting_state=alerting_state)

    # Inject rules_store into service_registry so get_rules() uses the DB
    init_rules_store(rules_store)
    # Seed per-service/endpoint rules from SERVICE_REGISTRY (INSERT OR IGNORE)
    seed_registry_to_db(rules_store)

    # Attach to app.state for use in route handlers
    app.state.event_store = event_store
    app.state.rules_store = rules_store
    app.state.alerting_state = alerting_state

    # ── Routes ────────────────────────────────────────────────────────────────
    app.include_router(ingestion_router)
    app.include_router(admin_router)
    app.include_router(health_check_admin_router)

    # ── Scheduler ─────────────────────────────────────────────────────────────

    health_checker = HealthChecker(db_path=settings.DATABASE_PATH)
    app.state.health_checker = health_checker

    def pipeline_tick() -> None:
        """One tick: aggregate → detect patterns → alert."""
        try:
            alert_events = aggregation_runner.run()
            alerting_runner.run(alert_events)
        except Exception as exc:
            logger.exception("Pipeline tick failed: %s", exc)

    def health_check_tick() -> None:
        """Poll all registered service health endpoints → alert on failures."""
        try:
            alert_events = health_checker.run()
            if alert_events:
                alerting_runner.run(alert_events)
        except Exception as exc:
            logger.exception("Health check tick failed: %s", exc)

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        pipeline_tick,
        trigger="interval",
        seconds=settings.AGGREGATION_INTERVAL_SECONDS,
        id="aggregation_pipeline",
        replace_existing=True,
    )
    scheduler.add_job(
        health_check_tick,
        trigger="interval",
        seconds=settings.HEALTH_CHECK_INTERVAL_SECONDS,
        id="health_check",
        replace_existing=True,
    )

    @app.on_event("startup")
    async def startup() -> None:
        scheduler.start()
        logger.info(
            "Monitoring system started. "
            "Pipeline runs every %ds. Window: %d min. "
            "Health checks every %ds.",
            settings.AGGREGATION_INTERVAL_SECONDS,
            settings.AGGREGATION_WINDOW_MINUTES,
            settings.HEALTH_CHECK_INTERVAL_SECONDS,
        )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        scheduler.shutdown(wait=False)
        logger.info("Monitoring system stopped.")

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "src.app:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=False,
        log_level="info",
    )
