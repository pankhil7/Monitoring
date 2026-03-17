"""
Ingestion API: POST /errors endpoint.

Supports multiple client payload formats via per-client adapters.
Clients declare their format using the X-Client-Type header (default: "canonical").

Flow:
  raw body  →  Adapter.normalise()  →  ErrorEventRequest (Pydantic)  →  EventStore
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from src.ingestion.adapters import ADAPTER_REGISTRY, AdapterError, get_adapter
from src.ingestion.models import ErrorEvent, ErrorEventRequest, ErrorEventResponse
from src.ingestion.validation import validate_timestamp_freshness, validate_service_name

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingestion"])


@router.post(
    "/errors",
    response_model=ErrorEventResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest an error event",
    description=(
        "Accepts error events from any service. "
        "Set `X-Client-Type` header to match the payload format "
        "(canonical | sentry | datadog | cloudwatch | custom). "
        "For `custom`, optionally set `X-Field-Map` to a JSON object mapping "
        "canonical field names to your field names, "
        "e.g. `{\"service\": \"source\", \"message\": \"error_text\"}`."
    ),
)
async def ingest_error(
    request: Request,
    x_client_type: str = Header(default="canonical", alias="X-Client-Type"),
    x_field_map: Optional[str] = Header(default=None, alias="X-Field-Map"),
) -> Any:
    # ── 1. Parse raw body ─────────────────────────────────────────────────────
    try:
        raw_body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON",
        )

    # ── 2. Parse optional field map (custom adapter) ──────────────────────────
    field_map: Optional[dict] = None
    if x_field_map:
        try:
            field_map = json.loads(x_field_map)
            if not isinstance(field_map, dict):
                raise ValueError("X-Field-Map must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid X-Field-Map header: {exc}",
            )

    # ── 3. Look up adapter (never raises — falls back to canonical) ───────────
    adapter, resolved_client_type = get_adapter(x_client_type, raw=raw_body)

    # ── 4. Normalise payload to canonical shape ───────────────────────────────
    try:
        canonical = adapter.normalise(raw_body, field_map=field_map)
    except AdapterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"[{resolved_client_type}] {exc}",
        )
    except Exception as exc:
        logger.exception("Adapter %s failed: %s", resolved_client_type, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"[{resolved_client_type}] Could not normalise payload: {exc}",
        )

    logger.debug(
        "Adapter '%s' normalised payload: service=%s endpoint=%s",
        resolved_client_type, canonical.get("service"), canonical.get("endpoint"),
    )

    # ── 5. Validate canonical payload ─────────────────────────────────────────
    try:
        payload = ErrorEventRequest(**canonical)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Validation error after normalisation: {exc}",
        )

    # ── 6. Extra non-fatal warnings ───────────────────────────────────────────
    warnings = validate_timestamp_freshness(payload) + validate_service_name(payload.service)
    for w in warnings:
        logger.warning("Ingestion warning service=%s client=%s: %s", payload.service, x_client_type, w)

    # ── 7. Persist ────────────────────────────────────────────────────────────
    event_store = request.app.state.event_store
    event = ErrorEvent.from_request(payload)
    event_store.add(event)

    logger.info(
        "Ingested id=%s service=%s endpoint=%s client_type=%s resolved_adapter=%s",
        event.id,
        event.service,
        event.endpoint,
        x_client_type,
        resolved_client_type,
    )

    response_extra: dict = {}
    if resolved_client_type != x_client_type.lower():
        # Inform the caller which adapter was actually used so they can fix their header.
        response_extra["adapter_used"] = resolved_client_type
        response_extra["adapter_note"] = (
            f"X-Client-Type '{x_client_type}' was not recognised. "
            f"Adapter '{resolved_client_type}' was used instead."
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"id": event.id, **response_extra},
    )


@router.get("/health", tags=["system"], summary="Health check")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/status", tags=["system"], summary="Current incidents and recent alerts")
async def system_status(request: Request) -> dict:
    alerting_state = request.app.state.alerting_state
    incidents = alerting_state.get_active_incidents()
    recent_alerts = alerting_state.get_recent_alerts(limit=20)
    return {
        "active_incidents": [inc.model_dump() for inc in incidents],
        "recent_alerts": [a.model_dump() for a in recent_alerts],
    }


@router.get("/adapters", tags=["ingestion"], summary="List supported client types")
async def list_adapters() -> dict:
    return {
        "supported_client_types": list(ADAPTER_REGISTRY.keys()),
        "header": "X-Client-Type",
        "default": "canonical",
        "custom_field_map_header": "X-Field-Map (JSON object, for custom type only)",
    }


