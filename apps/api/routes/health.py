from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request, Response

from apps.api.dependencies import SettingsDep
from patchfrog.ops.health import check_readiness

router = APIRouter()


@router.get("/health")
@router.get("/health/live")
async def health_live() -> dict[str, str]:
    """Liveness check. Deliberately has no dependencies on the DB, Redis,
    GitHub, or the LLM provider -- "is the process alive" must never
    depend on an external system."""

    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(request: Request, settings: SettingsDep, response: Response) -> dict[str, Any]:
    """Readiness check: is this instance able to actually serve a real
    request right now? Checks the database (reachable *and* on the
    migration revision this running code expects) and Redis -- never
    GitHub or the LLM provider (their outages don't make the API itself
    unable to accept a webhook; see :mod:`patchfrog.ops.health`'s module
    docstring). Returns HTTP 503 (never 200) when any check fails, so a
    load balancer/orchestrator routes traffic away automatically."""

    engine = request.app.state.db_engine
    report = await check_readiness(engine=engine, redis_url=settings.redis_url)
    if not report.healthy:
        response.status_code = 503
    return {
        "status": "ok" if report.healthy else "unavailable",
        "checks": [asdict(c) for c in report.checks],
    }
