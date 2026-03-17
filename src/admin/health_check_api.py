"""
Admin API: health-check state inspection and test helpers.

Endpoints:
  GET    /admin/health-check/state            – all rows in health_check_state table
  GET    /admin/health-check/state/{service}  – single service state
  DELETE /admin/health-check/state/{service}  – reset consecutive_failures to 0
  POST   /admin/health-check/seed/{service}   – pre-seed N consecutive failures
                                                (test helper — simulates N past failed ticks)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/health-check", tags=["admin", "health-check"])


def _checker(request: Request):
    return request.app.state.health_checker


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get(
    "/state",
    summary="Current health-check state for all services",
    description=(
        "Returns every row in the health_check_state table — "
        "consecutive failure counts, last checked timestamp, and last error."
    ),
)
async def get_health_check_state(request: Request) -> list[dict]:
    checker = _checker(request)
    with checker._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM health_check_state ORDER BY service"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get(
    "/state/{service}",
    summary="Health-check state for a single service",
)
async def get_service_health_check_state(service: str, request: Request) -> dict:
    checker = _checker(request)
    state = checker._get_state(service)
    if state["consecutive_failures"] == 0 and state["last_error"] is None:
        # Could be genuinely healthy or just never checked — distinguish via DB
        with checker._connect() as conn:
            row = conn.execute(
                "SELECT * FROM health_check_state WHERE service = ?", (service,)
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Service '{service}' has no health-check state recorded yet.",
            )
    return state


@router.delete(
    "/state/{service}",
    summary="Reset health-check state for a service",
    description="Resets consecutive_failures to 0. Useful after a fix or for test cleanup.",
)
async def reset_service_health_check_state(service: str, request: Request) -> dict:
    checker = _checker(request)
    now = datetime.now(timezone.utc).isoformat()
    checker._record_recovery(service, now)
    logger.info("AUDIT health_check_reset: service=%s", service)
    return {"status": "reset", "service": service, "consecutive_failures": 0}


class SeedRequest(BaseModel):
    consecutive_failures: int = 2
    error: str = "Connection refused (test seed)"


@router.post(
    "/seed/{service}",
    status_code=status.HTTP_201_CREATED,
    summary="Pre-seed consecutive failures (test helper)",
    description=(
        "Directly writes N consecutive failures into health_check_state for "
        "the given service. Use this to simulate 'already failed N ticks' so "
        "the very next health-check tick pushes the count to the alert threshold "
        "without waiting N full intervals. TEST USE ONLY."
    ),
)
async def seed_failures(service: str, body: SeedRequest, request: Request) -> dict:
    checker = _checker(request)
    now = datetime.now(timezone.utc).isoformat()
    with checker._lock, checker._connect() as conn:
        conn.execute(
            """
            INSERT INTO health_check_state
                (service, consecutive_failures, last_checked_at, last_error)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(service) DO UPDATE SET
                consecutive_failures = excluded.consecutive_failures,
                last_checked_at      = excluded.last_checked_at,
                last_error           = excluded.last_error
            """,
            (service, body.consecutive_failures, now, body.error),
        )
        conn.commit()
    logger.info(
        "AUDIT health_check_seed: service=%s consecutive_failures=%d",
        service, body.consecutive_failures,
    )
    return {
        "status": "seeded",
        "service": service,
        "consecutive_failures": body.consecutive_failures,
        "note": (
            f"Next health-check tick will increment to "
            f"{body.consecutive_failures + 1}. "
            f"Alert threshold is HEALTH_CHECK_CONSECUTIVE_FAILURES "
            f"(default 3)."
        ),
    }
