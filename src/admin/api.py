"""
Admin API: CRUD endpoints for managing per-service / per-endpoint alert rules.

All changes take effect within RULES_CACHE_TTL_SECONDS (default 60s) without
requiring a restart.

Endpoints:
  GET    /admin/rules                              – all rules in DB
  GET    /admin/rules/defaults                     – global default rules
  GET    /admin/rules/{service}                    – merged rules for a service
  GET    /admin/rules/{service}/{endpoint:path}    – merged rules for an endpoint
  PUT    /admin/rules/{service}                    – set/update service-level rules (bulk)
  PUT    /admin/rules/{service}/{endpoint:path}    – set/update endpoint-level rules (bulk)
  PATCH  /admin/rules/{service}/{rule_key}         – update one service-level rule
  DELETE /admin/rules/{service}/{rule_key}         – delete one service-level rule (reverts to default)
  DELETE /admin/rules/{service}/{endpoint:path}/{rule_key} – delete one endpoint rule
"""
from __future__ import annotations

from typing import Any, Optional

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from src.store.rules_store import RULE_SCHEMA

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/rules", tags=["admin"])


# ── Request/response models ────────────────────────────────────────────────────

class RuleValue(BaseModel):
    value: Any


class RuleBulk(BaseModel):
    """Dict of rule_key → value for bulk set operations."""
    rules: dict[str, Any]


# ── Helper ────────────────────────────────────────────────────────────────────

def _store(request: Request):
    return request.app.state.rules_store


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", summary="All rules in DB")
async def list_all_rules(request: Request) -> list[dict]:
    return _store(request).get_all_rules()


@router.get("/defaults", summary="Global default rules")
async def get_defaults(request: Request) -> dict:
    return _store(request).get_rules(service=None, endpoint=None)


@router.get("/schema", summary="Valid rule keys and types")
async def get_schema() -> dict:
    return {k: v.__name__ for k, v in RULE_SCHEMA.items()}


@router.get("/{service}", summary="Merged rules for a service")
async def get_service_rules(service: str, request: Request) -> dict:
    return _store(request).get_rules(service=service, endpoint=None)


@router.get("/{service}/{endpoint:path}", summary="Merged rules for service+endpoint")
async def get_endpoint_rules(service: str, endpoint: str, request: Request) -> dict:
    return _store(request).get_rules(service=service, endpoint=endpoint)


@router.put("/{service}", status_code=status.HTTP_200_OK, summary="Bulk set service-level rules")
async def set_service_rules(service: str, body: RuleBulk, request: Request) -> dict:
    store = _store(request)
    errors = []
    for key, value in body.rules.items():
        try:
            store.set_rule(key, value, service=service, endpoint=None)
            logger.info("AUDIT rule_change: service=%s key=%s value=%s", service, key, value)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    return {"status": "ok", "service": service, "updated": list(body.rules.keys())}


@router.put("/{service}/{endpoint:path}", status_code=status.HTTP_200_OK, summary="Bulk set endpoint-level rules")
async def set_endpoint_rules(service: str, endpoint: str, body: RuleBulk, request: Request) -> dict:
    store = _store(request)
    errors = []
    for key, value in body.rules.items():
        try:
            store.set_rule(key, value, service=service, endpoint=endpoint)
            logger.info("AUDIT rule_change: service=%s endpoint=%s key=%s value=%s", service, endpoint, key, value)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    return {"status": "ok", "service": service, "endpoint": endpoint, "updated": list(body.rules.keys())}


@router.patch("/{service}/{rule_key}", summary="Update a single service-level rule")
async def patch_service_rule(service: str, rule_key: str, body: RuleValue, request: Request) -> dict:
    try:
        _store(request).set_rule(rule_key, body.value, service=service, endpoint=None)
        logger.info("AUDIT rule_change: service=%s key=%s value=%s", service, rule_key, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "service": service, "rule_key": rule_key, "value": body.value}


@router.delete("/{service}/{rule_key}", summary="Delete a service-level rule (reverts to global default)")
async def delete_service_rule(service: str, rule_key: str, request: Request) -> dict:
    deleted = _store(request).delete_rule(rule_key, service=service, endpoint=None)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_key}' not found for service '{service}'")
    logger.info("AUDIT rule_delete: service=%s key=%s (reverted to global default)", service, rule_key)
    return {"status": "deleted", "service": service, "rule_key": rule_key}


@router.delete("/{service}/{endpoint:path}/{rule_key}", summary="Delete an endpoint-level rule")
async def delete_endpoint_rule(service: str, endpoint: str, rule_key: str, request: Request) -> dict:
    deleted = _store(request).delete_rule(rule_key, service=service, endpoint=endpoint)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Rule '{rule_key}' not found for service '{service}' endpoint '{endpoint}'",
        )
    logger.info("AUDIT rule_delete: service=%s endpoint=%s key=%s (reverted to global default)", service, endpoint, rule_key)
    return {"status": "deleted", "service": service, "endpoint": endpoint, "rule_key": rule_key}
